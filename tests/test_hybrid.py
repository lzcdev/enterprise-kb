"""波次0：BM25 混合检索测试。"""
from app.interfaces import ScoredChunk
from app.retrieval.hybrid import BM25Index, HybridRetriever, rrf_fuse, tokenize


def _docs():
    return [
        ScoredChunk(id="d1", text="XJ-200 型吸尘器保修期两年", score=0.5,
                    metadata={"tenant_id": "default"}),
        ScoredChunk(id="d2", text="退款需七个工作日到账", score=0.4,
                    metadata={"tenant_id": "default"}),
        ScoredChunk(id="d3", text="XL-500 扫地机器人质保一年", score=0.3,
                    metadata={"tenant_id": "other"}),
    ]


def test_tokenize_keeps_exact_strings():
    tokens = tokenize("XJ-200 退款")
    assert "xj" in tokens and "200" in tokens
    assert "退款" in tokens


def test_bm25_exact_model_ranks_first():
    """精确型号串查询：BM25 把含 XJ-200 的文档排第一。"""
    index = BM25Index()
    index.build(_docs())
    results = index.search("XJ-200 保修", top_k=3)
    assert results[0].id == "d1"


def test_bm25_relevance_order():
    """退款查询：含退款的文档排第一。"""
    index = BM25Index()
    index.build(_docs())
    results = index.search("退款到账", top_k=3)
    assert results[0].id == "d2"


def test_rrf_fuses_rankings():
    """RRF：两路都靠前的文档融合后仍靠前。"""
    a = [ScoredChunk(id="x", text="x", score=1, metadata={}),
         ScoredChunk(id="y", text="y", score=0.9, metadata={})]
    b = [ScoredChunk(id="x", text="x", score=2, metadata={}),
         ScoredChunk(id="z", text="z", score=1.5, metadata={})]
    fused = rrf_fuse([a, b])
    assert fused[0].id == "x"  # 两路都第一
    assert "rrf_score" in fused[0].metadata
    assert "rrf_ranks" in fused[0].metadata


def test_hybrid_retriever_tenant_filter():
    """混合检索：BM25 路按 tenant 过滤，跨租户文档不泄漏。"""
    class FakeRetriever:
        def retrieve(self, q, **kwargs):
            return [ScoredChunk(id="d1", text="XJ-200 保修", score=0.8,
                                metadata={"tenant_id": "default"})]

    hybrid = HybridRetriever(FakeRetriever())
    hybrid.build_bm25(_docs())
    results = hybrid.retrieve("XJ-200", top_k=3, tenant_id="default")
    assert all(c.metadata.get("tenant_id") == "default" for c in results)
    assert "d3" not in [c.id for c in results]
