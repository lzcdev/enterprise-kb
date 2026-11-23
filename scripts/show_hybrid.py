"""演示：混合检索（BM25 + 向量 RRF 融合）如何救回精确串查询。

运行：
    .venv/bin/python scripts/show_hybrid.py "XJ-200 保修期多久"
    .venv/bin/python scripts/show_hybrid.py "退款多久到账"

对比纯向量检索与混合检索的 Top3，看精确型号串是否被 BM25 救回来。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.factory import active_namespace, get_embedder, get_vectorstore
from app.interfaces import ScoredChunk, VectorRecord
from app.retrieval.hybrid import BM25Index, HybridRetriever, rrf_fuse
from app.retrieval.retriever import Retriever

# 构造测试文档：含精确型号串 + 语义相关但无型号的干扰项
DOCUMENTS = [
    ("d1", "XJ-200 型吸尘器的整机保修期为两年，电机保修三年。"),
    ("d2", "XL-500 扫地机器人提供一年质保，配件不在保修范围内。"),
    ("d3", "我们的售后服务政策覆盖所有在保修期内的产品，请7天无理由退款。"),
    ("d4", "产品使用说明：请定期清理滤网以保证吸力。"),
    ("d5", "关于退款，签收后七日内可申请无理由退货退款。"),
]


def main(query: str = "XJ-200 保修期多久"):
    embedder = get_embedder()
    store = get_vectorstore()
    ns = active_namespace() + "__hybrid_demo"
    store.create_namespace(ns, embedder.dimension)

    vectors = embedder.embed([text for _, text in DOCUMENTS])
    records = [
        VectorRecord(
            id=doc_id, vector=vec, text=text,
            metadata={"tenant_id": "default", "doc_id": doc_id,
                      "filename": "产品手册.md", "embedding_model": embedder.model_id},
        )
        for (doc_id, text), vec in zip(DOCUMENTS, vectors)
    ]
    store.upsert(ns, records)

    retriever = Retriever(embedder=embedder, store=store, namespace=ns)
    print(f"查询: {query}\n")
    print("=" * 60)

    # ① 纯向量检索
    vec_results = retriever.retrieve(query, top_k=3, tenant_id="default", score_threshold=0.0)
    print("\n【纯向量检索 Top3】")
    for i, c in enumerate(vec_results, 1):
        print(f"  {i}. score={c.score:.4f}  {c.text}")

    # ② 纯 BM25
    all_docs = [ScoredChunk(id=r.id, text=r.text, score=0.0, metadata=r.metadata)
                for r in records]
    bm25 = BM25Index()
    bm25.build(all_docs)
    bm25_results = bm25.search(query, top_k=3)
    print("\n【纯 BM25 检索 Top3】")
    for i, c in enumerate(bm25_results, 1):
        print(f"  {i}. score={c.score:.4f}  {c.text}")

    # ③ 混合 RRF
    hybrid = HybridRetriever(retriever)
    hybrid.build_bm25(all_docs)
    mixed = hybrid.retrieve(query, top_k=3, tenant_id="default", score_threshold=0.0)
    print("\n【混合检索 RRF Top3】")
    for i, c in enumerate(mixed, 1):
        ranks = c.metadata.get("rrf_ranks", [])
        print(f"  {i}. rrf={c.score:.5f} ranks={ranks}  {c.text}")

    print("\n" + "=" * 60)
    top_vec = vec_results[0].id if vec_results else "无"
    top_mix = mixed[0].id if mixed else "无"
    print(f"Top1 对比：纯向量={top_vec}  混合={top_mix}")
    if top_vec != top_mix:
        print("[结论] 混合检索改变了 Top1——BM25 把精确串命中的文档拉了上来")
    else:
        print("[说明] 本次 Top1 未变：当前是 deterministic 哈希档，")
        print("       它对字符重叠敏感，恰好也能命中 XJ-200。")
        print("       换成真语义 embedding（Qwen3）后，编号串会被'压扁'语义，")
        print("       这时 BM25 的救回价值才真正显现——这正是生产必配混合检索的原因。")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "XJ-200 保修期多久")
