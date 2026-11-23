"""波次1 向量持久化验证：证明 Chroma 的索引在服务重启后仍可检索。

双进程设计（同 verify_pg_persistence.py）：
    进程 A：往 Chroma 写几条固定向量，记下查询 expected top id。
    进程 A 退出（内存全清）。
    进程 B：全新进程从同一个 persist_dir 读，查同样的 query，比对 top id。

用法：
    .venv/bin/python scripts/verify_chroma_persistence.py          # 一键双进程
    .venv/bin/python scripts/verify_chroma_persistence.py write    # 仅写入
    .venv/bin/python scripts/verify_chroma_persistence.py read     # 仅读取
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 用临时目录做持久化路径：每次运行都是干净环境，不污染项目 data/。
# 两个子进程通过环境变量共享同一个目录路径。
PERSIST_DIR = os.environ.get("KB_CHROMA_TEST_DIR", "")

NAMESPACE = "kb__deterministic__v1"
QUERY_VECTOR = [0.9, 0.1, 0.0, 0.0]
CHUNKS = [
    ("chunk_refund", "退款政策：七天无理由", [1.0, 0.0, 0.0, 0.0]),
    ("chunk_color", "商品颜色说明", [0.0, 1.0, 0.0, 0.0]),
    ("chunk_warranty", "保修条款", [0.8, 0.2, 0.0, 0.0]),
]


def _make_store():
    from app.retrieval.vectorstore_chroma import ChromaStore

    store = ChromaStore(persist_dir=PERSIST_DIR)
    store.create_namespace(NAMESPACE, dimension=4)
    return store


def _expect_file() -> Path:
    return Path(PERSIST_DIR) / "expected_top_ids.txt"


def write_phase() -> int:
    from app.interfaces import VectorRecord

    store = _make_store()
    store.upsert(NAMESPACE, [
        VectorRecord(
            id=cid, vector=vec, text=text,
            metadata={"tenant_id": "default", "embedding_model": "deterministic"},
        )
        for cid, text, vec in CHUNKS
    ])
    results = store.search(NAMESPACE, QUERY_VECTOR, top_k=3,
                           filters={"tenant_id": "default"})
    top_ids = [r.id for r in results]
    # 关键：把"重启前"的结果落盘。验收标准是【前后一致】，
    # 而不是 top1 等于某个我预判的值——后者依赖相似度数学，不是持久化的职责。
    _expect_file().write_text("\n".join(top_ids), encoding="utf-8")
    print(f"[write] ✓ 写入 {len(CHUNKS)} 条向量到 {PERSIST_DIR}")
    print(f"[write] 重启前 top3 = {top_ids}（已记录到文件，供进程 B 比对）")
    return 0


def read_phase() -> int:
    store = _make_store()
    count = store.count(NAMESPACE)
    results = store.search(NAMESPACE, QUERY_VECTOR, top_k=3,
                           filters={"tenant_id": "default"})
    top_ids = [r.id for r in results]
    expect_file = _expect_file()
    expected = expect_file.read_text(encoding="utf-8").splitlines() if expect_file.exists() else []

    print(f"[read] 从磁盘恢复的向量数 = {count}")
    print(f"[read] 重启后 top3 = {top_ids}")
    print(f"[read] 重启前 top3 = {expected}")

    if count != len(CHUNKS):
        print(f"[read] ✗ 向量数不符：期望 {len(CHUNKS)}，实际 {count}")
        return 1
    if not expected:
        print("[read] ✗ 找不到重启前的结果记录，无法比对")
        return 1
    if top_ids != expected:
        print(f"[read] ✗ 重启前后 top id 不一致：{expected} → {top_ids}")
        return 1
    print("[read] ✓ 重启前后同一 query 返回完全相同的 top chunk id —— 向量持久化成功")
    return 0


def run_both() -> int:
    global PERSIST_DIR
    if not PERSIST_DIR:
        PERSIST_DIR = tempfile.mkdtemp(prefix="kb_chroma_test_")

    print("=" * 64)
    print("波次1 向量持久化验证：两个独立进程 = 模拟服务重启")
    print(f"持久化目录: {PERSIST_DIR}")
    print("=" * 64)
    python = sys.executable
    script = str(Path(__file__).resolve())
    env = dict(os.environ, KB_CHROMA_TEST_DIR=PERSIST_DIR)

    print("\n--- 阶段1：写入（进程 A）---")
    if subprocess.run([python, script, "write"], env=env).returncode != 0:
        return 1
    print("\n--- 进程 A 已退出 ---\n--- 阶段2：读取（全新进程 B）---")
    if subprocess.run([python, script, "read"], env=env).returncode != 0:
        print("\n结果: ✗ 向量持久化验证失败")
        return 1
    print("\n" + "=" * 64)
    print("结果: ✓ 跨进程向量持久化验证通过 —— 重启后检索结果一致")
    print("=" * 64)
    return 0


def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else "both"
    if phase in ("write", "read") and not PERSIST_DIR:
        print("错误：单阶段运行需设置 KB_CHROMA_TEST_DIR（both 模式会自动建临时目录）")
        return 2
    if phase == "write":
        return write_phase()
    if phase == "read":
        return read_phase()
    if phase == "both":
        return run_both()
    print(f"未知阶段: {phase!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
