"""混合检索：BM25 关键词检索 + 向量检索，用 RRF 融合。

为什么生产 RAG 必须做混合检索？（面试高频）
    向量检索是"有损压缩"——把一段文本压成一个向量，词与词的精确对应关系丢了。
    用户搜"型号 XJ-200 的保修期"或"订单号 20241123"这类**精确串**时，
    向量检索常常召回一堆"语义相近但没这个编号"的文档。
    BM25 是关键词检索的经典算法，恰好擅长精确匹配。两者互补，生产标配。

BM25 原理（能自己推导）：
    score(D,Q) = Σ IDF(qi) · (f(qi,D)·(k1+1)) / (f(qi,D) + k1·(1-b+b·|D|/avgdl))
    - f(qi,D)：词 qi 在文档 D 里的词频，越高越相关，但收益递减（k1 控制饱和）
    - IDF(qi) = ln(1 + (N - df + 0.5)/(df + 0.5))：稀有词权重高，常见词权重低
    - 长度归一化：文档越长越容易命中词，用 b 和 |D|/avgdl 惩罚长文档
    直觉：一个词越稀有、在文档里出现越集中、文档越短，越能说明这篇文档相关。

中文分词问题：
    标准 BM25 要先把文本切成词。装 jieba 最简单，但为保持零依赖，
    这里用**字符 bigram**（相邻两字）当"词"——"退款政策"→["退款","款政","政策"]。
    对中文这种黏着语言效果不差，且完全不依赖分词库。生产建议换 jieba/IK。

RRF 融合（Reciprocal Rank Fusion）：
    两路检索的分数**量纲不同**（BM25 可正可负无上界，cosine 在 -1~1），
    绝对不能相加或直接比较。RRF 只看**排名**：
        score(d) = Σ 1 / (k + rank_i(d))，k 常取 60
    某文档在两路都排前面，得分就高；只在一路靠前的得分低。稳健且无需调权重。
"""
import math
import re
from collections import Counter
from typing import Optional

from app.interfaces import ScoredChunk


def tokenize(text: str) -> list[str]:
    """中文友好的 tokenize：连续英文/数字整体保留，中文按字符 bigram。

    "XJ-200退款" → ["xj", "200", "退", "退款", "款"] 之类。
    精确串（型号、订单号）作为整体 token，是 BM25 能救回向量检索的关键。
    """
    text = text.lower()
    tokens = []
    for seg in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text):
        if re.match(r"^[a-z0-9]+$", seg):
            tokens.append(seg)
    # 中文单字流 → bigram
    han = re.findall(r"[\u4e00-\u9fff]+", text)
    for run in han:
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


class BM25Index:
    """纯 Python BM25 索引。build 一次，多次 query。

    生产直接用 Elasticsearch / Meilisearch，它们内置 BM25 且支持分布式。
    这里手写是为了讲清原理 + 原型档零依赖验证混合检索的控制流。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs: list[ScoredChunk] = []
        self.doc_tf: list[Counter] = []
        self.doc_len: list[int] = []
        self.avgdl = 0.0
        self.df: Counter = Counter()
        self._built = False

    def build(self, docs: list[ScoredChunk]) -> None:
        """建索引：统计每篇词频、文档长度、每个词的文档频率 df。"""
        self.docs = docs
        self.doc_tf = []
        self.doc_len = []
        self.df = Counter()
        for doc in docs:
            tf = Counter(tokenize(doc.text))
            self.doc_tf.append(tf)
            self.doc_len.append(sum(tf.values()))
            for term in tf:
                self.df[term] += 1
        self.avgdl = sum(self.doc_len) / len(docs) if docs else 0.0
        self._built = True

    def _idf(self, term: str) -> float:
        """IDF：稀有词权重高。+0.5 平滑避免除零。"""
        n = len(self.docs)
        df = self.df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 10) -> list[ScoredChunk]:
        """按 BM25 分数降序返回 Top-K。"""
        if not self._built or not self.docs:
            return []
        q_terms = tokenize(query)
        scores = []
        for i, doc in enumerate(self.docs):
            tf = self.doc_tf[i]
            dl = self.doc_len[i]
            score = 0.0
            for term in set(q_terms):          # 同一词在 query 里重复不重复计分
                f = tf.get(term, 0)
                if f == 0:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * dl / self.avgdl) if self.avgdl else f + self.k1
                score += self._idf(term) * (f * (self.k1 + 1)) / denom
            scores.append((score, doc))
        scores.sort(key=lambda x: x[0], reverse=True)
        top = [(s, d) for s, d in scores if s > 0][:top_k]
        # 用 BM25 分数覆盖 ScoredChunk.score（RRF 只看排名，分数仅展示用）
        return [ScoredChunk(id=d.id, text=d.text, score=s, metadata=d.metadata)
                for s, d in top]


def rrf_fuse(rankings: list[list[ScoredChunk]], *, k: int = 60,
             top_n: Optional[int] = None) -> list[ScoredChunk]:
    """RRF 融合多路排名。

    rankings: 多路检索结果，每路已按各自相关性降序。
    返回按 RRF 分数降序的融合列表，metadata 里带 rrf_score 和各路 rank 便于排障。
    """
    fused: dict[str, ScoredChunk] = {}   # id -> 合并后的 chunk
    scores: dict[str, float] = {}
    ranks: dict[str, list[int]] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, 1):
            scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(chunk.id, []).append(rank)
            if chunk.id not in fused:
                fused[chunk.id] = chunk
    ordered = sorted(fused, key=lambda cid: scores[cid], reverse=True)
    if top_n is not None:
        ordered = ordered[:top_n]
    result = []
    for cid in ordered:
        c = fused[cid]
        md = {**c.metadata, "rrf_score": round(scores[cid], 6), "rrf_ranks": ranks[cid]}
        result.append(ScoredChunk(id=c.id, text=c.text, score=scores[cid], metadata=md))
    return result


class HybridRetriever:
    """把向量检索和 BM25 组合起来，对外仍是 retrieve() 一个入口。

    设计要点：BM25 索引在 build() 时一次性建好（离线），query 时只做打分。
    生产里 BM25 索引由 ES 维护，这里内存版仅验证控制流。
    """

    def __init__(self, retriever, *, bm25_k1: float = 1.5, bm25_b: float = 0.75):
        self.retriever = retriever
        self.bm25 = BM25Index(k1=bm25_k1, b=bm25_b)

    def build_bm25(self, documents: list[ScoredChunk]) -> None:
        """用向量库里的全部文档建 BM25 索引（离线步骤）。"""
        self.bm25.build(documents)

    def retrieve(self, query: str, *, top_k: int = 10,
                 tenant_id: Optional[str] = None,
                 score_threshold: Optional[float] = None,
                 vector_k: int = 20, bm25_k: int = 20) -> list[ScoredChunk]:
        """两路召回 → RRF 融合 → 取 top_k。

        注意阈值过滤：只在向量路做（cosine 有明确阈值），BM25 路分数无量纲阈值，
        靠 RRF 排名自然抑制弱相关结果。
        """
        vec = self.retriever.retrieve(query, top_k=vector_k, tenant_id=tenant_id,
                                      score_threshold=score_threshold)
        bm25 = self.bm25.search(query, top_k=bm25_k)
        # 两路都带 tenant 过滤：BM25 索引里只放了对应租户文档（build 时按租户建）
        if tenant_id:
            bm25 = [c for c in bm25 if c.metadata.get("tenant_id") == tenant_id]
        return rrf_fuse([vec, bm25], top_n=top_k)
