"""演示脚本：chunk -> 向量 -> 入库 -> 检索 的完整链路（任务3）。

默认用零依赖的 deterministic + memory 档，立刻可跑：
    .venv/bin/python scripts/show_embedding.py

用法：
    .venv/bin/python scripts/show_embedding.py "退款政策怎么申请"
    .venv/bin/python scripts/show_embedding.py "退款" --tenant t1
    .venv/bin/python scripts/show_embedding.py --build-only   # 只建库不查询
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.factory import get_embedder, get_vectorstore, active_namespace
from app.ingestion.loader_pymupdf import PyMuPDFParser
from app.ingestion.splitter import RecursiveSplitter, section_to_chunks
from app.interfaces import VectorRecord


def build_index(embedder, store, namespace: str, doc_path: str, tenant_id: str) -> int:
    """离线建库：文档 -> chunk -> 向量 -> 带版本标记的 VectorRecord -> 入库。"""
    parser = PyMuPDFParser()
    sections = parser.parse(doc_path)
    chunks = []
    for sec in sections:
        chunks.extend(section_to_chunks(sec, RecursiveSplitter(chunk_size=150, overlap=20)))

    texts = [c.text for c in chunks]
    vectors = embedder.embed(texts)  # passage 不加 query 指令

    records = []
    for chunk, vec in zip(chunks, vectors):
        records.append(VectorRecord(
            id=chunk.chunk_id,
            vector=vec,
            text=chunk.text,
            metadata={
                "embedding_model": embedder.model_id,  # 版本标记：迁移的锚点
                "tenant_id": tenant_id,
                "filename": chunk.meta.filename,
                "page": chunk.meta.page,
                "title_path": chunk.meta.title_path,
            },
        ))
    store.create_namespace(namespace, embedder.dimension)
    store.upsert(namespace, records)
    return len(records)


def main() -> None:
    cli = argparse.ArgumentParser(description="向量化与检索链路演示")
    cli.add_argument("query", nargs="?", default="七天无理由退款怎么申请？",
                     help="查询问题")
    cli.add_argument("--tenant", default="default", help="租户 id（验证隔离）")
    cli.add_argument("--top-k", type=int, default=3, help="返回条数")
    cli.add_argument("--build-only", action="store_true", help="只建库不查询")
    args = cli.parse_args()

    embedder = get_embedder()
    store = get_vectorstore()
    namespace = active_namespace()

    print("=" * 70)
    print(f"Embedder: {embedder.model_id}  维度={embedder.dimension}")
    print(f"Store:    {store.name}  namespace={namespace}")
    print("=" * 70)

    n = build_index(embedder, store, namespace, "data/测试产品手册.md", args.tenant)
    print(f"[建库] {n} 个 chunk -> {store.count(namespace)} 条向量记录")
    print(f"[版本] metadata.embedding_model = {embedder.model_id}")

    if args.build_only:
        return

    # 在线查询：query 必须用同一个 embedder 向量化（is_query=True）
    q_vec = embedder.embed([args.query], is_query=True)[0]
    results = store.search(
        namespace, q_vec, top_k=args.top_k,
        filters={"tenant_id": args.tenant},  # 多租户隔离：只搜本租户数据
    )
    print(f"\n[查询] {args.query}")
    for i, r in enumerate(results, 1):
        print(f"  {i}. score={r.score:.4f}  page={r.metadata.get('page')}  "
              f"{r.text[:60]}...")

    # 隔离验证：用一个不存在的租户查，应查不到任何数据
    other = store.search(namespace, q_vec, top_k=3, filters={"tenant_id": "__other__"})
    print(f"\n[隔离] 其他租户命中: {len(other)} 条（应为 0）")


if __name__ == "__main__":
    main()
