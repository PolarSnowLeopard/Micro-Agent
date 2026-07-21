#!/usr/bin/env python3
"""
AST 代码结构分析器

对 Python 代码仓库进行静态分析，提取文件结构、类/函数签名、import 依赖。

支持两种模式：
  listing — 输出轻量文件清单（供 LLM 选择相关文件）
  tiered  — 基于 DARP (Dependency-Aware Relevance Propagation) 的精准子图
           提取 + BAGE (Budget-constrained Adaptive Granularity Encoding) 编码

种子选择方式（优先级）：
  1. --seeds-file：LLM 显式选择的文件列表
  2. --intent-file：关键词+IDF 自动匹配（降级方案）
"""
import argparse
import ast
import json
import math
import os
import re
import sys
from collections import defaultdict, deque


# ====== 核心分析 ======

def analyze_file(filepath: str) -> dict | None:
    """分析单个 Python 文件"""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            source = f.read()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return None

    functions = []
    classes = []
    imports = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_extract_function(node))

        elif isinstance(node, ast.ClassDef):
            methods = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(_extract_function(item))
            classes.append({
                "name": node.name,
                "docstring": ast.get_docstring(node),
                "methods": methods,
                "line": node.lineno
            })

        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    if not functions and not classes:
        return None

    return {
        "functions": functions,
        "classes": classes,
        "imports": sorted(set(imports)),
        "lines": len(source.splitlines())
    }


def _extract_function(node) -> dict:
    """提取函数信息"""
    args = []
    for arg in node.args.args:
        if arg.arg == 'self':
            continue
        arg_info = {"name": arg.arg}
        if arg.annotation:
            try:
                arg_info["type"] = ast.unparse(arg.annotation)
            except Exception:
                pass
        args.append(arg_info)

    return_type = None
    if node.returns:
        try:
            return_type = ast.unparse(node.returns)
        except Exception:
            pass

    return {
        "name": node.name,
        "args": args,
        "return_type": return_type,
        "docstring": ast.get_docstring(node),
        "line": node.lineno,
        "is_async": isinstance(node, ast.AsyncFunctionDef)
    }


def analyze_directory(dirpath: str) -> dict:
    """分析目录下所有 Python 文件"""
    results = {}
    skip_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv',
                 '.tox', 'dist', 'build', '.eggs', '.mypy_cache',
                 'test', 'tests', 'testing', 'benchmarks', 'examples',
                 'docs', 'doc', 'tutorials', 'samples'}

    for root, dirs, files in os.walk(dirpath):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if f.endswith('.py'):
                filepath = os.path.join(root, f)
                relpath = os.path.relpath(filepath, dirpath)
                analysis = analyze_file(filepath)
                if analysis:
                    results[relpath] = analysis

    return results


# ====== Listing 模式：生成 LLM 可读的轻量文件清单 ======

def format_listing(results: dict) -> str:
    """生成紧凑的文件清单文本，供 LLM 选择相关文件"""
    lines = []
    for fp in sorted(results.keys()):
        a = results[fp]
        parts = [fp]
        fn_names = [f["name"] for f in a.get("functions", []) if not f["name"].startswith("_")]
        cls_names = [c["name"] for c in a.get("classes", [])]
        if fn_names:
            names = fn_names if len(fn_names) <= 10 else fn_names[:10] + [f"+{len(fn_names)-10}"]
            parts.append(f"fn: {', '.join(names)}")
        if cls_names:
            parts.append(f"cls: {', '.join(cls_names)}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


# ====== 有向依赖图 ======

def _build_module_map(results: dict) -> tuple[set[str], dict[str, str]]:
    """构建内部包集合与模块名→文件路径映射"""
    internal_pkgs = set()
    for filepath in results:
        parts = filepath.split('/')
        if len(parts) > 1:
            internal_pkgs.add(parts[0])

    module_to_file: dict[str, str] = {}
    for filepath in results:
        mod = filepath.replace('/', '.').replace('\\', '.')
        if mod.endswith('.py'):
            mod = mod[:-3]
        module_to_file[mod] = filepath
        if mod.endswith('.__init__'):
            module_to_file[mod.removesuffix('.__init__')] = filepath

    return internal_pkgs, module_to_file


def build_directed_dep_graph(
    results: dict,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """构建文件级有向依赖图（仅追踪项目内部 import）

    Returns:
        (forward_graph, backward_graph)
        forward_graph[A] = {B, C}  表示 A import 了 B 和 C
        backward_graph[B] = {A}    表示 B 被 A import
    """
    internal_pkgs, module_to_file = _build_module_map(results)

    forward: dict[str, set[str]] = defaultdict(set)
    backward: dict[str, set[str]] = defaultdict(set)

    for filepath, analysis in results.items():
        for imp in analysis.get("imports", []):
            if imp.split('.')[0] not in internal_pkgs:
                continue
            for mod, fpath in module_to_file.items():
                if imp == mod or imp.startswith(mod + '.') or mod.startswith(imp + '.'):
                    if fpath != filepath:
                        forward[filepath].add(fpath)
                        backward[fpath].add(filepath)

    return forward, backward


# ====== DARP: Dependency-Aware Relevance Propagation ======

def _match_seeds(results: dict, seed_paths: set[str]) -> set[str]:
    """模糊匹配种子文件路径，并始终包含顶层配置文件"""
    matched = set()
    for fp in results:
        for seed in seed_paths:
            if fp == seed or fp.endswith('/' + seed) or seed.endswith('/' + fp):
                matched.add(fp)
                break
    for fp in results:
        name = fp.split('/')[-1]
        if name in ('setup.py', 'setup.cfg', '__init__.py') and fp.count('/') <= 1:
            matched.add(fp)
    return matched


def propagate_relevance(
    forward_graph: dict[str, set[str]],
    backward_graph: dict[str, set[str]],
    seeds: set[str],
    alpha_fwd: float = 0.6,
    alpha_bwd: float = 0.3,
    max_depth: int = 3,
    threshold: float = 0.1,
) -> dict[str, float]:
    """在有向依赖图上从种子节点进行非对称相关度传播

    前向边（种子的依赖）衰减较慢 (alpha_fwd)，因为它们定义了接口契约；
    后向边（种子的被使用方）衰减较快 (alpha_bwd)，因为对封装任务帮助有限。

    Returns:
        {filepath: relevance_score}  仅包含 score >= threshold 的文件
    """
    rel: dict[str, float] = {s: 1.0 for s in seeds}
    queue: deque[tuple[str, int]] = deque((s, 0) for s in seeds)

    while queue:
        u, d = queue.popleft()
        if d >= max_depth:
            continue

        fwd_neighbors = forward_graph.get(u, set())
        for v in fwd_neighbors:
            new_rel = rel[u] * alpha_fwd
            if new_rel > rel.get(v, 0):
                rel[v] = new_rel
                queue.append((v, d + 1))

        bwd_neighbors = backward_graph.get(u, set())
        fan_out = len(bwd_neighbors)
        if fan_out > 0:
            damping = min(1.0, 10.0 / fan_out)
            effective_bwd = alpha_bwd * damping
            for v in bwd_neighbors:
                new_rel = rel[u] * effective_bwd
                if new_rel > rel.get(v, 0) and new_rel >= threshold:
                    rel[v] = new_rel
                    queue.append((v, d + 1))

    return {v: s for v, s in rel.items() if s >= threshold}


# ====== 关键词+IDF 降级方案 ======

_STOP_WORDS = frozenset({
    'the', 'and', 'for', 'with', 'from', 'this', 'that', 'will', 'can',
    'are', 'was', 'not', 'use', 'using', 'based', 'need', 'should',
    'also', 'into', 'such', 'has', 'have', 'been', 'its', 'may', 'each',
    'other', 'all', 'new', 'one', 'two', 'more', 'than', 'when', 'what',
    'which', 'how', 'where', 'then', 'only', 'just', 'like', 'some',
    'any', 'most', 'about', 'after', 'before', 'over', 'under', 'very',
    'make', 'get', 'set', 'put', 'take', 'give', 'keep', 'let', 'run',
    'does', 'did', 'done', 'being', 'having', 'doing', 'used',
    'mcp', 'python', 'service', 'server', 'tool', 'tools', 'api',
    'model', 'code', 'data', 'function', 'method', 'class', 'module',
    'input', 'output', 'return', 'result', 'request', 'response',
    'http', 'docker', 'port', 'host', 'client', 'config', 'test',
})


def select_seeds_by_keywords(results: dict, intent: str) -> set[str]:
    """关键词+IDF 降级方案：从 intent 提取关键词匹配种子文件"""
    keywords = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{2,}', intent.lower())
    keywords = {w for w in keywords if w not in _STOP_WORDS}

    top_packages = set()
    for fp in results:
        parts = fp.split('/')
        if len(parts) > 1:
            top_packages.add(parts[0].lower())
    keywords -= top_packages

    if not keywords:
        return set(results.keys())

    n = len(results)
    doc_freq: dict[str, int] = defaultdict(int)
    for fp, a in results.items():
        blob = fp.lower()
        for f in a.get("functions", []):
            blob += " " + f["name"].lower() + " " + (f.get("docstring") or "").lower()
        for c in a.get("classes", []):
            blob += " " + c["name"].lower() + " " + (c.get("docstring") or "").lower()
        for kw in keywords:
            if kw in blob:
                doc_freq[kw] += 1
    idf = {kw: math.log(n / (1 + doc_freq.get(kw, 0))) for kw in keywords}

    scores: dict[str, float] = {}
    for fp, a in results.items():
        s = 0.0
        blob_path = fp.lower()
        for kw in keywords:
            w = idf.get(kw, 1.0)
            if kw in blob_path:
                s += 3.0 * w
            for f in a.get("functions", []):
                if kw in f["name"].lower():
                    s += 2.0 * w
                if kw in (f.get("docstring") or "").lower():
                    s += 0.5 * w
            for c in a.get("classes", []):
                if kw in c["name"].lower():
                    s += 2.0 * w
                if kw in (c.get("docstring") or "").lower():
                    s += 0.5 * w
        scores[fp] = s

    positive = sorted((s for s in scores.values() if s > 0), reverse=True)
    if not positive:
        return set(results.keys())
    threshold = positive[len(positive) // 2] if len(positive) > 10 else 0
    return {fp for fp, s in scores.items() if s > threshold}


# ====== 格式化输出 ======

def _format_file_detailed(analysis: dict) -> dict:
    """精细层：完整签名 + docstring 首行 + imports"""
    info = {"lines": analysis["lines"]}
    if analysis["functions"]:
        info["functions"] = []
        for func in analysis["functions"]:
            sig = f"{func['name']}({', '.join(a['name'] for a in func['args'])})"
            if func.get("return_type"):
                sig += f" -> {func['return_type']}"
            if func.get("docstring"):
                sig += f"  # {func['docstring'].split(chr(10))[0].strip()}"
            info["functions"].append(sig)
    if analysis["classes"]:
        info["classes"] = []
        for cls in analysis["classes"]:
            cls_info = {"name": cls["name"], "methods": [m["name"] for m in cls["methods"]]}
            if cls.get("docstring"):
                cls_info["doc"] = cls["docstring"].split('\n')[0].strip()
            info["classes"].append(cls_info)
    if analysis["imports"]:
        info["imports"] = analysis["imports"]
    return info


def _format_file_compact(analysis: dict) -> dict:
    """概要层：公开函数名（≤8个）+ 类名"""
    info = {"lines": analysis["lines"]}
    if analysis["functions"]:
        public = [f["name"] for f in analysis["functions"] if not f["name"].startswith("_")]
        info["functions"] = public if len(public) <= 8 else public[:8] + [f"+{len(public)-8}"]
        if len(analysis["functions"]) != len(public):
            info["n_private"] = len(analysis["functions"]) - len(public)
    if analysis["classes"]:
        info["classes"] = [c["name"] for c in analysis["classes"]]
    return info


# ====== BAGE: Budget-constrained Adaptive Granularity Encoding ======

def _estimate_tokens(text: str) -> int:
    """启发式 token 估算（偏保守，计入 JSON indent 等结构开销）"""
    return len(text) // 3 + 1


def _format_file_minimal(filepath: str, analysis: dict) -> str:
    """最小层：路径 + 行数 + 公开 API 数量（单行）"""
    n_pub = sum(1 for f in analysis.get("functions", []) if not f["name"].startswith("_"))
    n_cls = len(analysis.get("classes", []))
    return f"{filepath} ({analysis['lines']}L, {n_pub}fn, {n_cls}cls)"


def format_with_budget(
    results: dict,
    relevance: dict[str, float],
    max_tokens: int = 40000,
) -> dict:
    """BAGE: 预算约束下的自适应粒度编码

    按相关度降序遍历文件，为每个文件尝试预算内的最高粒度：
      relevance >= 0.5 → 优先 detailed
      relevance >= 0.2 → 优先 compact
      其余              → minimal（单行摘要）
    """
    sorted_files = sorted(relevance.keys(), key=lambda f: relevance[f], reverse=True)

    overview = {
        "total_repo_files": len(results),
        "relevant_subgraph_files": len(relevance),
        "seed_files": [f for f in sorted_files if relevance[f] >= 1.0],
    }
    remaining = max_tokens - _estimate_tokens(json.dumps(overview, ensure_ascii=False))

    detailed_section: dict = {}
    compact_section: dict = {}
    minimal_list: list[str] = []

    for fp in sorted_files:
        if fp not in results:
            continue
        analysis = results[fp]
        rel = relevance[fp]
        placed = False

        if rel >= 0.5:
            entry = _format_file_detailed(analysis)
            cost = _estimate_tokens(json.dumps({fp: entry}, ensure_ascii=False))
            if cost <= remaining:
                detailed_section[fp] = entry
                remaining -= cost
                placed = True

        if not placed and rel >= 0.2:
            entry = _format_file_compact(analysis)
            cost = _estimate_tokens(json.dumps({fp: entry}, ensure_ascii=False))
            if cost <= remaining:
                compact_section[fp] = entry
                remaining -= cost
                placed = True

        if not placed:
            line = _format_file_minimal(fp, analysis)
            cost = _estimate_tokens(line)
            if cost <= remaining:
                minimal_list.append(line)
                remaining -= cost

    included = set(detailed_section) | set(compact_section) | {
        m.split(" (")[0] for m in minimal_list
    }
    excluded_relevant = [
        fp for fp in sorted_files
        if fp not in included and fp in results
    ]

    summary = dict(overview)
    if detailed_section:
        summary["detailed"] = detailed_section
    if compact_section:
        summary["compact"] = compact_section
    if minimal_list:
        summary["minimal"] = minimal_list
    if excluded_relevant:
        summary["also_relevant"] = (
            f"{len(excluded_relevant)} more files in relevant subgraph "
            f"(use code_explorer to inspect). Top paths: "
            + ", ".join(excluded_relevant[:20])
        )
    return summary


def format_flat_summary(results: dict) -> dict:
    """无 intent 时的扁平摘要（兜底）"""
    summary = {
        "file_count": len(results),
        "total_functions": 0,
        "total_classes": 0,
        "files": {}
    }
    for filepath, analysis in results.items():
        summary["files"][filepath] = _format_file_detailed(analysis)
        summary["total_functions"] += len(analysis.get("functions", []))
        summary["total_classes"] += len(analysis.get("classes", []))
    return summary


# ====== CLI ======

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AST 代码结构分析器 (DARP + BAGE)")
    parser.add_argument("directory", help="要分析的目录路径")
    parser.add_argument("--mode", choices=["listing", "tiered"], default="tiered",
                        help="listing=轻量清单供LLM选择; tiered=DARP子图提取+BAGE编码（默认）")
    parser.add_argument("--seeds-file", default=None,
                        help="LLM 选择的种子文件路径列表（一行一个）")
    parser.add_argument("--intent-file", default=None,
                        help="包含 wrap_intent 的文件（关键词降级方案）")
    parser.add_argument("--cache-file", default=None,
                        help="缓存原始分析结果，避免重复解析")
    parser.add_argument("--max-tokens", type=int, default=40000,
                        help="BAGE 上下文 token 预算（默认 40000）")
    parser.add_argument("--alpha-fwd", type=float, default=0.6,
                        help="DARP 前向（依赖方向）衰减因子（默认 0.6）")
    parser.add_argument("--alpha-bwd", type=float, default=0.3,
                        help="DARP 后向（被依赖方向）衰减因子（默认 0.3）")
    parser.add_argument("--max-depth", type=int, default=3,
                        help="DARP 最大传播跳数（默认 3）")
    parser.add_argument("--threshold", type=float, default=0.1,
                        help="DARP 相关度阈值，低于此值的文件被排除（默认 0.1）")
    args = parser.parse_args()

    # 加载或执行分析
    if args.cache_file and os.path.isfile(args.cache_file):
        with open(args.cache_file, 'r', encoding='utf-8') as f:
            results = json.load(f)
        print(f"Loaded cache: {len(results)} files", file=sys.stderr)
    else:
        if not os.path.isdir(args.directory):
            print(f"Error: {args.directory} is not a directory", file=sys.stderr)
            sys.exit(1)
        results = analyze_directory(args.directory)
        if args.cache_file:
            with open(args.cache_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False)
            print(f"Cached {len(results)} files -> {args.cache_file}", file=sys.stderr)

    # Listing 模式
    if args.mode == "listing":
        print(format_listing(results))
        sys.exit(0)

    # Tiered 模式：DARP 子图提取 + BAGE 编码
    # Step 1: 确定种子
    if args.seeds_file and os.path.isfile(args.seeds_file):
        with open(args.seeds_file, 'r', encoding='utf-8') as f:
            raw_seeds = {line.strip() for line in f if line.strip()}
        seeds = _match_seeds(results, raw_seeds)
        method = "seeds"
    elif args.intent_file and os.path.isfile(args.intent_file):
        with open(args.intent_file, 'r', encoding='utf-8') as f:
            intent = f.read().strip()
        seeds = select_seeds_by_keywords(results, intent)
        method = "keywords"
    else:
        summary = format_flat_summary(results)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        sys.exit(0)

    if not seeds:
        seeds = set(list(results.keys())[:20])

    # Step 2: DARP — 在有向依赖图上传播相关度
    fwd_graph, bwd_graph = build_directed_dep_graph(results)
    relevance = propagate_relevance(
        fwd_graph, bwd_graph, seeds,
        alpha_fwd=args.alpha_fwd,
        alpha_bwd=args.alpha_bwd,
        max_depth=args.max_depth,
        threshold=args.threshold,
    )

    n_detailed = sum(1 for r in relevance.values() if r >= 0.5)
    n_compact = sum(1 for r in relevance.values() if 0.2 <= r < 0.5)
    n_minimal = sum(1 for r in relevance.values() if r < 0.2)
    print(
        f"DARP ({method}): {len(seeds)} seeds -> "
        f"{len(relevance)} relevant files "
        f"(~{n_detailed}D/{n_compact}C/{n_minimal}M) "
        f"out of {len(results)} total",
        file=sys.stderr,
    )

    # Step 3: BAGE — 预算约束下自适应粒度编码
    summary = format_with_budget(results, relevance, max_tokens=args.max_tokens)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
