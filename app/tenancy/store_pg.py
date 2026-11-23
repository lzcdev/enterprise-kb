"""波次1：PostgreSQL 版租户存储（代码骨架，需装 psycopg2 + 起容器才运行）。

为什么把租户注册表从内存 dict 挪到 PostgreSQL？
    内存 dict 进程一停就没——租户配置是业务数据，必须持久化。
    PostgreSQL 提供事务、并发控制、备份恢复，是租户元数据的标准落点。

职责边界：
    本模块只负责【存取】TenantConfig（CRUD）；
    配置覆盖链、身份校验这些【业务逻辑】仍复用 registry.py 的 TenantRegistry。
    即：把 registry 的数据源从内存换成 PG，业务代码零改动——
    这正是"存储与逻辑分离"的价值，也是可拔插架构的兑现。

表结构（SQL 见 deploy/schema.sql）：
    tenants 一行一个租户，overrides/type_overrides/doc_overrides 用 JSONB 存，
    这样加新参数不用改表结构（schema-less 的灵活性 + 关系库的可靠性）。

生产改进点（这里没做，面试可讲）：
- 加 Redis 缓存租户配置，避免每次请求查库（租户配置读多写少，缓存命中率高）。
- 用连接池（psycopg2.pool）而非每次新建连接。
- 加行级权限/审计日志。
"""
import json
from typing import Optional

from app.tenancy.registry import TenantConfig


class PGTenantStore:
    """PostgreSQL 租户存储。懒加载 psycopg2，未安装时可 import 不报错。"""

    def __init__(self, dsn: str = "postgresql://kb_user:dev_password_change_me@localhost:5432/enterprise_kb"):
        self.dsn = dsn
        self._conn = None

    def _load(self):
        if self._conn is None:
            try:
                import psycopg2
            except ImportError as exc:
                raise RuntimeError(
                    "PG 租户存储需要 psycopg2-binary：pip install psycopg2-binary，"
                    "并先用 deploy/docker-compose.yml 起 postgres 容器。"
                ) from exc
            self._conn = psycopg2.connect(self.dsn)
        return self._conn

    def init_schema(self) -> None:
        """建表（生产用迁移工具 Alembic，这里 demo 直接建）。"""
        sql = """
        CREATE TABLE IF NOT EXISTS tenants (
            tenant_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            allowed_source_types JSONB,
            max_documents INTEGER,
            overrides JSONB NOT NULL DEFAULT '{}',
            type_overrides JSONB NOT NULL DEFAULT '{}',
            doc_overrides JSONB NOT NULL DEFAULT '{}'
        );
        """
        conn = self._load()
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()

    def upsert(self, cfg: TenantConfig) -> None:
        """插入或更新租户配置（ON CONFLICT 幂等，重试安全）。"""
        sql = """
        INSERT INTO tenants (tenant_id, name, status, allowed_source_types,
                             max_documents, overrides, type_overrides, doc_overrides)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id) DO UPDATE SET
            name = EXCLUDED.name, status = EXCLUDED.status,
            allowed_source_types = EXCLUDED.allowed_source_types,
            max_documents = EXCLUDED.max_documents,
            overrides = EXCLUDED.overrides,
            type_overrides = EXCLUDED.type_overrides,
            doc_overrides = EXCLUDED.doc_overrides;
        """
        conn = self._load()
        with conn.cursor() as cur:
            # JSONB 写入必须用 psycopg2.extras.Json 显式包装，原因有两个：
            #   1. list 会被 psycopg2 默认适配成 PG 的 text[] 数组，与 jsonb 列类型不匹配
            #      （实测报错：column is of type jsonb but expression is of type text[]）。
            #   2. Json() 会正确序列化嵌套 dict/list，并保证读回是 JSONB 对象而非转义字符串。
            # None 保持 SQL NULL：allowed_source_types=NULL 表示"不限制类型"。
            from psycopg2.extras import Json

            def j(value):
                return Json(value) if value is not None else None

            cur.execute(sql, (
                cfg.tenant_id, cfg.name, cfg.status,
                j(cfg.allowed_source_types),
                cfg.max_documents,
                j(cfg.overrides), j(cfg.type_overrides), j(cfg.doc_overrides),
            ))
        conn.commit()

    def close(self) -> None:
        """关闭数据库连接。

        为什么要有显式 close？进程退出时 OS 会回收连接，但测试里要删库、
        应用里要优雅停机——连接不关会导致 "database is being accessed by other users"。
        """
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get(self, tenant_id: str) -> Optional[TenantConfig]:
        """按 id 取租户配置。"""
        conn = self._load()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tenants WHERE tenant_id = %s", (tenant_id,))
            row = cur.fetchone()
        if not row:
            return None
        return self._row_to_config(row)

    def list_all(self) -> list[TenantConfig]:
        conn = self._load()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tenants ORDER BY tenant_id")
            rows = cur.fetchall()
        return [self._row_to_config(r) for r in rows]

    @staticmethod
    def _as_object(value):
        """JSONB 字段归一化为 Python 对象。

        关键坑：psycopg2 对 JSONB 列会【自动反序列化】——
        upsert 时写入 json.dumps({"a": 1})，读回来已经是 dict 而非字符串。
        所以不能再 json.loads(dict)，否则 TypeError。
        但有些驱动/连接配置下读回的是字符串，两种都要兼容。
        """
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)

    @staticmethod
    def _row_to_config(row) -> TenantConfig:
        """DB 行 → TenantConfig。列顺序必须与 SELECT * 的表定义一致。"""
        (tenant_id, name, status, allowed_types, max_docs,
         overrides, type_overrides, doc_overrides) = row
        return TenantConfig(
            tenant_id=tenant_id,
            name=name,
            status=status,
            allowed_source_types=PGTenantStore._as_object(allowed_types),
            max_documents=max_docs,
            overrides=PGTenantStore._as_object(overrides) or {},
            type_overrides=PGTenantStore._as_object(type_overrides) or {},
            doc_overrides=PGTenantStore._as_object(doc_overrides) or {},
        )
