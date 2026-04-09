"""基于 Embedding 的向量检索器。

使用 litellm.aembedding() 做文本向量化，numpy 余弦相似度搜索。
支持从目录批量加载 Markdown 文档并按段落分块。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import numpy as np
from loguru import logger

from core.rag.base import Document, Retriever

_DEFAULT_MODEL = "text-embedding-3-small"


class EmbeddingRetriever(Retriever):

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.model = model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.api_key = api_key
        self.base_url = base_url
        self._docs: list[Document] = []
        self._vectors: Optional[np.ndarray] = None

    async def add(self, content: str, source: str = "", **metadata: Any) -> None:
        chunks = self._split_text(content, source, metadata)
        if not chunks:
            return
        texts = [c.content for c in chunks]
        vecs = await self._embed_batch(texts)
        if vecs is None:
            return
        self._docs.extend(chunks)
        if self._vectors is None:
            self._vectors = vecs
        else:
            self._vectors = np.vstack([self._vectors, vecs])
        logger.debug(f"已索引 {len(chunks)} 个片段 (来源: {source}), 总计 {len(self._docs)}")

    async def load_directory(self, directory: Path) -> int:
        """从目录加载所有 .md 文件并索引。返回加载的文档数。"""
        if not directory.exists():
            logger.warning(f"知识库目录不存在: {directory}")
            return 0
        count = 0
        for fp in sorted(directory.glob("*.md")):
            text = fp.read_text(encoding="utf-8").strip()
            if text:
                await self.add(text, source=fp.name)
                count += 1
        logger.info(f"知识库已加载: {directory} ({count} 个文档, {len(self._docs)} 个片段)")
        return count

    async def retrieve(self, query: str, top_k: int = 5) -> list[Document]:
        if not self._docs or self._vectors is None:
            return []
        q_vec = await self._embed_batch([query])
        if q_vec is None:
            return []
        scores = self._cosine_similarity(q_vec[0], self._vectors)
        top_indices = np.argsort(scores)[::-1][:top_k]
        results = []
        for idx in top_indices:
            score = float(scores[idx])
            if score < 0.1:
                break
            doc = self._docs[idx]
            results.append(Document(
                content=doc.content, source=doc.source,
                score=score, metadata=doc.metadata,
            ))
        return results

    def _split_text(self, text: str, source: str, metadata: dict) -> list[Document]:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks: list[Document] = []
        current = ""
        for para in paragraphs:
            if len(current) + len(para) + 2 > self.chunk_size and current:
                chunks.append(Document(content=current, source=source, metadata=dict(metadata)))
                overlap_text = current[-self.chunk_overlap:] if self.chunk_overlap else ""
                current = overlap_text + "\n\n" + para if overlap_text else para
            else:
                current = current + "\n\n" + para if current else para
        if current:
            chunks.append(Document(content=current, source=source, metadata=dict(metadata)))
        return chunks

    async def _embed_batch(self, texts: list[str]) -> Optional[np.ndarray]:
        import litellm
        try:
            params: dict[str, Any] = {"model": self.model, "input": texts}
            if self.api_key:
                params["api_key"] = self.api_key
            if self.base_url:
                params["api_base"] = self.base_url
            resp = await litellm.aembedding(**params)
            vecs = [item["embedding"] for item in resp.data]
            return np.array(vecs, dtype=np.float32)
        except Exception as e:
            logger.error(f"Embedding 调用失败 [{self.model}]: {e}")
            return None

    @staticmethod
    def _cosine_similarity(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
        query_norm = query / (np.linalg.norm(query) + 1e-10)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10
        matrix_norm = matrix / norms
        return matrix_norm @ query_norm
