"""检索层（粗召回）：把 Embedder 和 VectorStore 编排成一句话的检索能力。

职责边界（面试要能讲清）：
- Retriever 只做【召回】：query 向量化 -> 向量库相似搜索 -> 阈值过滤。
- 它不做精排（那是 Reranker 的事），也不生成答案（那是 Generator 的事）。
  分层清晰，每一层都能单独替换/测试。

为什么要阈值过滤（score_threshold）？
向量库总会返回 top_k 条，哪怕全不相关（余弦也可能有 0.3 的分数）。
若不设阈值，用户问"公司团建去哪"也会硬塞两条退款政策给 LLM，
导致模型基于无关内容编答案（幻觉）。低于阈值直接判"没检索到"，是防幻觉第一道闸。
"""
from typing import Optional

from app.factory import active_namespace, get_embedder, get_vectorstore
from app.config import settings
from app.interfaces import Embedder, ScoredChunk, VectorStore


class Retriever:
    """组合 embedder 与 vectorstore 的检索器。"""

    def __init__(
        self,
        embedder: Optional[Embedder] = None,
        store: Optional[VectorStore] = None,
        namespace: Optional[str] = None,
        *,
        bind_embedder_to_namespace: bool = False,
    ):
        # 允许依赖注入：测试时可传 mock，生产用工厂默认实现
        # bind_embedder_to_namespace=True 时，按 namespace 编码的模型版本自动选
        # embedder——蓝绿/灰度迁移必备（蓝用 A 模型、绿用 B 模型，查询必须各用各的）。
        # 默认 False 保持向后兼容：显式传 embedder 或走全局默认，现有调用不受影响。
        if embedder is not None:
            self.embedder = embedder
        elif bind_embedder_to_namespace:
            from app.factory import embedder_for_namespace
            self.embedder = embedder_for_namespace(namespace or active_namespace())
        else:
            self.embedder = get_embedder()
        self.store = store or get_vectorstore()
        self.namespace = namespace or active_namespace()

    def retrieve(
        self,
        query: str,
        *,
        top_k: Optional[int] = None,
        tenant_id: Optional[str] = None,
        score_threshold: Optional[float] = None,
    ) -> list[ScoredChunk]:
        """单条 query 的粗召回。

        Args:
            query: 用户原始问题。
            top_k: 召回条数，None 用配置 TOP_K（默认 10）。
            tenant_id: 多租户隔离；传入则作为 metadata 过滤条件。
            score_threshold: 低于此分丢弃；None 用配置 SCORE_THRESHOLD。
        Returns:
            按余弦相似度降序的候选列表，已过滤低分。
        """
        k = top_k if top_k is not None else settings.TOP_K
        threshold = (score_threshold if score_threshold is not None
                     else settings.SCORE_THRESHOLD)

        # query 必须走和 passage 同一个 embedder，并标记 is_query（Qwen3 会加指令前缀）
        q_vec = self.embedder.embed([query], is_query=True)[0]
        filters = {"tenant_id": tenant_id} if tenant_id else None
        candidates = self.store.search(self.namespace, q_vec, top_k=k, filters=filters)

        # 防幻觉闸门：分数低于阈值的结果直接丢弃，宁可少答也不乱答
        return [c for c in candidates if c.score >= threshold]

    def retrieve_batch(self, queries: list[str], **kwargs) -> list[list[ScoredChunk]]:
        """批量检索：评估脚本常用，逐条调用 retrieve 保持逻辑一致。"""
        return [self.retrieve(q, **kwargs) for q in queries]
