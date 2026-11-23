"""发布控制包：蓝绿切流、灰度放量、影子读、秒级回滚。

对外暴露 ReleaseController / ReleaseError / ReleaseState。
状态存储是可替换的（JsonFileStore → Nacos/Apollo），见 state.py。
"""
from app.release.controller import ReleaseController, ReleaseError
from app.release.state import (
    ReleaseState,
    ReleaseStateStore,
    JsonFileStore,
    set_release_store,
    get_release_store,
)

__all__ = [
    "ReleaseController",
    "ReleaseError",
    "ReleaseState",
    "ReleaseStateStore",
    "JsonFileStore",
    "set_release_store",
    "get_release_store",
]
