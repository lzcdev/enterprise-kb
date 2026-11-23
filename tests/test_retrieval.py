"""任务4 单元测试：Retriever 召回 + Reranker 重排。"""
from app.factory import get_embedder, get_vectorstore
from app.interfaces import ScoredChunk
from app.retrieval.retriever import Retriever
from app.retrieval.reranker_local import DeterministicReranker, LocalReranker


def make_candidates() -> list[ScoredChunk]:
    """手工构造候选：cosine 顺序与关键词重叠顺序故意相反，验证重排会改变顺序。"""
    return [
        ScoredChunk(id="a", text="商品颜色和尺寸", score=0.9, metadata={}),
        ScoredChunk(id="b", text="退款到账时间说明", score=0.5, metadata={}),
    ]


def test_retriever_recall_and_threshold():
    """Retriever 走通粗召回，且低分结果被阈值过滤。"""
    store = get_vectorstore()
    emb = get_embedder()
    ns = "test_retr"
    from app.interfaces import VectorRecord
    vecs = emb.embed(["退款到账时间", "商品颜色和尺寸"])
    store.create_namespace(ns, emb.dimension)
    store.upsert(ns, [
        VectorRecord(id="r1", vector=vecs[0], text="退款到账时间",
                     metadata={"tenant_id": "t1", "embedding_model": emb.model_id}),
        VectorRecord(id="r2", vector=vecs[1], text="商品颜色和尺寸",
                     metadata={"tenant_id": "t1", "embedding_model": emb.model_id}),
    ])
    r = Retriever(embedder=emb, store=store, namespace=ns)
    results = r.retrieve("退款", top_k=2, score_threshold=0.0)
    assert len(results) == 2
    # 阈值设成 1.01（超过任何余弦值）应全部被过滤
    assert r.retrieve("退款", top_k=2, score_threshold=1.01) == []
    store.delete_namespace(ns)


def test_retriever_tenant_isolation():
    """检索层把 tenant_id 透传给向量库做过滤。"""
    store = get_vectorstore()
    emb = get_embedder()
    ns = "test_retr_iso"
    from app.interfaces import VectorRecord
    vec = emb.embed(["退款"])[0]
    store.create_namespace(ns, emb.dimension)
    store.upsert(ns, [
        VectorRecord(id="A1", vector=vec, text="A租户退款",
                     metadata={"tenant_id": "A", "embedding_model": emb.model_id}),
        VectorRecord(id="B1", vector=vec, text="B租户退款",
                     metadata={"tenant_id": "B", "embedding_model": emb.model_id}),
    ])
    r = Retriever(embedder=emb, store=store, namespace=ns)
    only_a = r.retrieve("退款", tenant_id="A", score_threshold=0.0)
    assert len(only_a) == 1
    assert only_a[0].metadata["tenant_id"] == "A"
    store.delete_namespace(ns)


def test_deterministic_reranker_reorders_and_truncates():
    """重排器按自己的分数重排，并截断到 top_n。"""
    rr = DeterministicReranker()
    cands = make_candidates()
    # query 和 "退款到账时间" 有重叠，和 "商品颜色和尺寸" 无重叠
    out = rr.rerank("退款到账", cands, top_n=1)
    assert len(out) == 1
    assert out[0].id == "b"  # 粗召回里 b 排第2，重排后升到 top1
    assert isinstance(rr, __import__("app.interfaces", fromlist=["Reranker"]).Reranker)


def test_reranker_empty_and_local_protocol():
    """空候选返回空；LocalReranker 满足协议（不加载模型）。"""
    assert DeterministicReranker().rerank("x", [], 3) == []
    lr = LocalReranker()
    from app.interfaces import Reranker
    assert isinstance(lr, Reranker)
    assert "qwen" in lr.model_id
