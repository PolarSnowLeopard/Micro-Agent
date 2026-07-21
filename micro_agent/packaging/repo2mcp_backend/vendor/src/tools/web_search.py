"""联网搜索与文档获取工具"""
import html
import re

import requests

from src.tools.base import BaseTool, ToolResult


class WebSearchTool(BaseTool):
    """搜索互联网获取最新的库文档、API 参考、使用示例等信息。"""

    name = "web_search"
    description = (
        "Search the web for Python library documentation, API references, "
        "usage examples, latest package versions, etc. "
        "Returns top results with titles, URLs, and snippets."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query, e.g. 'fastmcp python sse transport example'"
            }
        },
        "required": ["query"]
    }

    MAX_RESULTS = 5
    MAX_OUTPUT = 3000

    def execute(self, query: str) -> ToolResult:
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=self.MAX_RESULTS))
        except ImportError:
            return ToolResult(
                success=False, output="",
                error="duckduckgo-search not installed. Run: pip install duckduckgo-search"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Search failed: {e}")

        if not results:
            return ToolResult(success=True, output="No results found.")

        parts = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url = r.get("href", "")
            snippet = r.get("body", "")
            parts.append(f"{i}. {title}\n   URL: {url}\n   {snippet}")

        output = "\n\n".join(parts)
        if len(output) > self.MAX_OUTPUT:
            output = output[:self.MAX_OUTPUT] + "\n... [truncated]"
        return ToolResult(success=True, output=output)


class WebFetchTool(BaseTool):
    """从 URL 获取文本内容。适用于阅读文档页面、PyPI 包信息、GitHub README 等。"""

    name = "web_fetch"
    description = (
        "Fetch content from a URL and return it as text. "
        "Useful for reading documentation, PyPI package info "
        "(https://pypi.org/pypi/{package}/json), GitHub READMEs, etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch"
            }
        },
        "required": ["url"]
    }

    MAX_OUTPUT = 5000
    TIMEOUT = 15

    def execute(self, url: str) -> ToolResult:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; Repo2MCP/1.0)"}
            resp = requests.get(url, headers=headers, timeout=self.TIMEOUT)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "json" in content_type:
                text = resp.text
            else:
                text = resp.text
                text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                text = html.unescape(text)

            if len(text) > self.MAX_OUTPUT:
                text = text[:self.MAX_OUTPUT] + "\n... [truncated]"

            return ToolResult(success=True, output=text)
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Fetch failed: {e}")
