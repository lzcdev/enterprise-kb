"""Milvus 向量库（企业档骨架）：面向海量向量 + 分布式 + 多种索引。

和 Chroma 满足同一 VectorStore 协议，工厂按 VECTORSTORE_BACKEND 切换。
企业档相比本地档的收益：
- 分布式、水平扩展，支持亿级向量
- 多种 ANN 索引（HNSW/IVF_PQ）可按数据量/精度/内存权衡
- partition_key 可做租户级物理分区，隔离更彻底

本文件是骨架：真实连接/建集合逻辑留 TODO，接入时按注释实现即可，
业务代码（retriever）不用改。
"""
from typing import Optional

from app.interfaces import VectorStore, VectorRecord, ScoredChunk


class MilvusStore:
    name = "milvus"

    def __init__(self, uri: str = "http://localhost:19530", token: str = ""):
        self.uri = uri
        self.token = token
        self._client = None

    def _load(self):
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:
                raise RuntimeError(
                    "Milvus 需要 pymilvus：pip install pymilvus。"
                    "本地验证可改 VECTORSTORE_BACKEND=memory/chroma。"
                ) from exc
            self._client = MilvusClient(uri=self.uri, token=self.token)
        return self._client

    def create_namespace(self, namespace: str, dimension: int) -> None:
        # TODO 真实实现：client.create_collection(collection_name=namespace, dim=dimension,
        #      metric_type="COSINE", partition_key_field="tenant_id" 做租户物理分区)
        raise RuntimeError("Milvus create_namespace 未接入，见 TODO 注释")

    def upsert(self, namespace: str, records: list[VectorRecord]) -> None:
        # TODO 真实实现：client.insert(collection_name=namespace, data=[...])，
        #      字段含 id/vector/text + metadata 展平（tenant_id/embedding_model 等）
        raise RuntimeError("Milvus upsert 未接入，见 TODO 注释")

    def search(
        self,
        namespace: str,
        query_vector: list[float],
        top_k: int,
        filters: Optional[dict] = None,
    ) -> list[ScoredChunk]:
        # TODO 真实实现：client.search(..., filter="tenant_id == 'xxx'", limit=top_k)
        raise RuntimeError("Milvus search 未接入，见 TODO 注释")

    def count(self, namespace: str) -> int:
        # TODO 真实实现：client.get_collection_stats(namespace)["row_count"]
        raise RuntimeError("Milvus count 未接入，见 TODO 注释")

    def delete_namespace(self, namespace: str) -> None:
        # TODO 真实实现：client.drop_collection(collection_name=namespace)
        raise RuntimeError("Milvus delete_namespace 未接入，见 TODO 注释")

    def list_namespaces(self) -> list[str]:
        # TODO 真实实现：client.list_collections()
        raise RuntimeError("Milvus list_namespaces 未接入，见 TODO 注释")
