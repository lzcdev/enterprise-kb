"""把真实文档数据灌进持久化后端（PG + Chroma），供 DBeaver 等 GUI 直接查看。

为什么需要这个脚本？
  默认 prototype/memory 档数据只在内存，进程结束即丢，GUI 无从看起。
  本脚本显式用持久化后端把 data/ 下的真实文档落盘：
    - PostgreSQL(enterprise_kb_test) → tenants 表：多租户配置（JSONB 覆盖链）
    - Chroma(data/chroma/*.sqlite3) → collection：真实 chunk + 向量 + metadata

  这样你打开 DBeaver 能看到"有血有肉"的数据，而不是空库或探针。

运行：
  .venv/bin/python scripts/seed_visual_data.py
幂等：重复运行不会重复插入（PG 用 ON CONFLICT，Chroma 用 upsert，ID 固定）。

前置：本机 PostgreSQL 在 5432 跑着（brew 版），库 enterprise_kb_test 已建。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ingestion.loader_pymupdf import PyMuPDFParser
from app.ingestion.splitter import RecursiveSplitter, section_to_chunks
from app.interfaces import VectorRecord
from app.retrieval.embedding_local import DeterministicEmbedder
from app.retrieval.vectorstore_chroma import ChromaStore

DATA_DIR = ROOT / "data"
CHROMA_DIR = DATA_DIR / "chroma"
PG_DSN = "postgresql://lzc@127.0.0.1:5432/enterprise_kb_test"
NAMESPACE = "kb__deterministic-hash-v1__v1"


def seed_chroma() -> int:
    """把真实文档切分、向量化、写入 Chroma 持久化库。"""
    parser = PyMuPDFParser()
    splitter = RecursiveSplitter(chunk_size=150, overlap=20)
    chunks = []
    for path in sorted(DATA_DIR.glob("*")):
        if path.suffix.lower() not in (".md", ".txt", ".docx", ".pdf"):
            continue
        for sec in parser.parse(str(path)):
            chunks.extend(section_to_chunks(sec, splitter))

    embedder = DeterministicEmbedder(dimension=128)  # 128 维，GUI 里看向量更短
    vectors = embedder.embed([c.text for c in chunks])

    store = ChromaStore(persist_dir=str(CHROMA_DIR))
    store.create_namespace(NAMESPACE, embedder.dimension)
    # 用内容哈希生成稳定 ID，而不是 Chunk 默认的 uuid4——否则每次重跑都是新 ID，
    # upsert 变 insert，数据翻倍（实测重跑一次 26→52）。内容哈希保证幂等。
    from hashlib import md5

    def stable_id(text: str, filename: str) -> str:
        return md5(f"{filename}:{text}".encode("utf-8")).hexdigest()

    store.upsert(NAMESPACE, [
        VectorRecord(
            id=stable_id(c.text, c.meta.filename), vector=v, text=c.text,
            metadata={
                "embedding_model": embedder.model_id,
                "tenant_id": "default",
                "doc_id": c.meta.doc_id,
                "filename": c.meta.filename,
                "page": c.meta.page if c.meta.page is not None else -1,
                "title_path": c.meta.title_path,
            },
        )
        for c, v in zip(chunks, vectors)
    ])
    print(f"Chroma: 写入 {len(chunks)} 条 chunk → {CHROMA_DIR}")
    print(f"        collection={NAMESPACE}, 向量维度={embedder.dimension}")
    return len(chunks)


def seed_pg() -> int:
    """往 PG 播种带真实覆盖链的租户配置。"""
    import json
    import psycopg2
    from psycopg2.extras import Json

    tenants = [
        ("default", "默认租户", "active", None, 1000, {}, {}, {}),
        ("law_firm", "星辰律师事务所", "active",
         ["pdf", "docx"], 500,
         {"chunk_size": 300, "top_n": 5},
         {"pdf": {"chunk_size": 400}},
         {}),
        ("startup", "初创公司演示", "active",
         ["md", "txt"], 100,
         {"chunk_size": 200, "score_threshold": 0.2},
         {},
         {"客服FAQ.md": {"chunk_size": 120}}),
    ]
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor() as cur:
            for t in tenants:
                tid, name, status, allowed, max_docs, ov, type_ov, doc_ov = t
                cur.execute("""
                    INSERT INTO tenants
                      (tenant_id, name, status, allowed_source_types, max_documents,
                       overrides, type_overrides, doc_overrides)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id) DO UPDATE SET
                      name=EXCLUDED.name, status=EXCLUDED.status,
                      allowed_source_types=EXCLUDED.allowed_source_types,
                      max_documents=EXCLUDED.max_documents,
                      overrides=EXCLUDED.overrides,
                      type_overrides=EXCLUDED.type_overrides,
                      doc_overrides=EXCLUDED.doc_overrides
                """, (tid, name, status, Json(allowed) if allowed else None,
                      max_docs, Json(ov), Json(type_ov), Json(doc_ov)))
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tenants")
            n = cur.fetchone()[0]
        print(f"PostgreSQL: {len(tenants)} 个租户配置已写入 → {PG_DSN}")
        print(f"            tenants 表现有 {n} 行")
        return n
    finally:
        conn.close()


def main() -> int:
    print("=" * 64)
    print("播种可可视化数据：真实文档 → PG + Chroma")
    print("=" * 64)
    n_chroma = seed_chroma()
    n_pg = seed_pg()
    print("=" * 64)
    print(f"完成：Chroma {n_chroma} 条向量 / PG {n_pg} 个租户")
    print("现在可用 DBeaver 连接查看（连接方式见 docs/DATA_VISUALIZATION.md）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
