"""任务5 演示：端到端 RAG —— 检索 → 重排 → 生成 → 溯源。

运行：
    .venv/bin/python scripts/show_rag.py "七天无理由退款怎么申请？"
    .venv/bin/python scripts/show_rag.py "公司团建去哪"          # 空结果分支演示
    .venv/bin/python scripts/show_rag.py --build-only            # 只建库

看点：
1. 答案带 [n] 引用标记，sources 溯源到文件名/页码/原文
2. 问知识库里没有的问题时，走空结果分支，不调 LLM
3. 端到端耗时 elapsed_ms
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.factory import active_namespace, get_embedder, get_reranker, get_vectorstore
from app.generation.generator import get_llm
from app.ingestion.loader_pymupdf import PyMuPDFParser
from app.ingestion.splitter import RecursiveSplitter, section_to_chunks
from app.interfaces import VectorRecord
from app.rag_service import RAGService
from app.retrieval.retriever import Retriever


def build_index(embedder, store, namespace: str) -> int:
    """把测试手册灌进【传入的】库实例（memory 档必须同一实例）。"""
    sections = PyMuPDFParser().parse("data/测试产品手册.md")
    chunks = []
    for sec in sections:
        chunks.extend(section_to_chunks(sec, RecursiveSplitter(chunk_size=150, overlap=20)))
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


def main() -> None:
    cli = argparse.ArgumentParser(description="端到端 RAG 演示")
    cli.add_argument("query", nargs="?", default="七天无理由退款怎么申请？")
    cli.add_argument("--threshold", type=float, default=0.0,
                     help="检索阈值，默认0看完整链路；调高可触发空结果分支")
    cli.add_argument("--build-only", action="store_true")
    args = cli.parse_args()

    namespace = active_namespace()
    embedder = get_embedder()
    store = get_vectorstore()
    n = build_index(embedder, store, namespace)

    print("=" * 70)
    print(f"建库: {n} 条 | namespace: {namespace}")
    print(f"embedder: {embedder.model_id} | rerank: {get_reranker().model_id} | llm: {get_llm().model_id}")
    print("=" * 70)
    if args.build_only:
        return

    # 显式注入同一 store 实例，避免 memory 档建库/检索各用一个空库
    service = RAGService(
        retriever=Retriever(embedder=embedder, store=store, namespace=namespace),
        reranker=get_reranker(),
        llm=get_llm(),
    )
    ans = service.answer(args.query, score_threshold=args.threshold)

    print(f"\n问题: {ans.question}")
    print(f"答案: {ans.answer}")
    print(f"耗时: {ans.elapsed_ms:.1f} ms")
    print(f"\n引用来源 ({len(ans.sources)} 条):")
    for i, s in enumerate(ans.sources, 1):
        loc = f"第{s.page}页" if s.page else (f"{s.start_ms}ms" if s.start_ms else "")
        print(f"  [{i}] {s.filename} {loc} (score={s.score:.4f})")
        print(f"      {s.text[:60]}...")


if __name__ == "__main__":
    main()
