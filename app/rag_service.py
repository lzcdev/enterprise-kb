"""RAG 服务编排层：把检索 -> 重排 -> 生成串成一条端到端流水线。

为什么要单独一层？
- API 层只调一个 answer()，不关心内部有几个模型、什么顺序 —— 换实现不影响接口。
- 每个环节（Retriever/Reranker/Generator）都可独立替换、独立测试。
- 端到端耗时统计、空结果短路这些"横切逻辑"集中在这里，不散落到各处。

这就是简历里能写的"RAG 全链路编排 + 组件可拔插"。
"""
import time
from typing import Optional

from app.config import settings
from app.factory import get_llm, get_reranker, get_retriever
from app.generation.generator import build_prompt, parse_citations
from app.interfaces import LLM, Reranker, ScoredChunk
from app.models import Answer
from app.retrieval.retriever import Retriever


# 哨兵：区分"没传这个参数"（用工厂造）和"显式传 False"（关闭该层）。
# 若直接用 None 做默认值，就无法表达"关闭 rerank"——因为 None 同时也是工厂可能返回的值。
_UNSET = object()


class RAGService:
    """端到端 RAG 服务。依赖可注入，便于测试。"""

    def __init__(self, retriever=_UNSET, reranker=_UNSET, llm=_UNSET):
        self.retriever = get_retriever() if retriever is _UNSET else retriever
        self.reranker = get_reranker() if reranker is _UNSET else (None if reranker is False else reranker)
        self.llm = get_llm() if llm is _UNSET else (None if llm is False else llm)

    def answer(self, question: str, *, tenant_id: str = "default",
               top_k: Optional[int] = None, top_n: Optional[int] = None,
               score_threshold: Optional[float] = None) -> Answer:
        """回答一个问题，返回答案 + 引用来源 + 耗时。

        流程：粗召回(top_k) -> 精排取 top_n -> 拼prompt -> LLM生成 -> 解析引用。
        关键短路：候选为空直接返回"未找到"，不调用 LLM（防幻觉）。
        """
        t0 = time.perf_counter()
        n = top_n if top_n is not None else settings.TOP_N

        # ① 粗召回（已含阈值过滤）
        candidates = self.retriever.retrieve(
            question, top_k=top_k, tenant_id=tenant_id,
            score_threshold=score_threshold,
        )

        # ② 精排（可关闭：reranker 为 None 时直接用粗召回顺序）
        if self.reranker is not None:
            candidates = self.reranker.rerank(question, candidates, top_n=n)
        else:
            candidates = candidates[:n]

        # 闸门：没有证据就不调 LLM，直接返回 —— 空上下文会逼 LLM 编造
        if not candidates:
            return Answer(
                answer="知识库中没有相关信息，无法回答该问题。",
                sources=[], question=question,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        # ③ 拼 prompt + ④ 生成
        prompt = build_prompt(question, candidates)
        raw = self.llm.generate(prompt)

        # ⑤ 引用解析 -> Source 溯源
        sources = parse_citations(raw, candidates)
        return Answer(
            answer=raw, sources=sources, question=question,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )
