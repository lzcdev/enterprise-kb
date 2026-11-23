"""FastAPI 服务层：把 RAG 能力暴露成 HTTP 接口。

启动：
    .venv/bin/python -m uvicorn app.api.main:app --reload
然后访问 http://127.0.0.1:8000/docs 看 Swagger 文档并直接调试。

为什么用 FastAPI？
- 原生 pydantic 校验：Question/Answer 模型直接当请求/响应体，类型错了自动 422。
- 自动生成 OpenAPI/Swagger 文档，前后端联调成本低。
- 异步友好，适合 LLM 这种慢 IO（本项目先用同步演示，生产可转 async）。
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.models import Answer, Question
from app.rag_service import RAGService
from app.tenancy.registry import (
    QuotaExceededError, SourceTypeNotAllowedError,
    TenantNotFoundError, TenantSuspendedError,
)

app = FastAPI(
    title="企业知识库问答 API",
    description="多租户 RAG 问答服务：检索 → 重排 → 生成 → 溯源",
    version="0.1.0",
)

# 服务单例：避免每个请求重建 embedder/store（生产用依赖注入 + 连接池）
# enforce_tenant=True：外部 HTTP 请求是不可信输入，必须强制校验租户身份
_service = RAGService(enforce_tenant=True)


def bootstrap_demo_index() -> int:
    """教学 demo：启动时把测试手册灌进服务自己的库实例。

    生产环境不要这么做——正式索引由离线建库流水线写入持久化向量库（Chroma/Milvus），
    API 只负责读。这里是为了让 memory 档（进程内、不共享）的 /ask 开箱可用。
    """
    from app.factory import active_namespace
    from app.ingestion.loader_pymupdf import PyMuPDFParser
    from app.ingestion.splitter import RecursiveSplitter, section_to_chunks
    from app.interfaces import VectorRecord

    embedder = _service.retriever.embedder
    store = _service.retriever.store
    namespace = active_namespace()
    sections = PyMuPDFParser().parse("data/测试产品手册.md")
    chunks = []
    for sec in sections:
        chunks.extend(section_to_chunks(sec, RecursiveSplitter(chunk_size=150, overlap=20)))
    vectors = embedder.embed([c.text for c in chunks])
    store.create_namespace(namespace, embedder.dimension)
    store.upsert(namespace, [
        VectorRecord(
            id=c.chunk_id, vector=v, text=c.text,
            metadata={
                "embedding_model": embedder.model_id, "tenant_id": "default",
                "doc_id": c.meta.doc_id, "filename": c.meta.filename,
                "page": c.meta.page, "title_path": c.meta.title_path,
            },
        )
        for c, v in zip(chunks, vectors)
    ])
    return len(chunks)


# 仅在 memory 档自动灌 demo 数据；持久化档由离线流水线建库，不在此处理
if settings.VECTORSTORE_BACKEND == "memory":
    bootstrap_demo_index()


@app.get("/health")
def health():
    """健康检查：K8s 探针 / 负载均衡探活用。"""
    return {"status": "ok"}


@app.post("/ask", response_model=Answer)
def ask(question: Question):
    """问答主接口：POST /ask。

    请求体用 Question（question/tenant_id/top_k），
    响应体用 Answer（answer/sources/question/elapsed_ms），pydantic 自动校验。
    租户身份由 _service(enforce_tenant=True) 强制校验，越权抛出的 TenantError
    由下方专用 handler 映射成对应 HTTP 状态码。
    """
    answer = _service.answer(
        question.question,
        tenant_id=question.tenant_id,
        top_k=question.top_k,
    )
    return answer


@app.get("/tenants")
def list_tenants():
    """管理接口：列出已注册租户（生产需加管理员鉴权，这里仅演示）。"""
    return {"tenants": _service.registry.list_tenants()}


@app.get("/tenants/{tenant_id}/config")
def tenant_config(tenant_id: str, source_type: str = "pdf"):
    """管理接口：查看某租户的最终生效配置与每层来源，排障用。

    客户说"我调的参数没生效"时，看 sources 就知道被哪层覆盖了。
    """
    cfg = _service.registry.resolve_config(tenant_id, source_type=source_type)
    return {**cfg.as_dict(), "sources": cfg.sources}


# ===== 租户异常 → HTTP 状态码映射：不同拒绝原因给前端不同语义 =====
@app.exception_handler(TenantNotFoundError)
async def tenant_not_found_handler(request: Request, exc: TenantNotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc), "code": "TENANT_NOT_FOUND"})


@app.exception_handler(TenantSuspendedError)
async def tenant_suspended_handler(request: Request, exc: TenantSuspendedError):
    return JSONResponse(status_code=403, content={"detail": str(exc), "code": "TENANT_SUSPENDED"})


@app.exception_handler(SourceTypeNotAllowedError)
async def source_type_denied_handler(request: Request, exc: SourceTypeNotAllowedError):
    return JSONResponse(status_code=403, content={"detail": str(exc), "code": "SOURCE_TYPE_DENIED"})


@app.exception_handler(QuotaExceededError)
async def quota_exceeded_handler(request: Request, exc: QuotaExceededError):
    return JSONResponse(status_code=429, content={"detail": str(exc), "code": "QUOTA_EXCEEDED"})


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """兜底异常处理：生产应记录日志并返回脱敏信息，别把堆栈暴露给前端。"""
    return JSONResponse(
        status_code=500,
        content={"detail": "服务内部错误，请稍后重试", "error": str(exc)},
    )
