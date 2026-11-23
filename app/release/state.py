"""发布状态与存储抽象：把"配置中心"抽象成可替换的协议。

为什么要抽象？
- 本地/教学：用 JSON 文件模拟配置中心，进程间可读、原子写入、零依赖。
- 生产：换成 Nacos / Apollo / etcd，只换实现类，控制器代码不动。
  这就是契约层（Protocol）在"发布控制"这个领域的复用——和 Embedder/VectorStore 同一套思想。

原子写入为什么重要？
切流时如果直接覆写 state.json，读到一半的进程可能拿到半个 JSON 而崩溃。
做法是写临时文件 + os.replace()（POSIX 保证原子），读者要么看到旧状态、要么看到新状态，
没有中间态——这就是"原子切流"在配置层的落地。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.config import settings


def utc_now() -> str:
    """统一时间格式：UTC ISO8601，便于跨时区排障。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ReleaseState:
    """一次蓝绿发布的完整状态快照。

    blue / green: 两套索引的 namespace（编码了 embedding 模型版本）。
    active: "blue" 或 "green"，指向当前主服务索引。
    canary_percent: 灰度期绿索引接收的流量百分比（0=全蓝，100=全绿）。
    history: 审计日志——谁在什么时候做了什么，配置中心必备。
    """

    blue: str
    green: str | None = None
    active: str = "blue"
    canary_percent: int = 0
    updated_at: str = ""
    history: list[dict] = field(default_factory=list)

    def effective_namespace(self) -> str:
        """当前"主"namespace：全量切流后返回绿，否则返回蓝。"""
        if self.active == "green" and self.green:
            return self.green
        return self.blue

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ReleaseState":
        """只取已知字段：配置中心可能往后加字段，旧版本要能容忍。"""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@runtime_checkable
class ReleaseStateStore(Protocol):
    """配置中心契约。换 Nacos/Apollo 时实现这两个方法即可。"""

    def load(self) -> ReleaseState | None: ...

    def save(self, state: ReleaseState) -> None: ...


class JsonFileStore:
    """本地文件版配置中心：零依赖、可读、原子写入。"""

    def __init__(self, path: Path | None = None):
        self.path = path or (settings.DATA_DIR / "release" / "state.json")

    def load(self) -> ReleaseState | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return ReleaseState.from_dict(data)
        except (json.JSONDecodeError, OSError, TypeError):
            # 文件损坏时返回 None，让调用方回退到默认 namespace，而不是把服务打挂。
            # 生产环境这里应该告警——静默回退可能掩盖配置中心故障。
            return None

    def save(self, state: ReleaseState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())  # 先落盘，再原子替换，防止断电写到一半
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


_default_store: JsonFileStore | None = None


def get_release_store() -> JsonFileStore:
    """全局单例 store：让同进程内的控制器看到同一份状态。"""
    global _default_store
    if _default_store is None:
        _default_store = JsonFileStore()
    return _default_store


def set_release_store(store: ReleaseStateStore | None) -> None:
    """注入替换实现（测试用 tmp 文件，生产用配置中心客户端）。"""
    global _default_store
    _default_store = store  # type: ignore[assignment]
