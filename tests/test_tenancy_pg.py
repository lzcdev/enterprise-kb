"""PostgreSQL 租户存储集成测试（波次1）。

设计要点：
- 默认跳过：CI 里没有 PG 服务，用 pytest.mark.skipif 按环境变量 KB_PG_TEST=1 启用。
  这样 CI 轻量依赖跑 75 项测试不受影响，本地有 PG 时手动开启集成测试。
- 覆盖两个真实踩过的 bug：
  1. JSONB 读回已是 dict/list，不能再 json.loads（_as_object 归一化）。
  2. list 写入必须用 psycopg2.extras.Json 包装，否则 PG 报 text[] vs jsonb 类型不匹配。
- 每个测试用独立库，避免互相污染。
"""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KB_PG_TEST") != "1",
    reason="需要本机 PostgreSQL；设置 KB_PG_TEST=1 启用",
)

BASE_DSN = os.environ.get("KB_PG_TEST_DSN", "postgresql://lzc@127.0.0.1:5432")


@pytest.fixture
def pg_store():
    """每个测试建独立库，测完删库——测试之间没有共享状态。"""
    import psycopg2
    from app.tenancy.store_pg import PGTenantStore

    db_name = f"kb_pg_test_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(BASE_DSN + "/postgres")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{db_name}"')
    admin.close()

    dsn = f"{BASE_DSN}/{db_name}"
    store = PGTenantStore(dsn=dsn)
    store.init_schema()
    try:
        yield store
    finally:
        # 必须先关掉 store 持有的连接，否则 DROP DATABASE 报 "being accessed by other users"
        store.close()
        admin = psycopg2.connect(BASE_DSN + "/postgres")
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        admin.close()


def test_upsert_and_get_roundtrip(pg_store):
    """写入再读回：验证 JSONB 序列化/反序列化两端都正确。"""
    from app.tenancy.registry import TenantConfig

    cfg = TenantConfig(
        tenant_id="acme", name="ACME 公司", status="active",
        allowed_source_types=["pdf", "md"], max_documents=100,
        overrides={"chunk_size": 600, "top_k": 15},
        type_overrides={"pdf": {"chunk_size": 800}},
    )
    pg_store.upsert(cfg)

    got = pg_store.get("acme")
    assert got is not None
    assert got.name == "ACME 公司"
    assert got.allowed_source_types == ["pdf", "md"]  # JSONB list 读回仍是 list
    assert got.overrides == {"chunk_size": 600, "top_k": 15}
    assert got.type_overrides == {"pdf": {"chunk_size": 800}}


def test_jsonb_types_not_string(pg_store):
    """回归 bug #1：JSONB 字段读回不能是字符串（否则覆盖链会拿到 str）。"""
    from app.tenancy.registry import TenantConfig

    pg_store.upsert(TenantConfig(
        tenant_id="typecheck",
        overrides={"chunk_size": 123},
        type_overrides={"video": {"chunk_overlap": 20}},
    ))
    got = pg_store.get("typecheck")
    assert isinstance(got.overrides, dict), f"overrides 应读回 dict，实际 {type(got.overrides)}"
    assert isinstance(got.type_overrides, dict)
    assert isinstance(got.doc_overrides, dict)


def test_allowed_source_types_jsonb_not_array(pg_store):
    """回归 bug #2：list 类型必须作为 JSONB 存入，而非 PG text[]。"""
    from app.tenancy.registry import TenantConfig

    pg_store.upsert(TenantConfig(
        tenant_id="listcheck", allowed_source_types=["pdf", "docx", "md"],
    ))
    got = pg_store.get("listcheck")
    assert got.allowed_source_types == ["pdf", "docx", "md"]

    # 从数据库层面确认列类型是 jsonb 而非 text[]
    with pg_store._load().cursor() as cur:
        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='tenants' AND column_name='allowed_source_types'"
        )
        assert cur.fetchone()[0] == "jsonb"


def test_upsert_is_idempotent(pg_store):
    """ON CONFLICT 幂等：重复 upsert 同一租户应更新而非报错。"""
    from app.tenancy.registry import TenantConfig

    pg_store.upsert(TenantConfig(tenant_id="idem", name="第一版", max_documents=10))
    pg_store.upsert(TenantConfig(tenant_id="idem", name="第二版", max_documents=20))
    assert len(pg_store.list_all()) == 1
    assert pg_store.get("idem").name == "第二版"
    assert pg_store.get("idem").max_documents == 20


def test_get_missing_returns_none(pg_store):
    assert pg_store.get("不存在") is None
