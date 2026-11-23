"""蓝绿发布五步演示：初始化 → 建绿索引 → 影子读 → 灰度放量 → 切流 → 回滚。

运行：.venv/bin/python scripts/show_release.py

诚实边界：当前原型档只有一个 embedding 实现（deterministic 哈希），
所以"绿索引"用同一个 embedder 灌相同数据，影子读重合率必然是 1.0。
这个演示验证的是【控制流】——切流指针、灰度分流、护栏、回滚都真实生效；
模型升级带来的【质量差异】要切到 Qwen3 后才能测（见 docs/RELEASE.md）。
"""
from __future__ import annotations

import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.factory import active_namespace, get_embedder, get_vectorstore
from app.interfaces import VectorRecord
from app.release import (
    JsonFileStore,
    ReleaseController,
    ReleaseError,
    set_release_store,
)

TEXTS = [
    "七天无理由退款，签收起七日内可申请退款",
    "退款例外情况：定制商品不支持无理由退款",
    "XJ-200 型设备保修期为一年",
]


def build_namespace(namespace: str, embedder, store) -> None:
    """给指定 namespace 灌入演示数据（两套索引内容相同，见文件头说明）。"""
    vectors = embedder.embed(TEXTS)
    store.create_namespace(namespace, embedder.dimension)
    store.upsert(namespace, [
        VectorRecord(
            # ID 不拼 namespace：跨 namespace 本身就隔离，同名 chunk 在蓝绿两套索引
            # 应保留相同 ID，影子读才能按 ID 对比召回差异。
            id=f"c{i}",
            vector=vec,
            text=text,
            metadata={"tenant_id": "default", "embedding_model": embedder.model_id},
        )
        for i, (text, vec) in enumerate(zip(TEXTS, vectors))
    ])


def main() -> int:
    # 用临时目录做配置中心，演示结束自动清理，不污染真实发布状态。
    with tempfile.TemporaryDirectory() as tmp:
        set_release_store(JsonFileStore(Path(tmp) / "state.json"))
        try:
            embedder = get_embedder()
            store = get_vectorstore()
            blue_ns = active_namespace()          # 当前线上索引
            green_ns = blue_ns.replace("__v1", "__v2")
            build_namespace(blue_ns, embedder, store)
            build_namespace(green_ns, embedder, store)

            controller = ReleaseController(
                namespace_exists=lambda ns: ns in store.list_namespaces()
            )

            print("=" * 64)
            print("蓝绿发布五步演示")
            print("=" * 64)

            state = controller.init(blue_ns)
            print(f"① 初始化 : blue={state.blue}")
            print(f"   线上 namespace = {active_namespace()}")

            state = controller.prepare(green_ns)
            print(f"② 建绿索引: green={state.green}, active={state.active}（线上仍走蓝）")

            def search(query: str, ns: str) -> list[str]:
                vec = embedder.embed([query], is_query=True)[0]
                return [r.id for r in store.search(ns, vec, top_k=3)]

            shadow = controller.shadow_compare(["七天无理由退款", "XJ-200 保修"], search)
            print(f"③ 影子读 : 平均重合率={shadow['avg_overlap']:.2f} "
                  f"（原型档绿索引同源，必然为 1.0，只验证控制流）")

            print("④ 灰度放量: 同一批租户的分流结果")
            for pct in (10, 50, 100):
                controller.set_canary(pct)
                picked = Counter(
                    "绿" if controller.select_namespace(f"tenant_{i}").endswith("__v2") else "蓝"
                    for i in range(100)
                )
                print(f"   {pct:3d}% → 蓝 {picked['蓝']:3d} / 绿 {picked['绿']:3d}（100 租户）")

            # 确定性验证：同一租户两次结果必须一致，否则用户会遇到"跳版本"
            same = controller.select_namespace("tenant_7") == controller.select_namespace("tenant_7")
            print(f"   确定性  : {same}（同一租户稳定命中同一版本）")

            state = controller.cut_over()
            print(f"⑤ 原子切流: active={state.active}")
            print(f"   线上 namespace = {active_namespace()}（新请求立刻走绿，无需重启）")

            # 护栏演示：已全量切流后想换绿索引，应被拒绝（用存在的索引排除"不存在"干扰）
            try:
                controller.prepare(blue_ns)
            except ReleaseError as exc:
                print(f"   护栏拦截: {str(exc)[:40]}...")

            state = controller.rollback()
            print(f"⑥ 秒级回滚: active={state.active}, canary={state.canary_percent}%")
            print(f"   线上 namespace = {active_namespace()}（绿索引保留供排障）")
            print("=" * 64)
            print(f"审计历史共 {len(state.history)} 条，配置中心文件可追溯每次操作")
            return 0
        finally:
            set_release_store(None)


if __name__ == "__main__":
    raise SystemExit(main())
