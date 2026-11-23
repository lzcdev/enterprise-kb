"""波次1 持久化验证：证明租户配置在"服务重启"后仍然存在。

为什么要用两个独立进程模拟重启？
    如果只在一个进程里 store.upsert() 再 store.get()，内存 dict 也能通过——
    那不是持久化，只是"没删掉"。真正的重启是【进程结束、内存清空、重新连接数据库】，
    只有用两个独立进程跑，才能证明数据真的落到了磁盘上的 PG。

用法：
    # 阶段1 写入：插入一个带独特标记的租户
    .venv/bin/python scripts/verify_pg_persistence.py write

    # 阶段2 读取：新进程从 PG 读回，核对标记还在
    .venv/bin/python scripts/verify_pg_persistence.py read

    # 一键跑完两阶段（内部仍是两个子进程）
    .venv/bin/python scripts/verify_pg_persistence.py
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DSN = os.environ.get(
    "PG_DSN", "postgresql://lzc@127.0.0.1:5432/enterprise_kb_test"
)
MARKER_TENANT = "persist_probe"
MARKER_OVERRIDE = {"chunk_size": 777, "top_k": 42}  # 独特的自定义值，用于核对


def _make_registry():
    """用 PG 后端构造注册表。设置环境变量让 config 读到 pg 配置。"""
    os.environ["TENANT_STORE_BACKEND"] = "pg"
    os.environ["PG_DSN"] = DSN
    # config.settings 是模块级单例，需在子进程启动前设好环境变量；
    # 这里重新 import 以确保拿到新配置（同一进程内二次调用时）。
    from app.config import Settings
    from app.tenancy.registry import TenantRegistry
    from app.tenancy.store_pg import PGTenantStore

    store = PGTenantStore(dsn=DSN)
    store.init_schema()
    return TenantRegistry(store=store)


def write_phase() -> int:
    """阶段1：写入带独特标记的租户配置。"""
    from app.tenancy.registry import TenantConfig

    registry = _make_registry()
    registry.register(TenantConfig(
        tenant_id=MARKER_TENANT,
        name="持久化探针",
        status="active",
        overrides=MARKER_OVERRIDE,
    ))
    cfg = registry.get(MARKER_TENANT)
    assert cfg is not None, "写入后立即读取失败"
    assert cfg.overrides["chunk_size"] == 777, "写入的值不对"
    print(f"[write] ✓ 已写入租户 {MARKER_TENANT!r}，overrides={cfg.overrides}")
    print("[write] 进程即将结束——内存数据会清空，只有 PG 里的记录留存")
    return 0


def read_phase() -> int:
    """阶段2：全新进程从 PG 读回，核对配置。"""
    registry = _make_registry()
    cfg = registry.get(MARKER_TENANT)
    if cfg is None:
        print(f"[read] ✗ 租户 {MARKER_TENANT!r} 不存在——持久化失败！")
        return 1
    expected = MARKER_OVERRIDE["chunk_size"]
    actual = cfg.overrides.get("chunk_size") if isinstance(cfg.overrides, dict) else None
    if actual != expected:
        print(f"[read] ✗ chunk_size 期望 {expected}，实际 {actual}——数据串台！")
        return 1

    # 顺带验证覆盖链：租户级 override 应该生效（chunk_size 来自 tenant 层）
    resolved = registry.resolve_config(MARKER_TENANT)
    if resolved.chunk_size != expected or resolved.sources.get("chunk_size") != "tenant":
        print(f"[read] ✗ 覆盖链异常: chunk_size={resolved.chunk_size}, "
              f"source={resolved.sources.get('chunk_size')}")
        return 1

    print(f"[read] ✓ 新进程从 PG 读回 {MARKER_TENANT!r}: "
          f"name={cfg.name!r}, overrides={cfg.overrides}")
    print(f"[read] ✓ 覆盖链生效: chunk_size={resolved.chunk_size} "
          f"(来源={resolved.sources['chunk_size']})")
    print("[read] ✓ 进程重启后配置仍在 —— 持久化成功")
    return 0


def run_both() -> int:
    """一键跑：用两个独立的 python 子进程模拟真正的服务重启。"""
    print("=" * 64)
    print("波次1 持久化验证：两个独立进程 = 模拟服务重启")
    print("=" * 64)
    python = sys.executable
    script = str(Path(__file__).resolve())
    env = dict(os.environ, PG_DSN=DSN)

    print("\n--- 阶段1：写入（进程 A）---")
    r1 = subprocess.run([python, script, "write"], env=env)
    if r1.returncode != 0:
        print("写入阶段失败，终止")
        return 1

    print("\n--- 进程 A 已退出，内存清空 ---")
    print("--- 阶段2：读取（全新进程 B）---")
    r2 = subprocess.run([python, script, "read"], env=env)
    if r2.returncode != 0:
        print("读取阶段失败：数据没有持久化")
        return 1

    print("\n" + "=" * 64)
    print("结果: ✓ 跨进程持久化验证通过 —— 重启后租户配置不丢")
    print("=" * 64)
    return 0


def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else "both"
    if phase == "write":
        return write_phase()
    if phase == "read":
        return read_phase()
    if phase == "both":
        return run_both()
    print(f"未知阶段: {phase!r}，应为 write / read / both")
    return 2


if __name__ == "__main__":
    sys.exit(main())
