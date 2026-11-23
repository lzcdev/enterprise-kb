"""任务4 演示：对比【粗召回】与【精排重排】的结果差异。

运行：
    .venv/bin/python scripts/show_retrieval.py "退款多久到账"
    .venv/bin/python scripts/show_retrieval.py "退货流程" --backend deterministic

看点：
1. 粗召回按 cosine 排序（双塔，快但粗）
2. 重排器重新打分排序（交叉编码器或零依赖启发式，准但慢）
3. 观察 top1 是否变化 —— 这就是 rerank 的价值
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.factory import active_namespace, get_embedder, get_reranker, get_vectorstore
from app.ingestion.loader_pymupdf import PyMuPDFParser
from app.ingestion.splitter import RecursiveSplitter, section_to_chunks
from app.interfaces import VectorRecord
from app.retrieval.retriever import Retriever


def build_index(embedder, store, namespace: str) -> int:
    """复用任务3的建库链路，把测试文档灌进【传入的】向量库实例。

    注意：memory 档的 get_vectorstore() 每次都 new 一个空实例，
    所以必须把同一个 store 对象传给建库和检索，否则检索查的是另一个空库。
    Chroma/Milvus 档连的是同一持久化存储，不会有这个问题。
    """
    parser = PyMuPDFParser()
    sections = parser.parse("data/测试产品手册.md")
    chunks = []
    for sec in sections:
        chunks.extend(section_to_chunks(sec, RecursiveSplitter(chunk_size=150, overlap=20)))
    vectors = embedder.embed([c.text for c in chunks])
    records = [
        VectorRecord(
            id=c.chunk_id, vector=v, text=c.text,
            metadata={
                "embedding_model": embedder.model_id, "tenant_id": "default",
                "filename": c.meta.filename, "page": c.meta.page,
                "title_path": c.meta.title_path,
            },
        )
        for c, v in zip(chunks, vectors)
    ]
    store.create_namespace(namespace, embedder.dimension)
    store.upsert(namespace, records)
    return len(records)


def show_list(title: str, items, score_label: str) -> None:
    print(f"\n{title}")
    for i, c in enumerate(items, 1):
        pg = c.metadata.get("page")
        print(f"  {i}. [{score_label}={c.score:.4f}] p{pg} {c.text[:50]}...")


def main() -> None:
    cli = argparse.ArgumentParser(description="粗召回与重排对比")
    cli.add_argument("query", nargs="?", default="七天无理由退款怎么申请？")
    cli.add_argument("--backend", default=None,
                     help="覆盖 RERANK_BACKEND: deterministic/local/off")
    cli.add_argument("--top-k", type=int, default=10, help="粗召回条数")
    cli.add_argument("--top-n", type=int, default=3, help="重排后保留条数")
    cli.add_argument("--threshold", type=float, default=0.0,
                     help="检索分数阈值（演示用，默认0看全部候选）")
    args = cli.parse_args()

    namespace = active_namespace()
    embedder = get_embedder()
    store = get_vectorstore()
    n = build_index(embedder, store, namespace)

    # 显式注入同一实例，避免 memory 档"建库和检索各用一个空库"的坑
    retriever = Retriever(embedder=embedder, store=store, namespace=namespace)
    # 无 rerank：直接走粗召回（这里把阈值设为0以便看到全部候选）
    coarse = retriever.retrieve(args.query, top_k=args.top_k,
                                score_threshold=args.threshold)

    print("=" * 70)
    print(f"query: {args.query}")
    print(f"建库: {n} 条 | namespace: {namespace} | embedder: {retriever.embedder.model_id}")
    print("=" * 70)
    show_list("【粗召回】双塔 cosine 排序:", coarse, "cosine")

    # 有 rerank：对粗召回的候选再精排
    if args.backend == "off":
        print("\n[重排] 已关闭（--backend off）")
        return
    from app.config import settings
    backend = args.backend or settings.RERANK_BACKEND
    # 用环境变量临时切换，保证 factory 读到正确 backend
    if args.backend:
        settings.RERANK_BACKEND = args.backend
    from app.factory import get_reranker
    reranker = get_reranker()
    if reranker is None:
        print("\n[重排] 未启用")
        return
    refined = reranker.rerank(args.query, coarse, top_n=args.top_n)
    show_list(f"【精排重排】{reranker.model_id} 排序:", refined, "rerank")

    coarse_top = coarse[0].id if coarse else None
    refined_top = refined[0].id if refined else None
    changed = coarse_top != refined_top
    print(f"\n[结论] top1 {'发生变化' if changed else '保持不变'}"
          f"（粗召回={coarse_top} / 重排={refined_top}）")
    if backend == "deterministic":
        print("[注意] deterministic 档是关键词重叠启发式，无语义理解，"
              "这里只演示控制流，真实质量请看 local 档。")


if __name__ == "__main__":
    main()
