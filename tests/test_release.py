"""蓝绿发布控制测试：状态机、护栏、确定性分流、工厂切流接线。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.factory import active_namespace
from app.release import (
    JsonFileStore,
    ReleaseController,
    ReleaseError,
    set_release_store,
)


@pytest.fixture
def isolated_store():
    """每个测试用独立临时配置中心，结束后恢复全局单例。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonFileStore(Path(tmp) / "state.json")
        set_release_store(store)
        yield store
        set_release_store(None)


def _controller(**kwargs) -> ReleaseController:
    return ReleaseController(**kwargs)


def test_init_is_idempotent(isolated_store):
    c = _controller()
    first = c.init("kb__blue__v1")
    second = c.init("kb__other__v9")
    assert second.blue == "kb__blue__v1"  # 不覆盖已有状态


def test_prepare_rejects_missing_namespace(isolated_store):
    c = _controller(namespace_exists=lambda ns: ns == "kb__green__v2")
    c.init("kb__blue__v1")
    with pytest.raises(ReleaseError, match="绿索引不存在"):
        c.prepare("kb__missing__v3")
    assert c.prepare("kb__green__v2").green == "kb__green__v2"


def test_shadow_compare_computes_jaccard(isolated_store):
    c = _controller()
    c.init("kb__blue__v1")
    c.prepare("kb__green__v2")

    def search(query: str, ns: str) -> list[str]:
        return ["a", "b", "c"] if ns == "kb__blue__v1" else ["b", "c", "d"]

    result = c.shadow_compare(["q1", "q2"], search)
    # 交集 {b,c}=2，并集 {a,b,c,d}=4 → 0.5
    assert result["avg_overlap"] == 0.5
    assert result["sample_size"] == 2
    assert result["per_query"] == [0.5, 0.5]


def test_shadow_requires_green(isolated_store):
    c = _controller()
    c.init("kb__blue__v1")
    with pytest.raises(ReleaseError, match="尚未登记绿索引"):
        c.shadow_compare(["q"], lambda q, ns: [])


def test_canary_is_deterministic_and_bounded(isolated_store):
    c = _controller()
    c.init("kb__blue__v1")
    c.prepare("kb__green__v2")
    c.set_canary(20)

    # 同一租户必须稳定命中同一版本（hash 不随进程/调用变化）
    for tenant in ("A", "B", "tenant_7"):
        assert c.select_namespace(tenant) == c.select_namespace(tenant)

    hits = sum(
        1 for i in range(500)
        if c.select_namespace(f"t{i}").endswith("__v2")
    )
    assert 60 < hits < 140  # 20% 上下浮动，确定性哈希不会精确等于 100


def test_canary_guard_blocks_big_jump(isolated_store):
    c = _controller()
    c.init("kb__blue__v1")
    c.prepare("kb__green__v2")
    with pytest.raises(ReleaseError, match="安全步长"):
        c.set_canary(100)
    assert c.status().canary_percent == 0  # 被拒后状态不变

    c.set_canary(100, force=True)
    assert c.status().canary_percent == 100


def test_canary_rejects_invalid_percent(isolated_store):
    c = _controller()
    c.init("kb__blue__v1")
    c.prepare("kb__green__v2")
    for bad in (-1, 101):
        with pytest.raises(ReleaseError, match="0..100"):
            c.set_canary(bad)


def test_cutover_requires_full_canary_unless_force(isolated_store):
    c = _controller()
    c.init("kb__blue__v1")
    c.prepare("kb__green__v2")
    c.set_canary(10)
    with pytest.raises(ReleaseError, match="灰度仅到"):
        c.cut_over()
    assert c.status().active == "blue"

    c.set_canary(50)   # 护栏强制分步：10 → 50 → 100
    c.set_canary(100)
    assert c.cut_over().active == "green"
    assert c.cut_over().active == "green"  # 幂等


def test_rollback_preserves_green(isolated_store):
    c = _controller()
    c.init("kb__blue__v1")
    c.prepare("kb__green__v2")
    c.set_canary(50)
    c.set_canary(100)
    c.cut_over()
    state = c.rollback()
    assert state.active == "blue"
    assert state.canary_percent == 0
    assert state.green == "kb__green__v2"  # 回滚不删绿索引，留着排障


def test_factory_follows_release_state(isolated_store, monkeypatch):
    """切流/回滚必须让检索层真实换 namespace，否则发布只是纸面状态。"""
    from app.config import settings

    monkeypatch.setattr(settings, "ACTIVE_NAMESPACE", None)
    c = _controller()
    c.init("kb__blue__v1")
    assert active_namespace() == "kb__blue__v1"

    c.prepare("kb__green__v2")
    c.set_canary(50)
    assert active_namespace() == "kb__blue__v1"  # 灰度期主索引仍是蓝

    c.set_canary(100)
    c.cut_over()
    assert active_namespace() == "kb__green__v2"

    c.rollback()
    assert active_namespace() == "kb__blue__v1"


def test_audit_history_records_every_action(isolated_store):
    c = _controller()
    c.init("b")
    c.prepare("g")
    c.set_canary(10)
    c.set_canary(50)
    c.set_canary(100)
    c.cut_over()
    c.rollback()
    actions = [h["action"] for h in c.status().history]
    # 三步放量留下三条审计——护栏强制分步，每一步都可追溯
    assert actions == [
        "init", "prepare",
        "set_canary", "set_canary", "set_canary",
        "cut_over", "rollback",
    ]


def test_corrupted_state_falls_back_to_none(isolated_store):
    """配置中心文件损坏时不能把服务打挂，应回退到默认 namespace。"""
    isolated_store.path.write_text("{broken json", encoding="utf-8")
    assert _controller().status() is None


def test_state_roundtrip_ignores_unknown_keys(isolated_store):
    """配置中心未来加字段，旧版本要能容忍（向前兼容）。"""
    isolated_store.path.parent.mkdir(parents=True, exist_ok=True)
    isolated_store.path.write_text(
        json.dumps({"blue": "b", "future_field": 123}),
        encoding="utf-8",
    )
    state = _controller().status()
    assert state is not None
    assert state.blue == "b"
    assert not hasattr(state, "future_field")
