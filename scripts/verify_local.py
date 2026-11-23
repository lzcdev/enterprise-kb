"""一键本地验证：把"这个项目到底能不能跑"变成一条命令 + 一份报告。

运行：
    .venv/bin/python scripts/verify_local.py
    .venv/bin/python scripts/verify_local.py --json      # 机器可读输出（CI 用）
    .venv/bin/python scripts/verify_local.py --skip-tests # 只验链路不跑 pytest

为什么要这个脚本？
    教学项目最容易"我以为能跑"——配置改了、依赖没装、某个环节悄悄坏了都不知道。
    真实公司里这叫 smoke test（冒烟测试）：部署/改动后先跑一遍最关键的链路，
    秒级确认"没冒烟"。它也是未来 CI 流水线的第一步。

检查分层（从便宜到昂贵、从静态到端到端）：
    1. config   配置一致性 + 依赖预检（纯静态，毫秒级）
    2. ingest   文档解析 → 切分（不依赖向量/模型）
    3. retrieve 向量化 → 入库 → 检索（验证 namespace/隔离）
    4. generate 端到端问答 → 引用溯源（验证全链路编排）
    5. api      FastAPI 接口（JWT 鉴权 + /ask）
    6. tests    全量 pytest

    每层独立 try/except：一层挂了不影响后面层继续跑，最后统一汇总——
    这样一次运行能看到所有问题，而不是"修一个跑一次"。
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Checker:
    """收集每层检查结果，统一输出。"""

    def __init__(self):
        self.results: list[dict] = []

    def run(self, name: str, fn):
        t0 = time.perf_counter()
        try:
            detail = fn() or ""
            self.results.append({
                "name": name, "ok": True, "detail": str(detail),
                "ms": round((time.perf_counter() - t0) * 1000, 1),
            })
        except Exception as exc:
            self.results.append({
                "name": name, "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "ms": round((time.perf_counter() - t0) * 1000, 1),
            })

    @property
    def all_ok(self) -> bool:
        return all(r["ok"] for r in self.results)


def check_config() -> str:
    from app.config import settings, validate_runtime
    problems = validate_runtime()
    if problems:
        raise AssertionError("; ".join(problems))
    return (f"TIER={settings.TIER} 名义与实际一致；"
            f"{settings.EMBEDDING_BACKEND}/{settings.VECTORSTORE_BACKEND}/"
            f"{settings.LLM_BACKEND} 依赖齐全")


def check_ingest() -> str:
    from app.ingestion.loader_pymupdf import PyMuPDFParser
    from app.ingestion.splitter import RecursiveSplitter, section_to_chunks
    sections = PyMuPDFParser().parse(str(ROOT / "data" / "测试产品手册.md"))
    assert sections, "解析结果为空"
    chunks = []
    for sec in sections:
        chunks.extend(section_to_chunks(sec, RecursiveSplitter(chunk_size=150, overlap=20)))
    assert chunks, "切分结果为空"
    assert any(c.meta.filename != "unknown" for c in chunks), "filename 未透传（溯源会显示 unknown）"
    return f"{len(sections)} 段 → {len(chunks)} chunk，filename 已透传"


def check_retrieve() -> str:
    from app.factory import active_namespace, get_embedder, get_vectorstore
    from app.ingestion.loader_pymupdf import PyMuPDFParser
    from app.ingestion.splitter import RecursiveSplitter, section_to_chunks
    from app.interfaces import VectorRecord
    from app.retrieval.retriever import Retriever

    emb = get_embedder()
    store = get_vectorstore()
    ns = active_namespace()

    sections = PyMuPDFParser().parse(str(ROOT / "data" / "测试产品手册.md"))
    chunks = []
    for sec in sections:
        chunks.extend(section_to_chunks(sec, RecursiveSplitter(chunk_size=150, overlap=20)))
    vecs = emb.embed([c.text for c in chunks])
    store.create_namespace(ns, emb.dimension)
    store.upsert(ns, [
        VectorRecord(id=c.chunk_id, vector=v, text=c.text,
                     metadata={"embedding_model": emb.model_id, "tenant_id": "default",
                               "filename": c.meta.filename, "doc_id": c.meta.doc_id,
                               "page": c.meta.page, "title_path": c.meta.title_path})
        for c, v in zip(chunks, vecs)
    ])
    assert store.count(ns) == len(chunks), "入库数量不符"

    retriever = Retriever(embedder=emb, store=store, namespace=ns)
    hits = retriever.retrieve("退款", top_k=3, tenant_id="default", score_threshold=0.0)
    assert hits, "检索无结果"
    others = retriever.retrieve("退款", top_k=3, tenant_id="__no_such_tenant__", score_threshold=0.0)
    assert not others, "租户隔离失效：未注册租户查到了数据"
    return (f"入库 {store.count(ns)} 条；检索命中 {len(hits)}；"
            f"跨租户命中 {len(others)}（应为 0）")


def check_generate() -> str:
    from app.api.main import bootstrap_demo_index  # 触发 demo 库初始化
    from app.api.main import _service  # 复用 API 单例：memory 档必须同一 store 实例

    # 直接用 API 的服务单例——它的 retriever 绑定的库已被 bootstrap_demo_index 灌入 demo 数据。
    # 不要新建 RAGService（memory 档每次 new 一个空库，会检索不到）。
    service = _service
    ans = service.answer("七天无理由退款", score_threshold=0.0)
    assert ans.answer, "答案为空"
    assert ans.sources, "未产生引用溯源"
    # 空结果分支：不该硬编答案
    empty = service.answer("公司团建去哪", score_threshold=0.9)
    assert "没有相关信息" in empty.answer, "空结果未短路"
    return f"答案 {len(ans.answer)} 字 / {len(ans.sources)} 条溯源；空结果已短路"


def check_api() -> str:
    from fastapi.testclient import TestClient
    from app.api.main import app

    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok", "/health 异常"
    r = client.post("/ask", json={"question": "七天无理由退款"})
    assert r.status_code == 401, f"无 token 应 401，实际 {r.status_code}"
    token = client.post("/auth/token", json={"tenant_id": "default"}).json()["access_token"]
    r = client.post("/ask", json={"question": "七天无理由退款"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"合法 token 应 200，实际 {r.status_code}: {r.text[:100]}"
    assert r.headers.get("x-trace-id"), "响应缺 X-Trace-Id"
    return "/health 正常；无 token 401；合法 token 200；trace_id 已回传"


def check_eval_gate() -> str:
    """评测 + 回归门禁：验证质量维度没退化（不只是'能跑'）。"""
    from app.eval.gate import compare_with_baseline
    from app.eval.runner import evaluate
    report = evaluate()
    gate = compare_with_baseline(report)
    if not gate.passed:
        bad = [f"{d.name} Δ{d.delta:+.3f}" for d in gate.deltas if not d.passed]
        raise AssertionError("质量退化: " + ", ".join(bad))
    tag = "（基线缺失，跳过对比）" if gate.baseline_missing else ""
    r3 = report.metrics.get("recall@3", 0)
    fa = report.metrics.get("faithfulness", 0)
    return (f"recall@3={r3:.2f} faithfulness={fa:.2f} "
            f"abstention={report.metrics.get('abstention_accuracy', 0):.2f} {tag}")


def check_tests() -> str:
    import subprocess
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=ROOT,
        capture_output=True, text=True, timeout=180,
    )
    tail = [l for l in proc.stdout.strip().splitlines() if l.strip()][-1]
    if proc.returncode != 0:
        raise AssertionError(f"pytest 失败: {tail}")
    return tail


def main() -> int:
    cli = argparse.ArgumentParser(description="本地全链路验证")
    cli.add_argument("--json", action="store_true", help="输出 JSON（CI 用）")
    cli.add_argument("--skip-tests", action="store_true", help="跳过 pytest")
    args = cli.parse_args()

    checker = Checker()
    checker.run("config", check_config)
    checker.run("ingest", check_ingest)
    checker.run("retrieve", check_retrieve)
    checker.run("generate", check_generate)
    checker.run("api", check_api)
    checker.run("eval", check_eval_gate)
    if not args.skip_tests:
        checker.run("tests", check_tests)

    if args.json:
        print(json.dumps({"ok": checker.all_ok, "checks": checker.results},
                         ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print("本地可验证报告")
        print("=" * 72)
        for r in checker.results:
            mark = "✓ PASS" if r["ok"] else "✗ FAIL"
            print(f"[{mark}] {r['name']:<10} {r['ms']:>7}ms  {r['detail']}")
        print("=" * 72)
        passed = sum(r["ok"] for r in checker.results)
        print(f"结果: {passed}/{len(checker.results)} 通过"
              + ("  —— 全部通过 ✓" if checker.all_ok else "  —— 存在失败，见上 ✗"))

    return 0 if checker.all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
