"""蓝绿发布控制器：namespace 版本化 → 影子读 → 灰度放量 → 原子切流 → 秒级回滚。

这个类是把 migrate_embedding.py 里的"教学伪代码"变成真正可执行的控制流。
核心工程价值：**切流只是改一个字符串**（ACTIVE_NAMESPACE 指向哪个 namespace），
因此回滚也是改回这个字符串——秒级、无需重建数据、无需重启服务。

三个关键设计：
1. 确定性分流：用 hash(tenant_id) 分桶，同一租户始终走同一版本。
   不能用随机数，否则同一用户两次提问结果可能不一样，客服会疯掉。
2. 护栏：不允许从 0% 直接跳到 100%（除非显式 force），
   也不允许没做过影子验证就切流——把"流程"编码进代码，而不是靠人记得。
3. 审计历史：每次动作写进 state.history，出问题能复盘谁在什么时候切了什么。
"""
from __future__ import annotations

import hashlib
from typing import Callable

from app.release.state import ReleaseState, ReleaseStateStore, get_release_store, utc_now


class ReleaseError(RuntimeError):
    """发布流程违规：护栏拦住了危险操作。"""


class ReleaseController:
    """蓝绿发布状态机。

    namespace_exists: 检查目标 namespace 是否真的有数据——防止切到一个空索引上，
    这是"切流成功但检索全空"这类事故的根源。生产里可接向量库的 list_namespaces()。
    """

    def __init__(
        self,
        store: ReleaseStateStore | None = None,
        namespace_exists: Callable[[str], bool] | None = None,
    ):
        self.store = store or get_release_store()
        # 默认放行：调用方没提供校验器时不阻断（本地演示方便），生产必须注入真实校验。
        self._namespace_exists = namespace_exists or (lambda ns: True)

    # ---------- 状态读取 ----------

    def status(self) -> ReleaseState | None:
        """每次读都从 store 重新加载——模拟配置中心的"最新值"，不缓存。"""
        return self.store.load()

    def effective_namespace(self) -> str:
        """给检索层用的当前主 namespace。未初始化时给出明确错误而不是静默默认值。"""
        state = self.status()
        if state is None:
            raise ReleaseError("发布状态未初始化，请先调用 init(blue_ns)")
        return state.effective_namespace()

    # ---------- 状态变更 ----------

    def init(self, blue_ns: str, green_ns: str | None = None) -> ReleaseState:
        """初始化发布状态（幂等：已有状态就不覆盖，避免误操作冲掉灰度进度）。"""
        state = self.store.load()
        if state is not None:
            return state
        state = ReleaseState(blue=blue_ns, green=green_ns, updated_at=utc_now())
        self._record(state, "init", {"blue": blue_ns, "green": green_ns})
        self.store.save(state)
        return state

    def prepare(self, green_ns: str) -> ReleaseState:
        """步骤1：绿索引就绪。此时 active 仍指向蓝，线上流量完全不受影响。"""
        state = self._require()
        if not self._namespace_exists(green_ns):
            raise ReleaseError(f"绿索引不存在或为空，拒绝登记: {green_ns}")
        if state.green and state.green != green_ns and state.active == "green":
            raise ReleaseError("当前正在服务旧绿索引，不允许直接改绿索引名（先回滚）")
        state.green = green_ns
        state.canary_percent = 0
        self._record(state, "prepare", {"green": green_ns})
        self.store.save(state)
        return state

    def shadow_compare(
        self,
        queries: list[str],
        search_fn: Callable[[str, str], list[str]],
        *,
        top_k: int = 5,
    ) -> dict:
        """步骤2：影子读对比。线上只返回蓝的结果，绿的结果仅用于评估。

        search_fn(query, namespace) -> 命中的 chunk id 列表。
        用 Top-K 重合率作为粗指标：重合太低说明新模型行为漂移大，要查原因；
        但重合高也不等于质量好——所以生产还要配合评测集（run_eval.py）看语义指标。
        """
        state = self._require()
        if not state.green:
            raise ReleaseError("尚未登记绿索引，无法影子读")

        overlaps: list[float] = []
        for q in queries:
            blue_ids = set(search_fn(q, state.blue)[:top_k])
            green_ids = set(search_fn(q, state.green)[:top_k])
            union = blue_ids | green_ids
            overlaps.append(len(blue_ids & green_ids) / len(union) if union else 1.0)

        avg = sum(overlaps) / max(len(overlaps), 1)
        return {
            "avg_overlap": round(avg, 4),
            "sample_size": len(queries),
            "per_query": [round(x, 4) for x in overlaps],
            "blue": state.blue,
            "green": state.green,
        }

    def set_canary(self, percent: int, *, force: bool = False) -> ReleaseState:
        """步骤3：灰度放量。绿索引接收 percent% 的流量，蓝索引承接其余。

        护栏：
        - 0..100 之外的值直接拒绝（配置写错是常见事故源）
        - 必须先 prepare 绿索引
        - 单次放量不超过 50 个百分点（0→100 必须 force），强制分步观察
        """
        state = self._require()
        if not 0 <= percent <= 100:
            raise ReleaseError(f"灰度比例必须在 0..100，收到 {percent}")
        if not state.green:
            raise ReleaseError("尚未登记绿索引，不能放量")
        if state.active == "green":
            raise ReleaseError("已全量切到绿索引，灰度阶段结束；要回退请 rollback()")

        step = abs(percent - state.canary_percent)
        if step > 50 and not force:
            raise ReleaseError(
                f"单次放量 {step}% 超过安全步长 50%（{state.canary_percent}% → {percent}%）；"
                "如确需跳级请显式 force=True"
            )

        state.canary_percent = percent
        self._record(state, "set_canary", {"percent": percent, "force": force})
        self.store.save(state)
        return state

    def cut_over(self, *, force: bool = False) -> ReleaseState:
        """步骤4：原子切流。把 active 指向绿，canary 收口为 100。"""
        state = self._require()
        if not state.green:
            raise ReleaseError("尚未登记绿索引，不能切流")
        if state.active == "green":
            return state  # 幂等：已经切过了就直接返回
        if state.canary_percent < 100 and not force:
            raise ReleaseError(
                f"灰度仅到 {state.canary_percent}%，请先放量到 100% 观察；"
                "如确需直接切流请显式 force=True"
            )
        state.active = "green"
        state.canary_percent = 100
        self._record(state, "cut_over", {"green": state.green, "force": force})
        self.store.save(state)
        return state

    def rollback(self) -> ReleaseState:
        """步骤5：秒级回滚。指针改回蓝，绿索引保留供排障——不删数据。"""
        state = self._require()
        state.active = "blue"
        state.canary_percent = 0
        self._record(state, "rollback", {"blue": state.blue})
        self.store.save(state)
        return state

    # ---------- 分流 ----------

    def select_namespace(self, tenant_id: str, *, salt: str = "") -> str:
        """按租户确定性分流：同一租户稳定命中同一版本。

        为什么用 md5 而不是 Python 内置 hash()？
        内置 hash 对字符串做了随机盐（PYTHONHASHSEED），进程重启后结果会变——
        灰度用户会在重启后"跳版本"。md5 跨进程稳定，这是分流函数的硬要求。
        """
        state = self._require()
        if state.active == "green":
            return state.green or state.blue
        pct = state.canary_percent
        if pct <= 0:
            return state.blue
        if pct >= 100:
            return state.green or state.blue
        if not state.green:
            raise ReleaseError("灰度比例 > 0 但没有绿索引，状态不一致")

        key = f"{tenant_id}:{salt}".encode("utf-8")
        bucket = int.from_bytes(hashlib.md5(key).digest()[:2], "big") % 100
        return state.green if bucket < pct else state.blue

    # ---------- 内部 ----------

    def _require(self) -> ReleaseState:
        state = self.status()
        if state is None:
            raise ReleaseError("发布状态未初始化，请先调用 init(blue_ns)")
        return state

    @staticmethod
    def _record(state: ReleaseState, action: str, detail: dict) -> None:
        """追加审计记录，只保留最近 50 条防止状态文件无限膨胀。"""
        state.history.append({"at": utc_now(), "action": action, **detail})
        state.history = state.history[-50:]
        state.updated_at = utc_now()
