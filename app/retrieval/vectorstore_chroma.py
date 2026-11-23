"""Chroma 向量库（本地生产档）：持久化 + metadata 过滤 + 多租户隔离。

namespace 用 Chroma 的 collection 实现：不同 embedding 版本的索引是不同 collection，
蓝绿迁移时建新 collection、验证、切流、删旧 collection，全程可回滚。

优雅降级：没装 chromadb 时导入不报错，调用才提示。
"""
from typing import Optional

from app.interfaces import VectorStore, VectorRecord, ScoredChunk


class ChromaStore:
    name = "chroma"

    def __init__(self, persist_dir: str = "data/chroma"):
        self.persist_dir = persist_dir
        self._client = None

    def _load(self):
        if self._client is None:
            try:
                import chromadb
            except ImportError as exc:
                raise RuntimeError(
                    "Chroma 需要 chromadb：pip install chromadb。"
                    "原型档可改 VECTORSTORE_BACKEND=memory 无需依赖跑通链路。"
                ) from exc
            self._client = chromadb.PersistentClient(path=self.persist_dir)
        return self._client

    def create_namespace(self, namespace: str, dimension: int) -> None:
        _ = dimension  # Chroma 自动推断维度，无需预声明
        self._load().get_or_create_collection(name=namespace)

    def upsert(self, namespace: str, records: list[VectorRecord]) -> None:
        if not records:
            return
        collection = self._load().get_or_create_collection(name=namespace)
        # metadata 里必须带 embedding_model 版本（构造记录时已写入）
        collection.upsert(
            ids=[r.id for r in records],
            embeddings=[r.vector for r in records],
            documents=[r.text for r in records],
            metadatas=[r.metadata for r in records],
        )

    def search(
        self,
        namespace: str,
        query_vector: list[float],
        top_k: int,
        filters: Optional[dict] = None,
    ) -> list[ScoredChunk]:
        collection = self._load().get_collection(name=namespace)
        # Chroma 的 where 过滤用 {key: value} 精确匹配，正好做 tenant_id 隔离
        kwargs = {"query_embeddings": [query_vector], "n_results": top_k}
        if filters:
            kwargs["where"] = filters
        raw = collection.query(**kwargs)
        if not raw["ids"] or not raw["ids"][0]:
            return []
        results = []
        for i, rec_id in enumerate(raw["ids"][0]):
            results.append(ScoredChunk(
                id=rec_id,
                text=raw["documents"][0][i],
                # Chroma 返回距离，转成相似度：1 - 距离（余弦距离场景）
                score=1.0 - raw["distances"][0][i],
                metadata=raw["metadatas"][0][i] or {},
            ))
        return results

    def count(self, namespace: str) -> int:
        return self._load().get_collection(name=namespace).count()

    def delete_namespace(self, namespace: str) -> None:
        self._load().delete_collection(name=namespace)

    def list_namespaces(self) -> list[str]:
        return [c.name for c in self._load().list_collections()]
