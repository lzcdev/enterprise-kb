"""组件工厂：唯一读取配置、决定实例化哪个实现的地方。

为什么要集中在这里？
- 业务代码里不出现 `if TIER == ...`，否则升级时要满项目找判断分支。
- 所有实现都在这里懒加载（函数内部 import），因为重依赖（torch/docling/chroma）
  只在真正用到那一档时才装/加载，本地开发原型档不会被企业档依赖拖慢。
- 换档位 = 改一个环境变量 TIER，业务代码零改动 —— 这就是"平滑升级"的控制开关。
"""
from typing import Optional

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


def get_llm() -> LLM:
    from app.generation.generator import OpenAILikeLLM
    return OpenAILikeLLM(
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
    # 用实际生效的 embedder 的 model_id，保证与写入 metadata 的标记一致
    safe_model = get_embedder().model_id.replace("/", "-").lower()
    return f"kb__{safe_model}__v1"
