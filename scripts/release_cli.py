"""发布控制命令行：CI/CD workflow 和本地手动操作共用同一入口。

用法（项目根目录执行）：
  .venv/bin/python scripts/release_cli.py status
  .venv/bin/python scripts/release_cli.py init --blue kb__old__v1
  .venv/bin/python scripts/release_cli.py prepare --green kb__new__v2
  .venv/bin/python scripts/release_cli.py shadow --queries "退款,保修"
  .venv/bin/python scripts/release_cli.py canary --percent 10
  .venv/bin/python scripts/release_cli.py cutover
  .venv/bin/python scripts/release_cli.py rollback

设计要点：所有危险动作都要显式确认（force），禁止 workflow 误操作一把梭。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.factory import get_embedder, get_vectorstore
from app.interfaces import VectorRecord
from app.release import ReleaseController, ReleaseError


def _controller() -> ReleaseController:
    """构造控制器，并尽量注入 namespace 存在性校验。

    memory 档：每次 get_vectorstore() 都是新空实例，校验会把所有 namespace
    误判为不存在——这是原型档特有的问题，此时跳过校验并明确告警。
    生产档（chroma/milvus）连同一持久化存储，校验真实生效，防止切到空索引。
    """
    if settings.VECTORSTORE_BACKEND == "memory":
        print("[WARN] memory 档不共享实例，跳过 namespace 存在性校验", file=sys.stderr)
        return ReleaseController()
    store = get_vectorstore()
    return ReleaseController(namespace_exists=lambda ns: ns in store.list_namespaces())


def _shadow_index(namespace: str, texts: list[str]) -> None:
    """为影子读准备最小可检索索引（原型档用同一 embedder 灌两套 namespace）。"""
    embedder = get_embedder()
    store = get_vectorstore()
    vectors = embedder.embed(texts)
    if namespace not in store.list_namespaces():
        store.create_namespace(namespace, embedder.dimension)
    store.upsert(namespace, [
        VectorRecord(
            id=f"{namespace}__c{i}",
            vector=vec,
            text=text,
            metadata={"tenant_id": "default", "embedding_model": embedder.model_id},
        )
        for i, (text, vec) in enumerate(zip(texts, vectors))
    ])


def cmd_status(args) -> int:
    state = _controller().status()
    print(json.dumps(state.to_dict() if state else None, ensure_ascii=False, indent=2))
    return 0


def cmd_init(args) -> int:
    st = _controller().init(args.blue, args.green)
    print(f"[OK] 初始化: blue={st.blue} green={st.green} active={st.active}")
    return 0


def cmd_prepare(args) -> int:
    st = _controller().prepare(args.green)
    print(f"[OK] 绿索引就绪: {st.green}（线上仍走蓝）")
    return 0


def cmd_shadow(args) -> int:
    controller = _controller()
    state = controller.status()
    if state is None or not state.green:
        print("[FAIL] 尚未登记绿索引", file=sys.stderr)
        return 2
    texts = [
        "七天无理由退款，签收起七日内可申请退款",
        "退款例外情况：定制商品不支持无理由退款",
        "XJ-200 型设备保修期为一年",
    ]
    _shadow_index(state.blue, texts)
    _shadow_index(state.green, texts)
    store = get_vectorstore()
    embedder = get_embedder()

    def search(query: str, ns: str) -> list[str]:
        vec = embedder.embed([query], is_query=True)[0]
        return [r.id for r in store.search(ns, vec, top_k=args.top_k)]

    result = controller.shadow_compare(args.queries.split(","), search, top_k=args.top_k)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["avg_overlap"] < args.min_overlap:
        print(f"[FAIL] 影子重合率 {result['avg_overlap']} 低于阈值 {args.min_overlap}", file=sys.stderr)
        return 1
    print("[OK] 影子读通过")
    return 0


def cmd_canary(args) -> int:
    st = _controller().set_canary(args.percent, force=args.force)
    print(f"[OK] 灰度放量: {st.canary_percent}% 流量走绿索引 {st.green}")
    return 0


def cmd_cutover(args) -> int:
    st = _controller().cut_over(force=args.force)
    print(f"[OK] 原子切流完成: 当前主索引 {st.effective_namespace()}")
    return 0


def cmd_rollback(args) -> int:
    st = _controller().rollback()
    print(f"[OK] 已回滚: 当前主索引 {st.effective_namespace()}")
    return 0


def cmd_select(args) -> int:
    ns = _controller().select_namespace(args.tenant)
    print(ns)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="蓝绿发布控制 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看当前发布状态").set_defaults(func=cmd_status)

    p = sub.add_parser("init", help="初始化发布状态")
    p.add_argument("--blue", required=True, help="蓝索引 namespace")
    p.add_argument("--green", help="绿索引 namespace（可选）")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("prepare", help="登记绿索引")
    p.add_argument("--green", required=True)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("shadow", help="影子读对比")
    p.add_argument("--queries", default="七天无理由退款,XJ-200保修")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--min-overlap", type=float, default=0.0)
    p.set_defaults(func=cmd_shadow)

    p = sub.add_parser("canary", help="灰度放量")
    p.add_argument("--percent", type=int, required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_canary)

    p = sub.add_parser("cutover", help="原子切流到绿")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_cutover)

    sub.add_parser("rollback", help="回滚到蓝").set_defaults(func=cmd_rollback)

    p = sub.add_parser("select", help="查看某租户当前命中哪个索引")
    p.add_argument("--tenant", required=True)
    p.set_defaults(func=cmd_select)

    args = parser.parse_args()
    try:
        return args.func(args)
    except ReleaseError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
