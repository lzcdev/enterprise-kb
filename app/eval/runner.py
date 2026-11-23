"""RAG 效果评测运行器：把"效果好不好"变成可复现的确定性分数。

核心设计（面试要能讲清为什么）：

1. 为什么不用 RAGAS/ChatGPT 当裁判？
   那些要 API key、结果有随机性、跑一次几毛钱。教学项目需要一个
   【零依赖、可复现、秒级】的评测，才能在每次改动后都跑。
   这里做的是 RAGAS 指标的【确定性近似】，不是替代——注释里标清边界。

2. 测什么？（分两段，对应 RAG 两阶段）
   - 检索段：Recall@k（正确文档是否进 top-k）、MRR（第一次命中排第几）
   - 生成段：Faithfulness 近似（答案是否含标准答案的关键事实）、
             引用命中（溯源是否指向正确文档）、拒答准确率（无据问题是否正确拒答）

3. 为什么阈值设 0.0？
   检索评测要测的是【排序能力】——正确文档排在前面。若用默认 0.3 阈值，
   确定性哈希档的 cosine 普遍低于阈值会被全过滤，测出来全是 0，
   分不清"排序差"还是"被阈值砍了"。所以评测检索时用 0.0 看纯排序，
   阈值的影响在端到端生成评测里自然体现。

4. 可复现性：所有指标纯字符串/集合运算，无随机数、无模型推理，
   同一份数据任何机器跑出来的分数完全一致——这是回归门禁的前提。

生产升级路径：把确定性近似换成 RAGAS 的 LLM-judge 指标 + 真语义 embedding，
评测集不变、接口不变，只换本模块的指标函数。
"""
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.config import settings
from app.factory import active_namespace, get_embedder, get_reranker, get_vectorstore
from app.ingestion.loader_pymupdf import PyMuPDFParser
from app.ingestion.splitter import RecursiveSplitter, section_to_chunks
from app.interfaces import VectorRecord
from app.rag_service import RAGService
from app.retrieval.retriever import Retriever


@dataclass
class CaseResult:
    """单条评测用例的结果。保留明细，退化时能定位到具体哪题坏了。"""
    case_id: str
    question: str
    category: str
    should_answer: bool
    relevant_source: Optional[str]
    retrieved_sources: list[str] = field(default_factory=list)
    answer: str = ""
    cited_sources: list[str] = field(default_factory=list)
    # 指标（0/1 或 0~1）
    hit_at_1: float = 0.0
    hit_at_3: float = 0.0
    hit_at_5: float = 0.0
    reciprocal_rank: float = 0.0
    faithfulness: float = 0.0
    citation_hit: float = 0.0
    abstention_correct: float = 0.0


@dataclass
class EvalReport:
    """评测总报告：聚合指标 + 逐条明细 + 运行环境。"""
    metrics: dict[str, float]
    cases: list[CaseResult]
    config: dict
    elapsed_ms: float = 0.0
    dataset_version: str = ""

    def summary(self) -> str:
        """人类可读的摘要。"""
        lines = ["=" * 68, "RAG 评测报告（确定性近似指标）", "=" * 68]
        for k, v in self.metrics.items():
            lines.append(f"  {k:<26} {v:.4f}")
        lines.append("-" * 68)
        lines.append(f"  用例数 {len(self.cases)} | 耗时 {self.elapsed_ms:.0f}ms "
                     f"| 数据版本 {self.dataset_version}")
        lines.append(f"  环境: TIER={self.config.get('TIER')} "
                     f"EMBED={self.config.get('EMBEDDING_BACKEND')} "
                     f"LLM={self.config.get('LLM_BACKEND')}")
        lines.append("=" * 68)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """可序列化为 JSON（用于保存报告/门禁对比）。"""
        return {
            "metrics": self.metrics,
            "config": self.config,
            "dataset_version": self.dataset_version,
            "elapsed_ms": self.elapsed_ms,
            "cases": [vars(c) for c in self.cases],
        }


def load_golden(path: Optional[str | Path] = None) -> dict:
    """加载评测集 JSON。"""
    p = Path(path) if path else settings.DATA_DIR / "eval" / "golden.json"
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _normalize(text: str) -> str:
    """归一化：去空白/标点，避免"5 分钟"与"5分钟"判为不匹配。"""
    return re.sub(r"[\s，。！？、；：\"'（）()《》]", "", text)


def _fact_in(text: str, fact: str) -> bool:
    """关键事实是否出现在文本里（归一化后子串匹配）。"""
    return _normalize(fact) in _normalize(text)


def build_eval_index(embedder, store, namespace: str, data_dir: Path) -> int:
    """把 data/ 下全部测试文档灌进指定库实例，供评测检索。

    为什么用全部文档而不是单份？评测的 Recall 要能区分"命中的是正确文档还是别的文档"，
    库里只有一份文档时 Recall 恒为 1，测不出检索质量。
    """
    parser = PyMuPDFParser()
    chunks: list = []
    splitter = RecursiveSplitter(chunk_size=150, overlap=20)
    for path in sorted(data_dir.glob("*")):
        if path.suffix.lower() not in (".md", ".txt", ".docx", ".pdf"):
            continue
        for sec in parser.parse(str(path)):
            chunks.extend(section_to_chunks(sec, splitter))

    vectors = embedder.embed([c.text for c in chunks])
    store.create_namespace(namespace, embedder.dimension)
    store.upsert(namespace, [
        VectorRecord(
            id=c.chunk_id, vector=v, text=c.text,
            metadata={
                "embedding_model": embedder.model_id, "tenant_id": "default",
                "doc_id": c.meta.doc_id, "filename": c.meta.filename,
                "page": c.meta.page, "title_path": c.meta.title_path,
            },
        )
        for c, v in zip(chunks, vectors)
    ])
    return len(chunks)


def evaluate(golden_path: Optional[str | Path] = None, *,
             top_k: int = 10) -> EvalReport:
    """跑完整评测：检索段 + 生成段，返回聚合报告和逐条明细。

    Args:
        golden_path: 评测集路径，默认 data/eval/golden.json
        top_k: 检索段考察的召回数量（Recall@k/MRR@k 的上界）
    """
    t0 = time.perf_counter()
    golden = load_golden(golden_path)
    cases = golden["cases"]

    # 建库：memory 档必须同一实例贯穿建库/检索/生成，这里显式共享。
    embedder = get_embedder()
    store = get_vectorstore()
    namespace = active_namespace()
    n_chunks = build_eval_index(embedder, store, namespace, settings.DATA_DIR)

    retriever = Retriever(embedder=embedder, store=store, namespace=namespace)
    service = RAGService(
        retriever=retriever, reranker=get_reranker(), llm=get_llm_safe(),
    )

    results: list[CaseResult] = []
    for case in cases:
        r = CaseResult(
            case_id=case["id"], question=case["question"],
            category=case["category"], should_answer=case["should_answer"],
            relevant_source=case.get("relevant_source"),
        )

        # ---- 检索段：看纯排序能力（阈值 0.0）----
        hits = retriever.retrieve(case["question"], top_k=top_k,
                                  tenant_id="default", score_threshold=0.0)
        r.retrieved_sources = [h.metadata.get("filename", "") for h in hits]
        if case["should_answer"] and case.get("relevant_source"):
            target = case["relevant_source"]
            positions = [i for i, s in enumerate(r.retrieved_sources, 1) if s == target]
            if positions:
                first = positions[0]
                r.hit_at_1 = 1.0 if first <= 1 else 0.0
                r.hit_at_3 = 1.0 if first <= 3 else 0.0
                r.hit_at_5 = 1.0 if first <= 5 else 0.0
                r.reciprocal_rank = 1.0 / first

        # ---- 生成段：端到端答案 + 引用 + 拒答 ----
        # 注意这里【不覆盖阈值】，用租户/全局真实配置（默认 0.3）。
        # 拒答题（无据问题）靠这道闸门正确返回"未找到"——若这里也传 0.0，
        # 哈希档相似度恒 >0，所有无据题都会被硬塞候选，拒答率必然为 0，
        # 测的就不是真实系统行为了。检索段才用 0.0 看纯排序，两段分工不同。
        ans = service.answer(case["question"])
        r.answer = ans.answer
        r.cited_sources = [s.filename for s in ans.sources]
        if case["should_answer"]:
            facts = case.get("key_facts", [])
            if facts:
                found = sum(1 for f in facts if _fact_in(ans.answer, f))
                r.faithfulness = found / len(facts)
            if case.get("relevant_source"):
                r.citation_hit = 1.0 if case["relevant_source"] in r.cited_sources else 0.0
        else:
            # 无据问题：答案里出现拒答表述即判对（确定性关键词判断）
            r.abstention_correct = 1.0 if ("没有相关信息" in ans.answer or "未找到" in ans.answer) else 0.0

        results.append(r)

    metrics = _aggregate(results, top_k)
    config = {
        "TIER": settings.TIER,
        "EMBEDDING_BACKEND": settings.EMBEDDING_BACKEND,
        "VECTORSTORE_BACKEND": settings.VECTORSTORE_BACKEND,
        "RERANK_BACKEND": settings.RERANK_BACKEND,
        "LLM_BACKEND": settings.LLM_BACKEND,
        "indexed_chunks": n_chunks,
    }
    return EvalReport(
        metrics=metrics, cases=results, config=config,
        elapsed_ms=(time.perf_counter() - t0) * 1000,
        dataset_version=golden.get("version", "unknown"),
    )


def get_llm_safe():
    """评测用的 LLM：优先工厂配置；若 api 档缺 key 则降级 mock，保证评测能跑。"""
    from app.factory import get_llm
    try:
        return get_llm()
    except Exception:
        from app.generation.generator import MockLLM
        return MockLLM()


def _aggregate(results: list[CaseResult], top_k: int) -> dict[str, float]:
    """聚合逐条指标为总体分数。分母只取对应子集，避免"无据题"污染检索指标。"""
    answerable = [r for r in results if r.should_answer and r.relevant_source]
    abstain = [r for r in results if not r.should_answer]

    def avg(seq, attr):
        return sum(getattr(r, attr) for r in seq) / len(seq) if seq else 0.0

    return {
        f"recall@{1}": avg(answerable, "hit_at_1"),
        f"recall@{3}": avg(answerable, "hit_at_3"),
        f"recall@{5}": avg(answerable, "hit_at_5"),
        f"mrr@{top_k}": avg(answerable, "reciprocal_rank"),
        "faithfulness": avg(answerable, "faithfulness"),
        "citation_hit_rate": avg(answerable, "citation_hit"),
        "abstention_accuracy": avg(abstain, "abstention_correct"),
    }
