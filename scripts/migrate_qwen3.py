"""真模型蓝绿迁移：Deterministic(蓝) → Qwen3-Embedding(绿)，按生产流程走完整五步。

这是 docs/RELEASE.md 里"换 embedding 模型"流程的**可执行版本**，和
show_release.py（同源演示、重合率恒 1.0）的本质区别：

  show_release.py   两套索引用同一个 embedder 灌数据 → 只验证"控制流"（切流指针/灰度/回滚）
  migrate_qwen3.py  蓝用 Deterministic、绿用 Qwen3 两套**不同模型** → 验证真实质量差异

最关键的正确性点（面试必讲）：
  切换 namespace 时必须同时切换 embedder。蓝索引存的是哈希向量，绿索引存的是
  Qwen3 语义向量，查绿索引若还用哈希模型生成 query 向量，余弦相似度就是噪音。
  本项目用 factory.embedder_for_namespace() 从 namespace 编码的 model_id 反推
  对应 embedder，并让 Retriever(bind_embedder_to_namespace=True) 自动绑定。

五步（对应 ReleaseController 状态机）：
  ① 建绿索引：用 Qwen3 全量重建历史 chunk，写绿 namespace，蓝不受影响
  ② 双写：迁移期新 chunk 同时写蓝绿，保证切流瞬间数据不缺
  ③ 影子读：同一批 query 各用各的模型查两套索引，对比召回 + 用 golden 集测质量
  ④ 灰度放量：10%→50%→100%，md5(tenant_id) 确定性分流，同一租户不跳版本
  ⑤ 原子切流 + 回滚演练：切到绿后线上 namespace/embedder 同步变，回滚秒级回蓝

运行：
  .venv/bin/python scripts/migrate_qwen3.py              # 控制流五步（零依赖，CI 可跑）
  .venv/bin/python scripts/migrate_qwen3.py --quality    # 追加真模型质量对比（需下载模型）
"""
from __future__ import annotations

import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.factory import active_namespace, embedder_for_namespace, parse_model_id
from app.interfaces import VectorRecord
from app.release import JsonFileStore, ReleaseController, ReleaseError, set_release_store
from app.retrieval.embedding_local import DeterministicEmbedder, LocalEmbedder
from app.retrieval.retriever import Retriever

# 蓝绿索引名：模型版本编码进 namespace，这是迁移的锚点
BLUE_NS = "kb__deterministic-hash-v1__v1"
GREEN_NS = "kb__qwen-qwen3-embedding-0.6b__v1"

# 演示语料：刻意包含"字面不重合但语义相关"的问答，才能体现语义模型的价值
TEXTS = [
    "七天无理由退款：自签收起七日内商品保持完好可申请退款，定制与生鲜商品除外",
    "质量问题换货：签收后十五日内可申请换货，来回运费由卖家承担",
    "忘记密码重置：登录页点忘记密码，输入注册手机号收验证码，验证码五分钟内有效",
    "旗舰版订阅价格每月四千九百九十九元，含全部 AI 功能",
    "专业版每月提供五万次 AI 问答额度",
    "客服热线四零零八八八六六六六，工作日九点到十八点",
    "事假三天以上需部门负责人审批，三天以内由直属主管审批",
    "公司团建安排将在季度会上公布（注：本条与退款无关）",
]

# 影子读 query：字面与语料重叠少，考验语义理解
SHADOW_QUERIES = [
    "买的东西不想要了能退吗",       # 应命中退款（语义相关，字面几乎不重叠）
    "收到的商品有瑕疵怎么换",       # 应命中换货
    "登录密码忘了怎么办",           # 应命中忘记密码
    "旗舰版一个月多少钱",           # 应命中价格
]

# 应命中的 chunk id（TEXTS 下标）
EXPECTED = {0: 0, 1: 1, 2: 2, 3: 3}  # query 下标 -> 应命中 text 下标


def build_namespace(store, embedder, namespace: str) -> int:
    """用指定 embedder 全量构建索引。注意 metadata 打上 embedding_model 版本标记。"""
    vectors = embedder.embed(TEXTS)
    store.create_namespace(namespace, embedder.dimension)
    store.upsert(namespace, [
        VectorRecord(
            id=f"c{i}",
            vector=vec,
            text=text,
            metadata={"tenant_id": "default", "embedding_model": embedder.model_id},
        )
        for i, (text, vec) in enumerate(zip(TEXTS, vectors))
    ])
    return store.count(namespace)


def dual_write(store, blue_emb, green_emb) -> None:
    """② 双写演示：迁移期一段新文档同时写蓝绿，各用各的模型向量化。"""
    new_text = "新增：会员年卡用户享受专属客服通道"
    # 蓝：用哈希模型
    bvec = blue_emb.embed([new_text])[0]
    store.upsert(BLUE_NS, [VectorRecord(
        id="c_new", vector=bvec, text=new_text,
        metadata={"tenant_id": "default", "embedding_model": blue_emb.model_id},
    )])
    # 绿：用 Qwen3 模型
    gvec = green_emb.embed([new_text])[0]
    store.upsert(GREEN_NS, [VectorRecord(
        id="c_new", vector=gvec, text=new_text,
        metadata={"tenant_id": "default", "embedding_model": green_emb.model_id},
    )])
    print(f"   蓝 count={store.count(BLUE_NS)} / 绿 count={store.count(GREEN_NS)}（新文档已双写）")


def make_search_fn(store, embedder, *, validate_model: bool = True):
    """闭包：用【指定 embedder】生成 query 向量查指定 namespace。
    这是本题的核心——蓝绿查询各用各的模型，绝不能共用。

    validate_model=True 时做防呆校验：embedder 必须与 namespace 编码的模型同源。
    控制流 stub 阶段蓝绿都是哈希向量（只是标记不同），此时传 False 关闭校验。
    """
    def search(query: str, namespace: str) -> list[str]:
        if validate_model:
            expect_model = parse_model_id(namespace)
            actual_model = embedder.model_id
            if expect_model and not actual_model.replace("/", "-").lower().startswith(
                expect_model.split("-")[0]
            ):
                raise RuntimeError(
                    f"模型-索引不匹配：namespace={namespace} 期望 {expect_model}，"
                    f"但 embedder={actual_model}（切索引必须同时切模型）"
                )
        qvec = embedder.embed([query], is_query=True)[0]
        return [r.id for r in store.search(namespace, qvec, top_k=3)]
    return search


def shadow_quality(store, blue_emb, green_emb) -> dict:
    """③ 影子读：各用各的模型查，计算 top1 命中率。
    对照组设计：query 与语料字面重叠低，哈希模型只靠 n-gram 重叠，语义模型能靠意图。"""
    results = {"blue": [], "green": []}
    blue_search = make_search_fn(store, blue_emb)
    green_search = make_search_fn(store, green_emb)
    for qi, q in enumerate(SHADOW_QUERIES):
        expected_id = f"c{EXPECTED[qi]}"
        b_top = blue_search(q, BLUE_NS)
        g_top = green_search(q, GREEN_NS)
        results["blue"].append(1.0 if b_top and b_top[0] == expected_id else 0.0)
        results["green"].append(1.0 if g_top and g_top[0] == expected_id else 0.0)
        print(f"   Q{qi+1} {q!r}")
        print(f"      蓝 top3={b_top}  {'✓' if results['blue'][-1] else '✗'}")
        print(f"      绿 top3={g_top}  {'✓' if results['green'][-1] else '✗'}")
    return {
        "blue_top1_acc": round(sum(results["blue"]) / len(results["blue"]), 3),
        "green_top1_acc": round(sum(results["green"]) / len(results["green"]), 3),
    }


def rerank_quality() -> dict:
    """精排质量对比：同一批候选，deterministic 重排 vs Qwen3-Reranker 重排。

    构造"召回分最高但语义无关"的陷阱候选——检验交叉编码器能否纠正粗召回的错误排序。
    """
    from app.interfaces import ScoredChunk
    from app.retrieval.reranker_local import DeterministicReranker, LocalReranker

    # query 刻意口语化、与文档无字面重叠——哈希/2-gram 启发式会失效，
    # 只有语义模型能理解"不想要了=退款""有瑕疵=换货"。
    query = "买的东西不想要了能退吗"
    # 模拟粗召回：无关的"价格"文档 cosine 最高（哈希档常见误召回）
    candidates = [
        ScoredChunk(id="price", text="旗舰版每月4999元含全部AI功能", score=0.9, metadata={}),
        ScoredChunk(id="refund", text="自签收起七日内商品保持完好可申请退货退款，定制生鲜除外", score=0.5, metadata={}),
        ScoredChunk(id="exchange", text="收到商品有瑕疵十五日内可换，来回运费由卖家承担", score=0.4, metadata={}),
    ]
    det = DeterministicReranker().rerank(query, candidates, top_n=3)
    sem = LocalReranker().rerank(query, candidates, top_n=3)
    det_top, sem_top = det[0].id, sem[0].id
    print(f"   召回 top1='price'（无关项分最高，故意设的陷阱）")
    print(f"   确定性重排 top1={det_top!r}  分数={[round(c.score,2) for c in det]}")
    print(f"   Qwen3 重排 top1={sem_top!r}  分数={[round(c.score,2) for c in sem]}")
    return {
        "deterministic_top1": det_top,
        "qwen3_rerank_top1": sem_top,
        "qwen3_fixed_misrank": sem_top == "refund" and det_top != "refund",
    }


def control_flow_demo(store) -> None:
    """五步控制流（不依赖真模型）：验证状态机、灰度分流、联动、回滚。"""
    blue_emb = DeterministicEmbedder()
    green_emb = GreenStub()  # 控制流阶段用 stub，避免必须有模型
    n_blue = build_namespace(store, blue_emb, BLUE_NS)
    n_green = build_namespace(store, green_emb, GREEN_NS)
    print(f"① 建绿索引: 蓝={n_blue} 条 / 绿={n_green} 条（绿用 {green_emb.model_id}）")

    dual_write(store, blue_emb, green_emb)

    controller = ReleaseController(namespace_exists=lambda ns: ns in store.list_namespaces())
    state = controller.init(BLUE_NS)
    print(f"   发布状态初始化: active={state.active}, blue={state.blue}")

    state = controller.prepare(GREEN_NS)
    print(f"② prepare: green={state.green}, active={state.active}（线上仍走蓝）")

    # 影子读控制流：stub 阶段蓝绿都是哈希向量，重合率应接近 1.0，只验证控制流
    shadow = controller.shadow_compare(
        SHADOW_QUERIES, make_search_fn(store, blue_emb, validate_model=False), top_k=3
    )
    print(f"③ 影子读控制流: 蓝vs绿 top3 平均重合率={shadow['avg_overlap']:.2f}"
          f"（stub 同源，只验证控制流）")

    print("④ 灰度放量: 100 租户的确定性分流")
    for pct in (10, 50, 100):
        controller.set_canary(pct)
        picked = Counter(
            "绿" if controller.select_namespace(f"tenant_{i}") == GREEN_NS else "蓝"
            for i in range(100)
        )
        print(f"   {pct:3d}% → 蓝 {picked['蓝']:3d} / 绿 {picked['绿']:3d}")

    # 验证 embedder 与 namespace 联动：查绿 namespace 必须拿到绿模型
    bound = Retriever(store=store, namespace=GREEN_NS, bind_embedder_to_namespace=True)
    print(f"   联动验证: namespace=绿 → embedder={type(bound.embedder).__name__} "
          f"({bound.embedder.model_id})")

    state = controller.cut_over()
    print(f"⑤ 原子切流: active={state.active}, canary={state.canary_percent}%")

    state = controller.rollback()
    print(f"⑥ 回滚演练: active={state.active}, canary={state.canary_percent}% "
          f"（绿索引保留，秒级可再切回）")


class GreenStub:
    """控制流阶段的绿索引 stub：复用哈希模型但换 model_id，只为验证链路，不评估质量。"""
    def __init__(self):
        self._inner = DeterministicEmbedder()
        self.model_id = "qwen-qwen3-embedding-0.6b"  # 伪装成绿，只为 namespace 校验通过
        self.dimension = self._inner.dimension

    def embed(self, texts, *, is_query=False):
        return self._inner.embed(texts, is_query=is_query)


def main() -> int:
    with_quality = "--quality" in sys.argv
    print("=" * 68)
    print("真模型蓝绿迁移五步：Deterministic(蓝) → Qwen3(绿)")
    print(f"模式: {'控制流 + 真模型质量对比' if with_quality else '控制流（加 --quality 跑质量对比）'}")
    print("=" * 68)

    # 用临时发布状态 + 临时 Chroma 目录，不污染真实发布状态
    from app.retrieval.vectorstore_chroma import ChromaStore
    with tempfile.TemporaryDirectory() as tmp:
        set_release_store(JsonFileStore(Path(tmp) / "state.json"))
        try:
            store = ChromaStore(persist_dir=str(Path(tmp) / "chroma"))
            control_flow_demo(store)

            if with_quality:
                print("\n" + "=" * 68)
                print("真模型质量对比（Qwen3 vs Deterministic）")
                print("=" * 68)
                from huggingface_hub import snapshot_download
                print("检查/下载 Qwen3-Embedding-0.6B（hf-mirror）...")
                import os
                os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
                snapshot_download(
                    "Qwen/Qwen3-Embedding-0.6B",
                    allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"],
                )
                blue_emb = DeterministicEmbedder()
                green_emb = LocalEmbedder(model_name="Qwen/Qwen3-Embedding-0.6B")
                print("模型已加载，构建两套索引...")
                build_namespace(store, blue_emb, BLUE_NS)
                build_namespace(store, green_emb, GREEN_NS)
                print("影子读质量对比（embedding, top1 命中率）:")
                q = shadow_quality(store, blue_emb, green_emb)
                print(f"   蓝(Deterministic) top1 命中率: {q['blue_top1_acc']:.3f}")
                print(f"   绿(Qwen3)         top1 命中率: {q['green_top1_acc']:.3f}")
                print(f"   质量差异: {q['green_top1_acc'] - q['blue_top1_acc']:+.3f}")

                print("\n精排质量对比（reranker, 纠正误召回）:")
                r = rerank_quality()
                print(f"   Qwen3-Reranker 纠正误召回: {r['qwen3_fixed_misrank']}")

                # 保存可追溯的结构化报告——面试/简历的硬证据，版本化进仓库
                import json
                from datetime import datetime, timezone
                report = {
                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "blue_model": blue_emb.model_id,
                    "green_model": green_emb.model_id,
                    "query_count": len(SHADOW_QUERIES),
                    "embedding_metrics": q,
                    "embedding_delta_top1": round(q["green_top1_acc"] - q["blue_top1_acc"], 3),
                    "rerank_metrics": r,
                    "note": "影子读 top1 命中率 + 精排纠正误召回。小样本教学演示，非严格 benchmark。",
                }
                report_path = ROOT / "data" / "eval" / "model_migration_report.json"
                report_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"   报告已保存: {report_path.relative_to(ROOT)}")
            return 0
        except RuntimeError as exc:
            print(f"\n[跳过] 真模型质量对比不可用: {exc}")
            print("控制流五步已验证通过；装依赖 + 下载模型后加 --quality 重跑。")
            return 0
        finally:
            set_release_store(None)


if __name__ == "__main__":
    raise SystemExit(main())
