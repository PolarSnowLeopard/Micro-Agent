"""Dependency-aware, budgeted repository evidence for the planning Agent.

The implementation follows the useful parts of AMQ-Wrap's preparation stage:

* DARP propagates intent relevance through internal import and call edges.
* BAGE spends the prompt budget on the most relevant files at the richest
  available granularity while retaining a compact inventory of the remainder.

This module is deliberately deterministic and reference-free. It never reads
benchmark ground truth or decides which symbols become MCP tools.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from micro_agent.packaging.analyzer import FileInfo, RepositoryIR, SymbolInfo


_STOP_WORDS = {
    "algorithm",
    "api",
    "code",
    "data",
    "function",
    "input",
    "mcp",
    "method",
    "model",
    "module",
    "output",
    "repository",
    "request",
    "result",
    "return",
    "server",
    "service",
    "tool",
    "use",
    "using",
    "with",
}


def build_relevance_evidence(
    ir: RepositoryIR,
    intent: str,
    *,
    max_tokens: int = 14_000,
    alpha_forward: float = 0.60,
    alpha_backward: float = 0.30,
    max_depth: int = 3,
    threshold: float = 0.10,
) -> dict[str, Any]:
    """Build DARP+BAGE evidence suitable for the planner's initial context."""

    python_files = {
        file.path: file for file in ir.files if file.kind == "python"
    }
    if not python_files:
        return {
            "method": "darp-bage/v1",
            "overview": {
                "totalFiles": len(ir.files),
                "pythonFiles": 0,
                "relevantFiles": 0,
                "seedFiles": [],
            },
            "detailed": {},
            "compact": {},
            "minimal": [],
        }

    forward, backward = _dependency_graph(ir, set(python_files))
    seeds, reasons = _select_seeds(ir, intent, set(python_files))
    relevance = _propagate_relevance(
        forward,
        backward,
        seeds,
        alpha_forward=alpha_forward,
        alpha_backward=alpha_backward,
        max_depth=max_depth,
        threshold=threshold,
    )
    if not relevance:
        fallback = sorted(python_files)[: min(20, len(python_files))]
        relevance = {path: 1.0 for path in fallback}
        seeds = set(fallback)
        reasons.update({path: ["deterministic fallback"] for path in fallback})

    return _encode_with_budget(
        ir,
        python_files,
        relevance,
        seeds,
        reasons,
        max_tokens=max_tokens,
        parameters={
            "alphaForward": alpha_forward,
            "alphaBackward": alpha_backward,
            "maxDepth": max_depth,
            "threshold": threshold,
        },
    )


def _dependency_graph(
    ir: RepositoryIR,
    python_files: set[str],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    forward: dict[str, set[str]] = defaultdict(set)
    backward: dict[str, set[str]] = defaultdict(set)
    module_to_file = _module_file_index(ir, python_files)

    for source_file, imports in ir.imports.items():
        if source_file not in python_files:
            continue
        for imported in imports:
            target = _resolve_import(imported, module_to_file)
            if target and target != source_file:
                forward[source_file].add(target)

    symbols_by_name: dict[str, list[SymbolInfo]] = defaultdict(list)
    for symbol in ir.symbols:
        if symbol.file not in python_files:
            continue
        symbols_by_name[symbol.qualifiedName].append(symbol)
        symbols_by_name[f"{symbol.module}.{symbol.name}"].append(symbol)
        symbols_by_name[symbol.name].append(symbol)
    for source in ir.symbols:
        if source.file not in python_files:
            continue
        for call in source.calls:
            matches: dict[str, SymbolInfo] = {}
            candidates = (call, call.removeprefix("self."), call.rsplit(".", 1)[-1])
            for candidate in candidates:
                for target_symbol in symbols_by_name.get(candidate, []):
                    matches[target_symbol.qualifiedName] = target_symbol
            if len(matches) != 1:
                continue
            target = next(iter(matches.values()))
            if target.file != source.file:
                forward[source.file].add(target.file)

    for source, targets in forward.items():
        for target in targets:
            backward[target].add(source)
    return forward, backward


def _module_file_index(ir: RepositoryIR, python_files: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in python_files:
        module = path[:-3].replace("/", ".") if path.endswith(".py") else path.replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        result[module] = path
    for symbol in ir.symbols:
        if symbol.file in python_files:
            result.setdefault(symbol.module, symbol.file)
    return result


def _resolve_import(imported: str, module_to_file: dict[str, str]) -> str | None:
    normalized = imported.lstrip(".")
    candidates = [
        (module, path)
        for module, path in module_to_file.items()
        if normalized == module or normalized.startswith(module + ".")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item[0]))[1]


def _select_seeds(
    ir: RepositoryIR,
    intent: str,
    python_files: set[str],
) -> tuple[set[str], dict[str, list[str]]]:
    seeds: set[str] = set()
    reasons: dict[str, list[str]] = defaultdict(list)

    for path in ir.entrypointHints:
        if path in python_files:
            seeds.add(path)
            reasons[path].append("entrypoint")
    if "main.py" in python_files:
        seeds.add("main.py")
        reasons["main.py"].append("root template/entrypoint")

    terms = _tokens(intent)
    if terms:
        blobs = {
            path: _file_search_blob(path, ir.symbols)
            for path in python_files
        }
        document_frequency = {
            term: sum(term in blob for blob in blobs.values()) for term in terms
        }
        count = max(len(blobs), 1)
        scored: list[tuple[float, str]] = []
        for path, blob in blobs.items():
            path_tokens = _tokens(path)
            score = 0.0
            for term in terms:
                if term not in blob:
                    continue
                inverse_frequency = math.log((count + 1) / (document_frequency[term] + 1)) + 1.0
                score += inverse_frequency * (3.0 if term in path_tokens else 1.0)
            if score:
                scored.append((score, path))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if scored:
            cutoff = max(scored[0][0] * 0.45, scored[min(3, len(scored) - 1)][0])
            for score, path in scored[:4]:
                if score < cutoff:
                    continue
                seeds.add(path)
                reasons[path].append(f"intent match score={score:.3f}")

    if not seeds:
        public_files = sorted(
            {
                symbol.file
                for symbol in ir.symbols
                if symbol.file in python_files and symbol.isPublic
            }
        )
        for path in public_files[: min(8, len(public_files))]:
            seeds.add(path)
            reasons[path].append("public callable fallback")
    return seeds, dict(reasons)


def _tokens(text: str) -> set[str]:
    english = {
        token
        for token in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
        if token not in _STOP_WORDS
    }
    chinese_runs = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    chinese = {
        run[index : index + 2]
        for run in chinese_runs
        for index in range(max(1, len(run) - 1))
    }
    return english | chinese


def _file_search_blob(path: str, symbols: Iterable[SymbolInfo]) -> set[str]:
    parts = [path]
    for symbol in symbols:
        if symbol.file != path:
            continue
        parts.extend((symbol.name, symbol.qualifiedName, symbol.docstring))
    return _tokens(" ".join(parts))


def _propagate_relevance(
    forward: dict[str, set[str]],
    backward: dict[str, set[str]],
    seeds: set[str],
    *,
    alpha_forward: float,
    alpha_backward: float,
    max_depth: int,
    threshold: float,
) -> dict[str, float]:
    relevance = {seed: 1.0 for seed in seeds}
    queue: deque[tuple[str, int, float]] = deque(
        (seed, 0, 1.0) for seed in sorted(seeds)
    )
    while queue:
        source, depth, source_score = queue.popleft()
        if depth >= max_depth:
            continue
        neighbors = (
            (forward.get(source, set()), alpha_forward),
            (backward.get(source, set()), alpha_backward),
        )
        for targets, alpha in neighbors:
            fanout_damping = min(1.0, 10.0 / len(targets)) if targets else 1.0
            for target in sorted(targets):
                score = source_score * alpha * fanout_damping
                if score < threshold or score <= relevance.get(target, 0.0):
                    continue
                relevance[target] = score
                queue.append((target, depth + 1, score))
    return {
        path: round(score, 4)
        for path, score in relevance.items()
        if score >= threshold
    }


def _encode_with_budget(
    ir: RepositoryIR,
    files: dict[str, FileInfo],
    relevance: dict[str, float],
    seeds: set[str],
    reasons: dict[str, list[str]],
    *,
    max_tokens: int,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    ordered = sorted(relevance, key=lambda path: (-relevance[path], path))
    below_threshold = sorted(set(files) - set(relevance))
    evidence: dict[str, Any] = {
        "method": "darp-bage/v1",
        "parameters": {**parameters, "maxTokens": max_tokens},
        "overview": {
            "totalFiles": len(ir.files),
            "pythonFiles": len(files),
            "relevantFiles": len(relevance),
            "seedFiles": [
                {
                    "path": path,
                    "reasons": reasons.get(path, []),
                }
                for path in sorted(seeds)
            ],
            "belowThresholdFileCount": len(below_threshold),
            "belowThresholdPaths": below_threshold[:100],
        },
        "detailed": {},
        "compact": {},
        "minimal": [],
    }
    while (
        _estimate_tokens(evidence) > max_tokens
        and evidence["overview"]["belowThresholdPaths"]
    ):
        evidence["overview"]["belowThresholdPaths"].pop()
    # Reserve a small amount for the final inclusion counters.
    remaining = max(0, max_tokens - _estimate_tokens(evidence) - 100)
    symbols_by_file: dict[str, list[SymbolInfo]] = defaultdict(list)
    for symbol in ir.symbols:
        symbols_by_file[symbol.file].append(symbol)

    for path in ordered:
        score = relevance[path]
        detailed = _detailed_file(
            files[path], score, ir.imports.get(path, []), symbols_by_file[path]
        )
        compact = _compact_file(files[path], score, symbols_by_file[path])
        minimal = {
            "path": path,
            "relevance": score,
            "size": files[path].size,
            "publicCallableCount": sum(
                symbol.isPublic and symbol.kind not in {"class"}
                for symbol in symbols_by_file[path]
            ),
        }
        options = (
            (("detailed", detailed), ("compact", compact), ("minimal", minimal))
            if score >= 0.50
            else (
                (("compact", compact), ("detailed", detailed), ("minimal", minimal))
                if score >= 0.20
                else (("minimal", minimal),)
            )
        )
        for section, item in options:
            cost = _estimate_tokens({path: item})
            if cost > remaining:
                continue
            if section == "minimal":
                evidence[section].append(item)
            else:
                evidence[section][path] = item
            remaining -= cost
            break

    included = (
        set(evidence["detailed"])
        | set(evidence["compact"])
        | {item["path"] for item in evidence["minimal"]}
    )
    omitted = [path for path in ordered if path not in included]
    evidence["overview"]["includedRelevantFiles"] = len(included)
    if omitted:
        evidence["overview"]["budgetOmittedRelevantFileCount"] = len(omitted)
    evidence["overview"]["estimatedTokens"] = _estimate_tokens(evidence)
    return evidence


def _detailed_file(
    file: FileInfo,
    relevance: float,
    imports: list[str],
    symbols: list[SymbolInfo],
) -> dict[str, Any]:
    return {
        "relevance": relevance,
        "size": file.size,
        "imports": imports,
        "symbols": [
            {
                "qualifiedName": symbol.qualifiedName,
                "kind": symbol.kind,
                "line": symbol.line,
                "signature": symbol.signature,
                "docstring": symbol.docstring[:400],
                "calls": symbol.calls[:30],
                "dispatchBranches": symbol.dispatchBranches,
                "isGenerator": symbol.isGenerator,
                "failureReturns": symbol.failureReturns,
                "isPublic": symbol.isPublic,
            }
            for symbol in sorted(symbols, key=lambda item: item.line)
        ],
    }


def _compact_file(
    file: FileInfo,
    relevance: float,
    symbols: list[SymbolInfo],
) -> dict[str, Any]:
    public = [symbol for symbol in symbols if symbol.isPublic]
    return {
        "relevance": relevance,
        "size": file.size,
        "publicSymbols": [
            {
                "qualifiedName": symbol.qualifiedName,
                "signature": symbol.signature,
                "docstringFirstLine": symbol.docstring.splitlines()[0][:180]
                if symbol.docstring
                else "",
            }
            for symbol in sorted(public, key=lambda item: item.line)[:30]
        ],
        "privateSymbolCount": len(symbols) - len(public),
    }


def _estimate_tokens(value: Any) -> int:
    # Three characters per token is intentionally conservative for mixed
    # Chinese, identifiers, and JSON punctuation.
    return len(str(value)) // 3 + 1


__all__ = ["build_relevance_evidence"]
