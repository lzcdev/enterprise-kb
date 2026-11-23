"""组件工厂：唯一读取配置、决定实例化哪个实现的地方。

为什么要集中在这里？
- 业务代码里不出现 `if TIER == ...`，否则升级时要满项目找判断分支。
- 所有实现都在这里懒加载（函数内部 import），因为重依赖（torch/docling/chroma）
  只在真正用到那一档时才装/加载，本地开发原型档不会被企业档依赖拖慢。
- 换档位 = 改一个环境变量 TIER，业务代码零改动 —— 这就是"平滑升级"的控制开关。
"""
from typing import Optional, Callable
from functools import lru_cache

from app.config import settings
from app.interfaces import Parser, Embedder, VectorStore, Reranker, LLM


def get_parser() -> Parser:
    backend = settings.PARSER_BACKEND
    if backend == "pymupdf":
        from app.ingestion.loader_pymupdf import PyMuPDFParser
        return PyMuPDFParser()
    if backend == "docling":
        from app.ingestion.loader_docling import DoclingParser
        return DoclingParser()
    if backend == "video":
        from app.ingestion.loader_video import VideoParser
        return VideoParser()
    raise ValueError(f"未知的 PARSER_BACKEND: {backend}")


def get_embedder() -> Embedder:
    """注意：model_id 会被写进每条向量记录的 metadata，是迁移的锚点。"""
    backend = settings.EMBEDDING_BACKEND
    if backend == "local":
        from app.retrieval.embedding_local import LocalEmbedder
        return LocalEmbedder(
            model_name=settings.EMBEDDING_MODEL,
            dimension=settings.EMBEDDING_DIM,
            query_instruction=settings.EMBEDDING_QUERY_INSTRUCTION,
        )
    if backend == "api":
        from app.retrieval.embedding_api import ApiEmbedder
        return ApiEmbedder(
            model_name=settings.EMBEDDING_MODEL,
            dimension=settings.EMBEDDING_DIM,
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )
    if backend == "deterministic":
        from app.retrieval.embedding_local import DeterministicEmbedder
        return DeterministicEmbedder(dimension=settings.EMBEDDING_DIM)
    raise ValueError(f"未知的 EMBEDDING_BACKEND: {backend}")


def get_vectorstore() -> VectorStore:
    backend = settings.VECTORSTORE_BACKEND
    if backend == "memory":
        from app.retrieval.vectorstore_memory import InMemoryStore
        return InMemoryStore()
    if backend == "chroma":
        from app.retrieval.vectorstore_chroma import ChromaStore
        return ChromaStore(persist_dir=str(settings.CHROMA_PERSIST_DIR))
    if backend == "milvus":
        from app.retrieval.vectorstore_milvus import MilvusStore
        return MilvusStore(uri=settings.MILVUS_URI)
    raise ValueError(f"未知的 VECTORSTORE_BACKEND: {backend}")


def get_reranker() -> Optional[Reranker]:
    """按配置造重排器；RERANK_ENABLED=false 时直接关闭整层。"""
    if not settings.RERANK_ENABLED:
        return None
    from app.retrieval.reranker_local import get_reranker as _build
    return _build(backend=settings.RERANK_BACKEND, model_name=settings.RERANK_MODEL)


def get_retriever():
    """检索器：组装 embedder + vectorstore + namespace，供 API 层直接调用。"""
    from app.retrieval.retriever import Retriever
    return Retriever()


def get_llm() -> Optional[LLM]:
    """按 LLM_BACKEND 选生成实现；mock 档零依赖验证控制流。"""
    from app.generation.generator import get_llm as _build
    return _build(
        backend=settings.LLM_BACKEND,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        model=settings.LLM_MODEL,
    )


def active_namespace() -> str:
    """当前正在服务的索引名。灰度切流就是改这个函数返回的值。

    namespace 形如 kb__qwen3-embedding-0.6b__v1，把模型版本编码进名字，
    这样任何时候都能一眼看出某套索引用的是哪个模型生成的向量。

    关键修正：名字必须来自【实际实例化的 embedder.model_id】，而不是配置项
    EMBEDDING_MODEL。因为 EMBEDDING_BACKEND=deterministic 时实际用的是哈希档，
    但 EMBEDDING_MODEL 仍写着 Qwen3——若按后者命名，namespace 会说 Qwen3，
    metadata.embedding_model 却写 deterministic，蓝绿迁移判断版本就会误判。
    namespace 与 metadata 两个版本标记必须同源，这是迁移正确性的根基。
    """
    configured = getattr(settings, "ACTIVE_NAMESPACE", None)
    if configured:
        return configured

    # 发布控制器状态（模拟配置中心）：切流后新请求立刻走绿，无需重启进程。
    # 只有 active=green（全量切流）才改变主 namespace；灰度期 active 仍是 blue，
    # 按比例分流由 ReleaseController.select_namespace() 在请求级完成。
    # 延迟 import 避免 factory ↔ release 循环依赖；任何异常都静默回退到默认，
    # 保证配置中心故障时检索服务仍然可用（可用性优先）。
    try:
        from app.release.controller import ReleaseController

        state = ReleaseController().status()
        if state is not None:
            if state.active == "green" and state.green:
                return state.green
            if state.blue:
                return state.blue
    except Exception:
        pass

    # 用实际生效的 embedder 的 model_id，保证与写入 metadata 的标记一致
    safe_model = get_embedder().model_id.replace("/", "-").lower()
    return f"kb__{safe_model}__v1"


def embedder_for_namespace(namespace: str) -> Embedder:
    """按 namespace 编码的模型版本，返回与之匹配的 embedder。

    这是蓝绿/灰度迁移里最容易被忽略、也最致命的一环：
    蓝索引用模型 A 的向量、绿索引用模型 B 的向量，查询时必须用【建该索引的同一个
    模型】生成 query 向量，余弦相似度才有意义。若切了 namespace 却仍用全局默认
    embedder（往往是 A），查绿索引就是拿 A 的 query 向量比 B 的文档向量，结果全是噪音。

    namespace 约定格式：kb__{model_id}__v{n}
      - kb__deterministic-hash-v1__v1 → DeterministicEmbedder
      - kb__qwen-qwen3-embedding-0.6b__v1 → LocalEmbedder(Qwen3)

    解析失败时回退到全局 get_embedder()，保证未知 namespace 不会让服务崩溃
    （可用性优先；生产应告警）。用 lru_cache 是因为 LocalEmbedder 加载模型很贵，
    同一 namespace 的查询必须复用同一实例，不能每次查都重新加载 600M 参数。
    """
    from app.retrieval.embedding_local import DeterministicEmbedder, LocalEmbedder

    model_id = parse_model_id(namespace)
    if model_id is None:
        return get_embedder()

    if model_id.startswith("deterministic"):
        return _cached_deterministic()
    # 其余（qwen3 / bge / 任何本地模型名）都走 LocalEmbedder
    model_name = _model_id_to_name(model_id)
    return _cached_local(model_name)


def parse_model_id(namespace: str) -> Optional[str]:
    """从 namespace 里取出 model_id 段：kb__{model_id}__v{n} -> model_id。

    要求严格三段格式（kb / model_id / v版本号），缺版本段的宽松格式一律返回 None，
    避免把 hybrid demo 的 `kb__x__v1__hybrid_demo` 之类或残缺名误判成模型版本。
    """
    parts = namespace.split("__")
    if len(parts) == 3 and parts[0] == "kb" and parts[2].startswith("v"):
        return parts[1]
    return None


def _model_id_to_name(model_id: str) -> str:
    """model_id（qwen-qwen3-embedding-0.6b）反推 HF 模型名（Qwen/Qwen3-Embedding-0.6B）。

    优先用配置里的 EMBEDDING_MODEL（它就是本地档的真实模型名）；仅当配置的模型
    与 namespace 标记不一致时（说明在查另一个版本的索引），才按约定反推。
    """
    configured = settings.EMBEDDING_MODEL
    if configured.replace("/", "-").lower() == model_id:
        return configured
    # 反推：qwen-qwen3-embedding-0.6b -> Qwen/Qwen3-Embedding-0.6B 无法百分百还原大小写，
    # 但 HuggingFace 模型 ID 大小写不敏感，用小写 + 首个连字符转斜杠即可命中。
    if "-" in model_id:
        org, rest = model_id.split("-", 1)
        return f"{org}/{rest}"
    return model_id


@lru_cache(maxsize=4)
def _cached_deterministic() -> Embedder:
    from app.retrieval.embedding_local import DeterministicEmbedder
    return DeterministicEmbedder(dimension=settings.EMBEDDING_DIM)


@lru_cache(maxsize=4)
def _cached_local(model_name: str) -> Embedder:
    from app.retrieval.embedding_local import LocalEmbedder
    return LocalEmbedder(
        model_name=model_name,
        dimension=settings.EMBEDDING_DIM,
        query_instruction=settings.EMBEDDING_QUERY_INSTRUCTION,
    )
