-- PostgreSQL 租户元数据表结构
-- 对应 app/tenancy/store_pg.py 的 init_schema() 与 _row_to_config() 列顺序。
-- 两种执行方式：
--   1) docker-compose 首次启动时自动执行：挂载到 /docker-entrypoint-initdb.d/（见 docker-compose.yml）
--   2) 手动执行：psql -h localhost -U kb_user -d enterprise_kb -f deploy/schema.sql
-- 生产环境应改用 Alembic 管理迁移（版本化、可回滚），这里用纯 SQL 是为了零额外依赖。

-- 列顺序至关重要：store_pg.py 的 _row_to_config() 用 SELECT * 按位置解包。
-- 新增列请追加在末尾，并同步改 _row_to_config()，否则列错位会导致配置串台。
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id           TEXT PRIMARY KEY,
    name                TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'active',   -- active / suspended
    allowed_source_types JSONB,                           -- NULL 表示不限制；JSON 数组
    max_documents       INTEGER,                          -- NULL 表示不限
    overrides           JSONB NOT NULL DEFAULT '{}',      -- 租户级差异覆盖
    type_overrides      JSONB NOT NULL DEFAULT '{}',      -- {'pdf': {...}, 'video': {...}}
    doc_overrides       JSONB NOT NULL DEFAULT '{}'       -- {'doc_id': {...}}
);

-- 状态过滤索引：租户列表/校验高频按 status 查，状态值少，B-tree 足够。
CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants (status);

-- 初始租户：与 app/tenancy/registry.py 的 default_registry() 保持一致。
-- ON CONFLICT DO NOTHING 保证重复执行幂等（docker 容器重建时不会报错/覆盖自定义配置）。
INSERT INTO tenants (tenant_id, name, status, allowed_source_types, max_documents,
                     overrides, type_overrides, doc_overrides) VALUES
    ('default', '默认租户', 'active', NULL, NULL, '{}', '{}', '{}'),
    ('law_firm', '某某律师事务所', 'active', NULL, NULL,
     '{"chunk_size": 300, "chunk_overlap": 30, "top_k": 20}',
     '{"pdf": {"chunk_size": 400}}', '{}'),
    ('startup', '初创公司', 'active', '["pdf", "md", "txt"]', 50, '{}', '{}', '{}')
ON CONFLICT (tenant_id) DO NOTHING;
