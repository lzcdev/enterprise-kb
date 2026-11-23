"""把 chunk -> 向量 -> 入库 -> 检索 的全过程导出成可视化 HTML。

运行（在项目根目录）：
    .venv/bin/python scripts/visualize_vectors.py
    .venv/bin/python scripts/visualize_vectors.py "七天无理由退款怎么申请？"

产出：
    data/viz/vectors.html   —— 自包含单文件，双击用浏览器打开

为什么要单独写这个脚本，而不是直接用向量库的 GUI？
1. 当前 VECTORSTORE_BACKEND=memory，数据只在进程内存里，没有任何现成 GUI 能看；
2. 即使是 Chroma/Milvus，GUI 也只能看"存进去的向量"，看不到"chunk 是怎么一步步变成向量"的；
3. 教学目的：把黑盒拆成 文本 -> n-gram -> 哈希桶 -> 归一化向量 -> metadata -> 相似度 的每一步。
"""
import argparse
import json
import math
import random
import sys
from collections import Counter
from hashlib import md5
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.factory import active_namespace, get_embedder, get_vectorstore
from app.ingestion.loader_pymupdf import PyMuPDFParser
from app.ingestion.splitter import RecursiveSplitter, section_to_chunks
from app.interfaces import VectorRecord


# ---------------------------------------------------------------------------
# 1. 建库：和 show_embedding.py 完全相同的链路（故意重复，脚本自包含便于教学）
# ---------------------------------------------------------------------------
def build_index(embedder, store, namespace: str, doc_path: str, tenant_id: str) -> list[VectorRecord]:
    parser = PyMuPDFParser()
    sections = parser.parse(doc_path)
    chunks: list = []
    for sec in sections:
        chunks.extend(section_to_chunks(sec, RecursiveSplitter(chunk_size=150, overlap=20)))

    vectors = embedder.embed([c.text for c in chunks])  # passage 侧不加 query 指令
    records = [
        VectorRecord(
            id=c.chunk_id,
            vector=v,
            text=c.text,
            metadata={
                "embedding_model": embedder.model_id,
                "tenant_id": tenant_id,
                "filename": c.meta.filename,
                "page": c.meta.page,
                "title_path": c.meta.title_path,
            },
        )
        for c, v in zip(chunks, vectors)
    ]
    store.create_namespace(namespace, embedder.dimension)
    store.upsert(namespace, records)
    return records


# ---------------------------------------------------------------------------
# 2. PCA 降维：纯 Python 幂迭代，不依赖 sklearn/numpy，1024 维 -> 2 维供画散点图
# ---------------------------------------------------------------------------
def pca_2d(vectors: list[list[float]], extra: list[float] | None = None,
           iters: int = 60, seed: int = 42):
    """对 vectors 做 PCA 投影到 2D；extra（如 query 向量）用同一套均值和主成分投影。

    为什么不用 sklearn？——保持原型档"零第三方依赖"的承诺，同时把 PCA 原理暴露给学习者：
    中心化 -> 反复乘协方差矩阵求主成分 -> 投影。
    """
    n, d = len(vectors), len(vectors[0])
    mean = [sum(v[j] for v in vectors) / n for j in range(d)]
    centered = [[v[j] - mean[j] for j in range(d)] for v in vectors]

    def matvec(u: list[float]) -> list[float]:
        """乘 X^T·X·u。不用真的构造 d×d 协方差矩阵，省内存。"""
        out = [0.0] * d
        for row in centered:
            dot = sum(a * b for a, b in zip(row, u))
            for j in range(d):
                out[j] += row[j] * dot
        return out

    def normalize(u: list[float]) -> list[float]:
        s = math.sqrt(sum(x * x for x in u)) or 1.0
        return [x / s for x in u]

    rng = random.Random(seed)

    def find_pc(exclude: list[list[float]] | None = None) -> list[float]:
        u = normalize([rng.random() for _ in range(d)])
        for _ in range(iters):
            w = matvec(u)
            for e in exclude or []:
                p = sum(a * b for a, b in zip(w, e))  # 施密特正交化
                w = [a - p * b for a, b in zip(w, e)]
            u = normalize(w)
        return u

    pc1 = find_pc()
    pc2 = find_pc([pc1])

    def project(v: list[float]) -> list[float]:
        c = [v[j] - mean[j] for j in range(d)]
        return [sum(a * b for a, b in zip(c, pc1)),
                sum(a * b for a, b in zip(c, pc2))]

    return [project(v) for v in vectors], (project(extra) if extra else None)


# ---------------------------------------------------------------------------
# 3. 还原 chunk -> 向量 的中间步骤（仅 deterministic 档有意义，用来教学演示）
# ---------------------------------------------------------------------------
def ngram_breakdown(text: str, dimension: int, ngram: int = 2,
                    top_k: int = 8) -> dict:
    """复刻 DeterministicEmbedder._vector 的哈希分桶过程，把黑盒打开给人看。"""
    padded = f"^{text}$"
    grams = [padded[i:i + ngram] for i in range(len(padded) - ngram + 1)]
    counter: Counter = Counter()
    gram_bucket: dict[str, int] = {}
    for g in grams:
        idx = int(md5(g.encode("utf-8")).hexdigest(), 16) % dimension
        counter[idx] += 1
        gram_bucket.setdefault(g, idx)

    top = counter.most_common(top_k)
    return {
        "char_count": len(text),
        "ngram_count": len(grams),
        "unique_buckets": len(counter),
        "sparsity": round(1 - len(counter) / dimension, 4),
        "first_grams": [{"gram": g, "bucket": gram_bucket[g]} for g in grams[:8]],
        "top_buckets": [
            {
                "bucket": idx,
                "count": cnt,
                "grams": [g for g, b in gram_bucket.items() if b == idx][:4],
            }
            for idx, cnt in top
        ],
    }


# ---------------------------------------------------------------------------
# 4. 组装页面数据
# ---------------------------------------------------------------------------
def build_payload(embedder, store, namespace, tenant_id, query, records) -> dict:
    q_vec = embedder.embed([query], is_query=True)[0]
    # 用 store.search 拿分数，和线上检索完全一致（不是自己另算一遍）
    scored = store.search(namespace, q_vec, top_k=len(records),
                          filters={"tenant_id": tenant_id})
    score_map = {s.id: s.score for s in scored}

    coords, q_coord = pca_2d([r.vector for r in records], extra=q_vec)
    dim = embedder.dimension

    rec_payload = []
    for r, coord in zip(records, coords):
        rec_payload.append({
            "id": r.id,
            "text": r.text,
            "vector": r.vector,           # 完整向量，前端热力图用
            "metadata": r.metadata,
            "score": round(score_map.get(r.id, 0.0), 4),
            "coord": [round(coord[0], 5), round(coord[1], 5)],
            "norm": round(math.sqrt(sum(x * x for x in r.vector)), 4),
            "breakdown": ngram_breakdown(r.text, dim),
        })

    return {
        "config": {
            "embedder": embedder.model_id,
            "dimension": dim,
            "store": store.name,
            "namespace": namespace,
            "tenant_id": tenant_id,
            "record_count": len(records),
            "query": query,
            "namespaces_in_store": store.list_namespaces(),
            # 暴露版本标记不一致：namespace 名字来自配置，metadata 来自实际 embedder
            "namespace_matches_embedder": embedder.model_id.replace("/", "-").lower() in namespace,
        },
        "query_vector": q_vec,
        "query_coord": [round(q_coord[0], 5), round(q_coord[1], 5)],
        "records": rec_payload,
    }


def main() -> None:
    cli = argparse.ArgumentParser(description="向量存储与 chunk→向量过程可视化")
    cli.add_argument("query", nargs="?", default="七天无理由退款怎么申请？")
    cli.add_argument("--tenant", default="default")
    cli.add_argument("--doc", default="data/测试产品手册.md")
    args = cli.parse_args()

    embedder = get_embedder()
    store = get_vectorstore()
    namespace = active_namespace()

    records = build_index(embedder, store, namespace, args.doc, args.tenant)
    print(f"[建库] {len(records)} 条 VectorRecord -> namespace={namespace}")
    print(f"[版本] metadata.embedding_model={embedder.model_id}")
    print(f"[检查] namespace 与实际 embedder 一致: "
          f"{embedder.model_id.replace('/', '-').lower() in namespace}")

    payload = build_payload(embedder, store, namespace, args.tenant, args.query, records)

    out_dir = ROOT / "data" / "viz"
    out_dir.mkdir(parents=True, exist_ok=True)
    template = (ROOT / "scripts" / "viz_template.html").read_text(encoding="utf-8")
    html = template.replace("/*__PAYLOAD__*/", json.dumps(payload, ensure_ascii=False))
    out_path = out_dir / "vectors.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"[完成] 可视化页面：{out_path.relative_to(ROOT)}")
    print(f"       用浏览器打开这个文件即可交互查看")


if __name__ == "__main__":
    main()
