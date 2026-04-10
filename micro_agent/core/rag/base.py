"""RAG 检索接口：定义统一的文档检索抽象。

实现方式可以是：
- SimpleRetriever: 基于关键词的内存检索（开发/测试用）
- FAISSRetriever: 基于 FAISS 的向量检索
- ChromaRetriever: 基于 ChromaDB 的向量检索
- GraphRetriever: 基于知识图谱的检索

Agent 通过 Retriever 获取相关文档，自动注入到 prompt 中增强上下文。

用法：
    retriever = SimpleRetriever()
    retriever.add("Python 的 GIL 限制了多线程的并行性", source="python_faq.md")

    agent = Agent(llm=llm, retriever=retriever)
    # Agent._think() 会自动检索并注入相关文档
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Document:
    """检索结果文档。"""

    content: str
    source: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class Retriever(ABC):
    """检索器抽象接口。"""

    @abstractmethod
    async def retrieve(self, query: str, top_k: int = 5) -> list[Document]:
        """根据查询检索相关文档。"""

    async def add(self, content: str, source: str = "", **metadata: Any) -> None:
        """添加文档到索引（可选实现）。"""

    async def add_batch(self, documents: list[Document]) -> None:
        """批量添加文档（可选实现）。"""
        for doc in documents:
            await self.add(doc.content, doc.source, **doc.metadata)


class SimpleRetriever(Retriever):
    """基于关键词匹配的简单检索器。开发/测试用。"""

    def __init__(self) -> None:
        self._docs: list[Document] = []

    async def retrieve(self, query: str, top_k: int = 5) -> list[Document]:
        query_lower = query.lower()
        scored: list[tuple[float, Document]] = []
        for doc in self._docs:
            words = query_lower.split()
            hits = sum(1 for w in words if w in doc.content.lower())
            if hits > 0:
                score = hits / max(len(words), 1)
                scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            Document(content=d.content, source=d.source, score=s, metadata=d.metadata)
            for s, d in scored[:top_k]
        ]

    async def add(self, content: str, source: str = "", **metadata: Any) -> None:
        self._docs.append(Document(content=content, source=source, metadata=metadata))
