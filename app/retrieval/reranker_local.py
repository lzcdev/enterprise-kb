"""重排层（精排）：对粗召回的候选做精细相关性排序。

两个实现，都满足 Reranker 协议：
1. LocalReranker（本地生产档）—— Qwen3-Reranker 交叉编码器，
   把 (query, doc) 拼接输入模型，逐对输出相关性分数。需要 torch/transformers。
2. DeterministicReranker（原型档）—— 零依赖的关键词重叠启发式，
   只用于验证"重排会改变候选顺序"这条链路，**没有语义理解**，
   绝不能用来评估检索质量。生产必须换 LocalReranker。

交叉编码器 vs 双塔（面试必背）：
- 双塔 Bi-Encoder：query/doc 各自独立编码成向量，doc 可预计算，快，但丢失词级交互。
- 交叉编码 Cross-Encoder：query 和 doc 拼接后一起过模型，注意力能看到两者交互，
  准，但每个 (query,doc) 对都要单独前向，慢，所以只对 Top10~50 做。
- 分数不同量纲：cross-encoder 输出（Qwen3-Reranker 是可正可负的 logit 差，
  实测相关 7.17 / 无关 -10.36）和 cosine 的 -1~1 不能混排，
  必须重排后重新取 top_n。绝不能把 rerank 分数和召回分数直接比较。
"""
import re
from typing import Optional

from app.interfaces import Reranker, ScoredChunk


class LocalReranker:
    """Qwen3-Reranker 交叉编码器实现。懒加载，优雅降级。"""

    def __init__(self, model_name: str = "Qwen/Qwen3-Reranker-0.6B"):
        self.model_name = model_name
        self.model_id = model_name.replace("/", "-").lower()
        self._tokenizer = None
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise RuntimeError(
                    "本地重排需要 sentence-transformers + torch："
                    "pip install sentence-transformers torch。"
                    "原型档可改 RERANK_BACKEND=deterministic 零依赖跑通链路。"
                ) from exc
            # 关键：必须用 CrossEncoder 而不是 AutoModelForSequenceClassification。
            # Qwen3-Reranker 的架构是 Qwen3ForCausalLM（生成式），没有分类头；
            # 若用 AutoModelForSequenceClassification 加载，会随机初始化一个头，
            # 打分全是噪音。CrossEncoder 内置了 Qwen3-Reranker 专用的 chat template
            # 和"取 yes token logit"的正确逻辑（实测相关 7.17 / 无关 -10.36）。
            self._model = CrossEncoder(self.model_name)
        return self._model

    def rerank(self, query: str, candidates: list[ScoredChunk], top_n: int) -> list[ScoredChunk]:
        if not candidates:
            return []
        model = self._load()

        # (query, doc) 逐对送进交叉编码器，模型能看到词级交互
        texts = [(query, c.text) for c in candidates]
        scores = model.predict(texts)
        # predict 单条返回标量、多条返回数组，统一成 list
        if hasattr(scores, "tolist"):
            scores = scores.tolist()
        if isinstance(scores, (int, float)):
            scores = [scores]

        reranked = []
        for cand, score in zip(candidates, scores):
            reranked.append(ScoredChunk(
                id=cand.id, text=cand.text, score=float(score), metadata=cand.metadata,
            ))
        # 用新分数重新排序再取 top_n —— 不能沿用粗召回的 cosine 顺序
        reranked.sort(key=lambda c: c.score, reverse=True)
        return reranked[:top_n]


class DeterministicReranker:
    """零依赖重排器：中文/英文按 2-gram 重叠率打分，仅用于验证链路。

    诚实声明：它不理解语义，"退款几天到账" 与 "3-7 个工作日" 不会匹配。
    它的价值是——在不装 torch 的环境里也能跑通"召回->重排->截断"的完整控制流，
    让学习者看到 rerank 如何改变候选顺序。评估质量请用 LocalReranker。
    """

    model_id = "deterministic-rerank-v1"

    def __init__(self, ngram: int = 2):
        self.ngram = ngram

    @staticmethod
    def _normalize(text: str) -> str:
        # 只保留中英文数字，去掉标点空格，避免标点干扰重叠统计
        return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text.lower()))

    def _ngrams(self, text: str) -> set[str]:
        s = self._normalize(text)
        if len(s) < self.ngram:
            return {s} if s else set()
        return {s[i:i + self.ngram] for i in range(len(s) - self.ngram + 1)}

    def rerank(self, query: str, candidates: list[ScoredChunk], top_n: int) -> list[ScoredChunk]:
        if not candidates:
            return []
        q_grams = self._ngrams(query)
        reranked = []
        for c in candidates:
            d_grams = self._ngrams(c.text)
            # Jaccard 相似度：交集/并集，作为"重排分数"（0~1）
            overlap = len(q_grams & d_grams)
            score = overlap / len(q_grams | d_grams) if (q_grams | d_grams) else 0.0
            reranked.append(ScoredChunk(
                id=c.id, text=c.text, score=round(score, 4), metadata=c.metadata,
            ))
        reranked.sort(key=lambda c: c.score, reverse=True)
        return reranked[:top_n]


def get_reranker(backend: str = "deterministic",
                 model_name: str = "Qwen/Qwen3-Reranker-0.6B") -> Optional[Reranker]:
    """按 backend 选重排实现。工厂集中选择，业务代码不写 if。"""
    if backend == "local":
        return LocalReranker(model_name)
    if backend == "deterministic":
        return DeterministicReranker()
    if backend == "off":
        return None
    raise ValueError(f"未知的 RERANK_BACKEND: {backend}")
