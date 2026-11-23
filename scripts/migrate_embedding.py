"""Embedding 模型平滑迁移脚本骨架（教学版，展示蓝绿迁移的完整流程）。

场景：线上用 A 模型，现在出了更强的 B 模型，要切换且服务不中断、可回滚。

核心策略 —— 蓝绿部署 + 双写 + 影子验证 + 原子切流：
  1. 建绿索引（新 namespace），用 B 模型全量重建历史数据
  2. 迁移期间双写：新入库文档同时写蓝(A)和绿(B)两套索引
  3. 影子读：线上 query 同时查两套，只返回蓝的结果，对比绿的结果做质量评估
  4. 验证达标后原子切流：改 ACTIVE_NAMESPACE 指向绿
  5. 观察期保留蓝索引，出问题秒级回滚

注意 chunk 本身通常不用重做（切分只依赖文本），除非用了语义切分。
"""
import logging

logger = logging.getLogger(__name__)


def build_green_index(old_model_id: str, new_model_id: str) -> str:
    """步骤 1：全量重建。用新模型给所有 chunk 重新向量化，写入新 namespace。"""
    from app.factory import get_vectorstore, get_embedder
    from app.ingestion.splitter import load_all_chunks

    store = get_vectorstore()
    new_namespace = f"kb__{new_model_id.replace('/', '-').lower()}__v1"

    if new_namespace in store.list_namespaces():
        logger.info("绿索引已存在，跳过重建: %s", new_namespace)
        return new_namespace

    store.create_namespace(new_namespace, dimension=0)  # 实际按 embedder.dimension
    embedder = get_embedder()
    chunks = load_all_chunks()  # 复用已切好的 chunk，不重新切分

    batch = []
    for c in chunks:
        vec = embedder.embed([c.text], is_query=False)[0]
        batch.append({
            "id": c.chunk_id,
            "vector": vec,
            "text": c.text,
            "metadata": {
                **c.meta.model_dump(),
                "embedding_model": new_model_id,  # 关键：打版本标记
            },
        })
        if len(batch) >= 100:
            store.upsert(new_namespace, batch)
            batch = []
    if batch:
        store.upsert(new_namespace, batch)

    logger.info("绿索引构建完成: %s, 共 %d 条", new_namespace, store.count(new_namespace))
    return new_namespace


def shadow_compare(blue_ns: str, green_ns: str, sample_queries: list[str]) -> dict:
    """步骤 3：影子读对比。同一批 query 查两套索引，评估召回差异。"""
    from app.factory import get_vectorstore, get_embedder

    store = get_vectorstore()
    embedder = get_embedder()
    overlap_sum = 0.0

    for q in sample_queries:
        qvec = embedder.embed([q], is_query=True)[0]
        blue_ids = {c.id for c in store.search(blue_ns, qvec, top_k=10)}
        green_ids = {c.id for c in store.search(green_ns, qvec, top_k=10)}
        overlap_sum += len(blue_ids & green_ids) / max(len(blue_ids), 1)

    avg_overlap = overlap_sum / max(len(sample_queries), 1)
    logger.info("影子对比平均重叠率: %.3f", avg_overlap)
    return {"avg_overlap": avg_overlap, "sample_size": len(sample_queries)}


def cut_over(green_ns: str) -> None:
    """步骤 4：原子切流。生产上改配置中心/环境变量，这里用 .env 演示。

    真实系统应当用配置中心（如 Nacos/Apollo）做到秒级生效且不用重启进程。
    """
    logger.info("切流到: %s（生产用配置中心下发 ACTIVE_NAMESPACE）", green_ns)
    # 伪代码：config_center.set("ACTIVE_NAMESPACE", green_ns)


def rollback(blue_ns: str) -> None:
    """步骤 5：回滚。把 ACTIVE_NAMESPACE 指回蓝索引，秒级恢复。"""
    logger.info("回滚到: %s", blue_ns)
