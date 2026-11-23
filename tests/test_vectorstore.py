"""任务3 单元测试：Embedder 协议 + 向量库存取/检索/隔离。"""
from app.factory import get_embedder, get_vectorstore, active_namespace
from app.interfaces import Embedder, VectorStore, VectorRecord


def make_records(embedder, tenant="t1"):
    vectors = embedder.embed(["七天无理由退款", "退款到账时间", "商品颜色和尺寸"])
    return [
        VectorRecord(
            # id 带租户前缀：不同租户的测试记录不能撞 id，否则 upsert 会互相覆盖
            id=f"{tenant}_c{i}", vector=v, text=t,
            metadata={"tenant_id": tenant, "embedding_model": embedder.model_id},
        )
        for i, (v, t) in enumerate(zip(vectors, ["七天无理由退款", "退款到账时间", "商品颜色和尺寸"]))
    ]


def test_embedder_protocol_and_deterministic():
    emb = get_embedder()
    assert isinstance(emb, Embedder)
    assert emb.dimension == 1024
    v1 = emb.embed(["同样文本"])
    v2 = emb.embed(["同样文本"])
    assert v1 == v2  # 确定性：相同输入必得相同向量
    # query/passage 同文本也应一致（deterministic 档无指令差异）
    assert emb.embed(["同样文本"], is_query=True) == v1


def test_vectorstore_protocol_and_crud():
    store = get_vectorstore()
    emb = get_embedder()
    assert isinstance(store, VectorStore)
    ns = "test_ns"
    store.create_namespace(ns, emb.dimension)
    store.upsert(ns, make_records(emb))
    assert store.count(ns) == 3
    assert ns in store.list_namespaces()

    q = emb.embed(["退款"], is_query=True)[0]
    results = store.search(ns, q, top_k=2)
    assert len(results) == 2
    assert results[0].score >= results[1].score  # 按相似度降序

    store.delete_namespace(ns)
    assert store.count(ns) == 0


def test_tenant_isolation():
    """多租户隔离：按 tenant_id 过滤，只能命中本租户数据。"""
    store = get_vectorstore()
    emb = get_embedder()
    ns = "test_iso"
    store.create_namespace(ns, emb.dimension)
    store.upsert(ns, make_records(emb, tenant="A") + make_records(emb, tenant="B"))
    q = emb.embed(["退款"], is_query=True)[0]
    a_only = store.search(ns, q, top_k=10, filters={"tenant_id": "A"})
    assert len(a_only) == 3
    assert all(r.metadata["tenant_id"] == "A" for r in a_only)


def test_namespace_encodes_model_version():
    ns = active_namespace()
    assert "qwen" in ns or "deterministic" in ns  # 索引名编码了模型标识
    assert ns.startswith("kb__")
