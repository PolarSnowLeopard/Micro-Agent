"""
算法模型自动生成 — 知识库检索模块

移植自「面向垂域应用服务的算法模型自动生成」项目的知识库功能，
为 Agent 提示词提供相关论文与开源模型的参考上下文。
"""

import json
import os
import re
import pickle
from pathlib import Path
from typing import List, Dict, Optional

# data.md 默认路径：与本文件同级的 aml_auto_generate_data.md
_DEFAULT_DATA_MD_PATH = os.path.join(
    os.path.dirname(__file__), "aml_auto_generate_data.md"
)


# ---------------------------------------------------------------------------
# 1. data.md 关键词搜索
# ---------------------------------------------------------------------------

def search_data_md(query: str, max_results: int = 5,
                   data_md_path: str = _DEFAULT_DATA_MD_PATH) -> List[str]:
    """
    在 data.md 中按关键词搜索相关的论文和模型条目。

    参数:
        query: 搜索关键词
        max_results: 最大返回条数
        data_md_path: data.md 文件路径

    返回:
        匹配的条目原文列表
    """
    if not os.path.exists(data_md_path):
        return []

    try:
        with open(data_md_path, "r", encoding="utf-8") as f:
            content = f.read()

        entries = re.split(r"(\d+\. \*\*论文\*\*：)", content)
        full_entries: List[str] = []
        for i in range(1, len(entries), 2):
            if i + 1 < len(entries):
                full_entries.append(entries[i] + entries[i + 1])

        query_lower = query.lower()
        relevant: List[str] = []
        for entry in full_entries:
            if query_lower in entry.lower():
                relevant.append(entry.strip())
                if len(relevant) >= max_results:
                    break

        return relevant
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 2. 轻量级研究知识库 (移植自 ResearchStore)
# ---------------------------------------------------------------------------

class ResearchStore:
    """
    基于倒排索引的论文/模型知识库。

    数据存储为 JSON，索引存储为 pickle。
    """

    def __init__(self, storage_dir: str = "research_data"):
        self.storage_dir = storage_dir
        self.data_file = os.path.join(storage_dir, "research_database.json")
        self.index_file = os.path.join(storage_dir, "research_index.pkl")
        os.makedirs(self.storage_dir, exist_ok=True)
        self.data: List[Dict] = self._load_data()
        self.index: Dict[str, List[int]] = self._load_index()

    # -- 持久化 ---------------------------------------------------------------

    def _load_data(self) -> List[Dict]:
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _load_index(self) -> Dict[str, List[int]]:
        if os.path.exists(self.index_file):
            try:
                with open(self.index_file, "rb") as f:
                    return pickle.load(f)
            except Exception:
                return {}
        return {}

    def _save_data(self) -> None:
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def _save_index(self) -> None:
        with open(self.index_file, "wb") as f:
            pickle.dump(self.index, f)

    # -- 索引构建 -------------------------------------------------------------

    _STOP_WORDS = frozenset(
        ["the", "and", "for", "with", "from", "that", "this"]
    )

    def _build_index(self) -> None:
        index: Dict[str, List[int]] = {}
        for idx, item in enumerate(self.data):
            keywords: set = set()
            for field in ("title", "name", "abstract", "description", "summary"):
                text = item.get(field, "").lower()
                for word in text.split():
                    if len(word) > 3 and word not in self._STOP_WORDS:
                        keywords.add(word)
            for kw in keywords:
                index.setdefault(kw, []).append(idx)
        self.index = index
        self._save_index()

    # -- 查询 -----------------------------------------------------------------

    def search_items(self, query: str) -> List[Dict]:
        if not self.index:
            self._build_index()

        query_words = {
            w for w in query.lower().split()
            if len(w) > 3 and w not in self._STOP_WORDS
        }
        hit_indices: set = set()
        for w in query_words:
            if w in self.index:
                hit_indices.update(self.index[w])

        return [self.data[i] for i in hit_indices if 0 <= i < len(self.data)]


# ---------------------------------------------------------------------------
# 3. 统一入口：构建知识上下文
# ---------------------------------------------------------------------------

def build_knowledge_context(
    query: str,
    paper_content: str = "",
    data_md_path: str = _DEFAULT_DATA_MD_PATH,
    research_store_dir: Optional[str] = None,
    max_data_md_results: int = 5,
    max_store_results: int = 5,
) -> tuple:
    """
    综合 data.md 与 ResearchStore 检索结果，构建知识上下文字符串。

    参数:
        query: 用户需求文本（用于检索关键词）
        paper_content: 上传文件提取的文本（可选，辅助检索）
        data_md_path: data.md 文件路径
        research_store_dir: ResearchStore 存储目录
        max_data_md_results: data.md 最多返回条目数
        max_store_results: ResearchStore 最多返回条目数

    返回:
        (knowledge_context_str, references_list)
        - knowledge_context_str: 用于拼入 prompt 的文本
        - references_list: [{type, title, summary}, ...] 供最终结果使用
    """
    search_query = query
    if paper_content:
        search_query += " " + paper_content[:500]

    references: List[Dict[str, str]] = []
    context_parts: List[str] = []

    # ---- data.md 检索 ----
    md_results = search_data_md(search_query, max_data_md_results, data_md_path)
    if md_results:
        context_parts.append("### 来自知识库的相关论文与模型\n")
        for entry in md_results:
            context_parts.append(entry + "\n")
            _extract_references(entry, references)

    # ---- ResearchStore 检索 ----
    if research_store_dir and os.path.isdir(research_store_dir):
        store = ResearchStore(storage_dir=research_store_dir)
        items = store.search_items(search_query)
        for item in items[:max_store_results]:
            item_type = item.get("type", "paper")
            title = item.get("title", item.get("name", ""))
            summary = item.get("summary", "")[:300]
            context_parts.append(
                f"- [{item_type}] {title}: {summary}\n"
            )
            references.append({
                "type": item_type,
                "title": title,
                "summary": summary,
            })

    return "\n".join(context_parts), references


def _extract_references(entry: str, references: List[Dict[str, str]]) -> None:
    """从 data.md 条目中提取论文/模型信息，追加到 references。"""
    paper_match = re.search(r"(?:\d+\. )?\*\*论文\*\*：(.+)", entry)
    if paper_match:
        title = paper_match.group(1).strip()
        summary = _extract_summary(entry)
        references.append({"type": "paper", "title": title, "summary": summary})

    model_match = re.search(r"\*\*开源模型\*\*：(.+)", entry)
    if model_match:
        title = model_match.group(1).strip()
        summary = _extract_summary(entry)
        references.append({"type": "model", "title": title, "summary": summary})


def _extract_summary(entry: str) -> str:
    marker = "**核心内容**："
    idx = entry.find(marker)
    if idx == -1:
        return ""
    raw = entry[idx + len(marker):].strip()
    return raw[:300] + "..." if len(raw) > 300 else raw
