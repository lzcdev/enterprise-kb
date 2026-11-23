"""任务6 单元测试：租户注册表、配置覆盖链、越权校验、API 身份闸门。"""
import pytest

from app.tenancy.registry import (
    QuotaExceededError, SourceTypeNotAllowedError, TenantConfig,
    TenantNotFoundError, TenantRegistry, TenantSuspendedError,
    _reset_registry,
)


def make_registry() -> TenantRegistry:
    """测试用注册表：覆盖三种典型租户。"""
    return TenantRegistry([
        TenantConfig(tenant_id="default", name="默认"),
        TenantConfig(tenant_id="law_firm", name="律所",
                     overrides={"chunk_size": 300, "top_k": 20},
                     type_overrides={"pdf": {"chunk_size": 400}}),
        TenantConfig(tenant_id="suspended", name="欠费", status="suspended"),
        TenantConfig(tenant_id="pdf_only", name="仅PDF",
                     allowed_source_types=["pdf", "md"], max_documents=2),
    ])


def test_global_defaults_and_tenant_override():
    """全局默认 + 租户级覆盖：只改差异键，其余继承。"""
    reg = make_registry()
    base = reg.resolve_config("default", source_type="pdf")
    assert base.chunk_size == 500 and base.sources["chunk_size"] == "global"

    law = reg.resolve_config("law_firm", source_type="txt")  # txt 无类型覆盖
    assert law.chunk_size == 300 and law.sources["chunk_size"] == "tenant"
    assert law.top_k == 20 and law.sources["top_k"] == "tenant"
    assert law.top_n == 3 and law.sources["top_n"] == "global"  # 未覆盖的继承默认


def test_type_layer_overrides_tenant_layer():
    """类型层优先级高于租户层：pdf 的 400 盖掉租户的 300。"""
    reg = make_registry()
    pdf = reg.resolve_config("law_firm", source_type="pdf")
    assert pdf.chunk_size == 400
    assert pdf.sources["chunk_size"] == "type"
    # 换成 doc 级覆盖再盖一层
    reg.get("law_firm").doc_overrides["doc_1"] = {"chunk_size": 200}
    doc = reg.resolve_config("law_firm", source_type="pdf", doc_id="doc_1")
    assert doc.chunk_size == 200 and doc.sources["chunk_size"] == "doc"


def test_unknown_override_key_ignored():
    """拼错的参数键应被忽略，不污染配置。"""
    reg = make_registry()
    reg.get("default").overrides["chunk_szie"] = 999  # 拼错
    cfg = reg.resolve_config("default")
    assert cfg.chunk_size == 500
    assert "chunk_szie" not in cfg.as_dict()


def test_identity_check():
    """身份校验：未注册抛 NotFound，禁用抛 Suspended。"""
    reg = make_registry()
    assert reg.require_active("default").tenant_id == "default"
    with pytest.raises(TenantNotFoundError):
        reg.require_active("ghost")
    with pytest.raises(TenantSuspendedError):
        reg.require_active("suspended")


def test_ingest_permission_and_quota():
    """建库前置校验：类型权限 + 文档配额。"""
    reg = make_registry()
    reg.check_ingest_allowed("pdf_only", "pdf", current_documents=1)  # 正常
    with pytest.raises(SourceTypeNotAllowedError):
        reg.check_ingest_allowed("pdf_only", "video")
    with pytest.raises(QuotaExceededError):
        reg.check_ingest_allowed("pdf_only", "pdf", current_documents=2)


def test_resolve_config_safe_degrades():
    """软解析：未注册租户降级到全局默认，不抛异常。"""
    reg = make_registry()
    cfg = reg.resolve_config_safe("ghost", source_type="pdf")
    assert cfg.chunk_size == 500
    assert all(src == "global" for src in cfg.sources.values())


def test_api_enforces_tenant_identity():
    """API 身份闸门：未注册 404，已注册 200。"""
    from fastapi.testclient import TestClient
    from app.api.main import app
    client = TestClient(app)
    r = client.post("/ask", json={"question": "七天无理由退款", "tenant_id": "never_registered"})
    assert r.status_code == 404
    assert r.json()["code"] == "TENANT_NOT_FOUND"
    r = client.post("/ask", json={"question": "七天无理由退款", "tenant_id": "default"})
    assert r.status_code == 200
    assert "answer" in r.json()


def test_api_config_endpoint_shows_sources():
    """管理接口：配置查询带来源标记。"""
    from fastapi.testclient import TestClient
    from app.api.main import app
    client = TestClient(app)
    data = client.get("/tenants/law_firm/config", params={"source_type": "pdf"}).json()
    assert data["chunk_size"] == 400
    assert data["sources"]["chunk_size"] == "type"
