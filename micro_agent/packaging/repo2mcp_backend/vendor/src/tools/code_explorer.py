"""代码探索工具 — 基于 AST cache 提供结构化仓库探索能力"""
import json
import os
import re
from typing import Optional

from src.tools.base import BaseTool, ToolResult


class CodeExplorerTool(BaseTool):
    """让 Agent 按需探索仓库代码，而非将所有内容预加载到上下文中。

    三种 action:
      search_symbol — 在 AST cache 中搜索函数/类名
      inspect_file  — 返回指定文件的完整 AST 详情
      read_source   — 读取源码指定行范围
    """

    name = "code_explorer"
    description = (
        "Explore repository code structure on demand. "
        "Actions: "
        "(1) search_symbol: search function/class names matching a query across the repo; "
        "(2) inspect_file: get detailed AST info (signatures, docstrings, imports) for a specific file; "
        "(3) read_source: read actual source code lines from a file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search_symbol", "inspect_file", "read_source"],
                "description": "The exploration action to perform",
            },
            "query": {
                "type": "string",
                "description": "For search_symbol: regex or substring to match against function/class names",
            },
            "file_path": {
                "type": "string",
                "description": "For inspect_file / read_source: relative file path in the repository",
            },
            "start_line": {
                "type": "integer",
                "description": "For read_source: start line number (1-based, default 1)",
            },
            "end_line": {
                "type": "integer",
                "description": "For read_source: end line number (inclusive, default start+100)",
            },
        },
        "required": ["action"],
    }

    MAX_RESULTS = 30
    MAX_SOURCE_LINES = 200

    def __init__(self, ast_cache: dict, source_dir: str):
        self.ast_cache = ast_cache
        self.source_dir = source_dir

    def execute(
        self,
        action: str,
        query: str = "",
        file_path: str = "",
        start_line: int = 1,
        end_line: Optional[int] = None,
    ) -> ToolResult:
        if action == "search_symbol":
            return self._search_symbol(query)
        elif action == "inspect_file":
            return self._inspect_file(file_path)
        elif action == "read_source":
            return self._read_source(file_path, start_line, end_line)
        return ToolResult(success=False, output="", error=f"Unknown action: {action}")

    def _search_symbol(self, query: str) -> ToolResult:
        if not query:
            return ToolResult(success=False, output="", error="query is required for search_symbol")
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(query), re.IGNORECASE)

        hits = []
        for fp, analysis in self.ast_cache.items():
            for func in analysis.get("functions", []):
                if pattern.search(func["name"]):
                    sig = func["name"]
                    args = func.get("args", [])
                    if args:
                        sig += f"({', '.join(a['name'] for a in args)})"
                    else:
                        sig += "()"
                    if func.get("return_type"):
                        sig += f" -> {func['return_type']}"
                    hits.append({"file": fp, "type": "function", "line": func.get("line"), "signature": sig})
            for cls in analysis.get("classes", []):
                if pattern.search(cls["name"]):
                    methods = [m["name"] for m in cls.get("methods", []) if not m["name"].startswith("_")]
                    hits.append({
                        "file": fp, "type": "class", "line": cls.get("line"),
                        "name": cls["name"], "public_methods": methods[:10],
                    })

        if not hits:
            return ToolResult(success=True, output=f"No symbols matching '{query}' found.")
        if len(hits) > self.MAX_RESULTS:
            hits = hits[: self.MAX_RESULTS]
            truncated = True
        else:
            truncated = False
        out = json.dumps(hits, ensure_ascii=False, indent=1)
        if truncated:
            out += f"\n... ({len(hits)}/{self.MAX_RESULTS} shown, refine your query for more precise results)"
        return ToolResult(success=True, output=out)

    def _inspect_file(self, file_path: str) -> ToolResult:
        if not file_path:
            return ToolResult(success=False, output="", error="file_path is required for inspect_file")
        analysis = self.ast_cache.get(file_path)
        if analysis is None:
            for fp in self.ast_cache:
                if fp.endswith("/" + file_path) or file_path.endswith("/" + fp):
                    analysis = self.ast_cache[fp]
                    file_path = fp
                    break
        if analysis is None:
            return ToolResult(success=False, output="", error=f"File not found in AST cache: {file_path}")

        info = {"file": file_path, "lines": analysis["lines"]}
        if analysis.get("imports"):
            info["imports"] = analysis["imports"]
        if analysis.get("functions"):
            info["functions"] = []
            for func in analysis["functions"]:
                sig = func["name"]
                args = func.get("args", [])
                arg_strs = []
                for a in args:
                    s = a["name"]
                    if a.get("type"):
                        s += f": {a['type']}"
                    arg_strs.append(s)
                sig += f"({', '.join(arg_strs)})"
                if func.get("return_type"):
                    sig += f" -> {func['return_type']}"
                entry = {"signature": sig, "line": func.get("line")}
                if func.get("docstring"):
                    entry["docstring"] = func["docstring"].split("\n")[0].strip()
                info["functions"].append(entry)
        if analysis.get("classes"):
            info["classes"] = []
            for cls in analysis["classes"]:
                entry = {"name": cls["name"], "line": cls.get("line")}
                if cls.get("docstring"):
                    entry["docstring"] = cls["docstring"].split("\n")[0].strip()
                entry["methods"] = []
                for m in cls.get("methods", []):
                    msig = m["name"]
                    margs = [a["name"] for a in m.get("args", [])]
                    msig += f"({', '.join(margs)})"
                    if m.get("return_type"):
                        msig += f" -> {m['return_type']}"
                    entry["methods"].append(msig)
                info["classes"].append(entry)
        return ToolResult(success=True, output=json.dumps(info, ensure_ascii=False, indent=1))

    def _read_source(self, file_path: str, start: int, end: Optional[int]) -> ToolResult:
        if not file_path:
            return ToolResult(success=False, output="", error="file_path is required for read_source")
        abs_path = os.path.join(self.source_dir, file_path)
        if not os.path.isfile(abs_path):
            for fp in self.ast_cache:
                candidate = os.path.join(self.source_dir, fp)
                if (fp.endswith("/" + file_path) or file_path.endswith("/" + fp)) and os.path.isfile(candidate):
                    abs_path = candidate
                    file_path = fp
                    break
        if not os.path.isfile(abs_path):
            return ToolResult(success=False, output="", error=f"Source file not found: {file_path}")

        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

        start = max(1, start)
        if end is None:
            end = start + 100
        end = min(end, start + self.MAX_SOURCE_LINES - 1, len(lines))

        selected = lines[start - 1 : end]
        numbered = [f"{i:>5}| {line.rstrip()}" for i, line in enumerate(selected, start=start)]
        header = f"# {file_path} (lines {start}-{end} of {len(lines)})"
        return ToolResult(success=True, output=header + "\n" + "\n".join(numbered))
