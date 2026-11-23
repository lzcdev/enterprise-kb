"""全链路追踪：用 contextvars 传递 trace_id，结构化 JSON 日志输出。

为什么需要 trace_id？
    一次用户请求会经过 API → RAGService → Retriever → Generator 多个环节。
    并发时几十个请求的日志混在一起，没有统一 id 根本分不清哪行属于哪次请求。
    给每个请求发一个 trace_id，所有日志都带上它，就能用 grep 拉出完整调用链。

为什么用 contextvars 而不是参数一路传？
    参数一路传要改每个函数签名，侵入性大。contextvars 是"上下文局部"变量：
    在每个请求/任务里有独立的值，异步(asyncio)和多线程都安全，且不用改业务函数签名。
    这是 Python 3.7+ 的标准方案（logging、OpenTelemetry 的 context 也基于它）。

生产升级路径：这里用标准库 logging 输出 JSON；上规模后接 OpenTelemetry +
Jaeger/Tempo，trace_id 会变成完整的 span 树，能看每个环节的时间瀑布图。
本模块的接口设计成"取 trace_id"这一个函数，将来换 OTel 只改这里。
"""
import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Optional

# ContextVar：每个执行上下文有独立值。默认值 None 表示还没进入请求上下文。
_trace_id: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)

logger = logging.getLogger("enterprise_kb")
logger.setLevel(logging.INFO)
if not logger.handlers:
    # 日志走 stderr，程序输出走 stdout —— 标准 Unix 约定。
    # 这样 CI/脚本把 stdout 重定向给解析器（如 verify_local.py --json）时，
    # 结构化日志不会混进机器可读输出；容器日志采集也只盯 stderr 不污染业务输出。
    _handler = logging.StreamHandler(sys.stderr)
    logger.addHandler(_handler)


def new_trace_id() -> str:
    """请求入口调用：生成并绑定一个 trace_id，返回它。"""
    tid = uuid.uuid4().hex[:16]
    _trace_id.set(tid)
    return tid


def get_trace_id() -> Optional[str]:
    """任意环节调用：取当前上下文的 trace_id。"""
    return _trace_id.get()


def log_event(stage: str, *, tenant_id: Optional[str] = None,
              elapsed_ms: Optional[float] = None, **extra):
    """打一条结构化日志（JSON 行）。

    JSON 而非裸字符串：机器可解析，采集到 ELK/Loki 后能按 stage、tenant_id 聚合。
    每次调用都重新读 _trace_id，保证拿到的是当前请求的值。
    """
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "trace_id": get_trace_id(),
        "stage": stage,
    }
    if tenant_id is not None:
        record["tenant_id"] = tenant_id
    if elapsed_ms is not None:
        record["elapsed_ms"] = round(elapsed_ms, 2)
    record.update(extra)
    logger.info(json.dumps(record, ensure_ascii=False))
