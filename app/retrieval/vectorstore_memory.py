"""纯 Python 内存向量库：无需任何依赖，用于跑通与测试检索链路。

它实现了 VectorStore 协议的全部方法（namespace 隔离 / metadata 过滤 / 余弦检索）。
生产档换 ChromaStore/MilvusStore，业务代码（retriever）不用改——这就是抽象的价值。

注意：内存实现每次进程重启数据就丢，只适合演示和测试，绝不能用于生产。
"""
import math
from collections import defaultdict
from typing import Optional

from app.interfaces import VectorStore, VectorRecord, ScoredChunk


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。向量已归一化时等价于内积，但这里不假设已归一化，故完整计算。"""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


class InMemoryStore:
    """namespace -> {record_id: VectorRecord} 的两层字典结构。"""

    name = "memory"

    def __init__(self):
        # 每个 namespace 是独立索引 —— 换 embedding 模型时建新 namespace 全量重建
        self._namespaces: dict[str, dict[str, VectorRecord]] = defaultdict(dict)

    def create_namespace(self, namespace: str, dimension: int) -> None:
        # 内存实现无需预声明维度，创建空字典即可；dimension 参数保留以对齐协议
        _ = dimension
        self._namespaces.setdefault(namespace, {})

    def upsert(self, namespace: str, records: list[VectorRecord]) -> None:
        # 幂等写入：相同 id 覆盖，方便重复建索引时不会产生重复数据
        bucket = self._namespaces[namespace]
        for rec in records:
            bucket[rec.id] = rec

    def search(
        self,
        namespace: str,
        query_vector: list[float],
        top_k: int,
        filters: Optional[dict] = None,
    ) -> list[ScoredChunk]:
        """暴力检索：算余弦 -> 过滤 -> 排序 -> 取 top_k。

        生产向量库用 ANN（HNSW/IVF）加速，这里 O(n) 暴力扫描只为正确性和可读性。
        """
        bucket = self._namespaces.get(namespace, {})
        results: list[ScoredChunk] = []
        for rec in bucket.values():
            # metadata 过滤：所有 filter 键值都要精确匹配 —— 多租户隔离靠 tenant_id
            if filters and any(rec.metadata.get(k) != v for k, v in filters.items()):
                continue
            score = _cosine(query_vector, rec.vector)
            results.append(ScoredChunk(
                id=rec.id, text=rec.text, score=score, metadata=rec.metadata,
            ))
        results.sort(key=lambda c: c.score, reverse=True)
        return results[:top_k]

    def count(self, namespace: str) -> int:
        return len(self._namespaces.get(namespace, {}))

    def delete_namespace(self, namespace: str) -> None:
        self._namespaces.pop(namespace, None)

    def list_namespaces(self) -> list[str]:
        return sorted(self._namespaces.keys())
