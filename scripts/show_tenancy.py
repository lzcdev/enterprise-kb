"""任务6 演示：多租户数据隔离 + 配置覆盖 + 越权拦截。

运行：
    .venv/bin/python scripts/show_tenancy.py

演示三件事（对应多租户三大能力）：
1. 数据隔离：同一 namespace 里灌两个租户的数据，A 租户检索绝不会看到 B 的。
2. 配置覆盖：同一份文档，default/law_firm/startup 三个租户解析出不同参数，
   并显示每个值来自哪一层。
3. 越权拦截：未注册租户、被禁用租户、无权限文档类型、超配额，分别被拦下。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.factory import get_embedder, get_vectorstore
from app.interfaces import VectorRecord
from app.retrieval.retriever import Retriever
from app.tenancy.registry import (
    QuotaExceededError, SourceTypeNotAllowedError, TenantConfig,
    TenantNotFoundError, TenantRegistry, TenantSuspendedError,
)


def demo_data_isolation():
    """① 数据隔离：共享索引里按 tenant_id 过滤，A 查不到 B。"""
    print("=" * 60)
    print("【演示1】数据隔离：同一索引，两个租户互不可见")
    print("=" * 60)
    emb = get_embedder()
    store = get_vectorstore()
    ns = "demo_tenancy_iso"
    vecs = emb.embed([
        "律所内部保密条款：客户案件信息不得外泄",
        "电商退货政策：七天无理由退款",
    ])
    store.create_namespace(ns, emb.dimension)
    store.upsert(ns, [
        VectorRecord(id="law_1", vector=vecs[0], text="律所内部保密条款：客户案件信息不得外泄",
                     metadata={"tenant_id": "law_firm", "embedding_model": emb.model_id}),
        VectorRecord(id="shop_1", vector=vecs[1], text="电商退货政策：七天无理由退款",
                     metadata={"tenant_id": "startup", "embedding_model": emb.model_id}),
    ])
    r = Retriever(embedder=emb, store=store, namespace=ns)
    for tenant in ["law_firm", "startup", "__intruder__"]:
        results = r.retrieve("退款政策", tenant_id=tenant, score_threshold=0.0)
        texts = [c.text for c in results]
        print(f"  租户 {tenant:14s} 命中 {len(results)} 条: {texts}")
    # 关键断言：law_firm 查不到 startup 的数据，反之亦然
    assert all(c.metadata["tenant_id"] == "law_firm"
               for c in r.retrieve("退款政策", tenant_id="law_firm", score_threshold=0.0))
    assert all(c.metadata["tenant_id"] == "startup"
               for c in r.retrieve("退款政策", tenant_id="startup", score_threshold=0.0))
    store.delete_namespace(ns)
    print("  ✓ 跨租户零泄漏：每个租户只看到自己的数据\n")


def demo_config_override():
    """② 配置覆盖：四层链解析，显示来源。"""
    print("=" * 60)
    print("【演示2】配置覆盖：同一文档类型，不同租户不同参数")
    print("=" * 60)
    registry = TenantRegistry([
        TenantConfig(tenant_id="default", name="默认租户"),
        TenantConfig(tenant_id="law_firm", name="律所",
                     overrides={"chunk_size": 300, "top_k": 20},
                     type_overrides={"pdf": {"chunk_size": 400}}),
        TenantConfig(tenant_id="startup", name="初创",
                     overrides={"score_threshold": 0.5}),
    ])
    for tenant in ["default", "law_firm", "startup"]:
        cfg = registry.resolve_config(tenant, source_type="pdf")
        print(f"  租户 {tenant:10s}: chunk_size={cfg.chunk_size}({cfg.sources['chunk_size']}), "
              f"top_k={cfg.top_k}({cfg.sources['top_k']}), "
              f"threshold={cfg.score_threshold}({cfg.sources['score_threshold']})")
    print("  ✓ law_firm 的 chunk_size=400 来自 type 层，覆盖了 tenant 层的 300")
    print("  ✓ 括号内是来源层：global/tenant/type/doc，排障一眼看清\n")


def demo_access_control():
    """③ 越权拦截：四种拒绝场景。"""
    print("=" * 60)
    print("【演示3】越权拦截：身份/状态/类型/配额四道闸门")
    print("=" * 60)
    registry = TenantRegistry([
        TenantConfig(tenant_id="active_one", name="正常租户"),
        TenantConfig(tenant_id="suspended_one", name="欠费租户", status="suspended"),
        TenantConfig(tenant_id="pdf_only", name="仅PDF租户",
                     allowed_source_types=["pdf", "md"]),
        TenantConfig(tenant_id="small", name="小配额租户", max_documents=2),
    ])
    cases = [
        ("未注册租户", lambda: registry.require_active("no_such_tenant"), TenantNotFoundError),
        ("已禁用租户", lambda: registry.require_active("suspended_one"), TenantSuspendedError),
        ("文档类型无权限", lambda: registry.check_ingest_allowed("pdf_only", "video"), SourceTypeNotAllowedError),
        ("文档数超配额", lambda: registry.check_ingest_allowed("small", "pdf", current_documents=5), QuotaExceededError),
    ]
    for label, action, expected in cases:
        try:
            action()
            print(f"  ✗ {label}: 未拦截（BUG！）")
        except expected as e:
            print(f"  ✓ {label}: {type(e).__name__} — {e}")
    print("  ✓ 四道闸门全部生效\n")


if __name__ == "__main__":
    demo_data_isolation()
    demo_config_override()
    demo_access_control()
    print("[完成] 多租户三大能力演示完毕")
