"""FastAPI 服务层：把 RAG 能力暴露成 HTTP 接口。

启动：
    .venv/bin/python -m uvicorn app.api.main:app --reload
然后访问 http://127.0.0.1:8000/docs 看 Swagger 文档并直接调试。

为什么用 FastAPI？
- 原生 pydantic 校验：Question/Answer 模型直接当请求/响应体，类型错了自动 422。
- 自动生成 OpenAPI/Swagger 文档，前后端联调成本低。
- 异步友好，适合 LLM 这种慢 IO（本项目先用同步演示，生产可转 async）。

生产化改造（波次 0）：
- 租户身份从 JWT 解析，不再信请求体里的 tenant_id（堵越权漏洞）。
- 输入校验：长度 + 疑似注入检测。
- 每个请求生成 trace_id，全链路日志可串联。
"""
import os
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.models import Answer, Question
from app.observability.tracing import get_trace_id, log_event, new_trace_id
from app.rag_service import RAGService
from app.security.auth import TokenError, TokenPayload, issue_token, verify_token
from app.security.validation import ValidationError, validate_question
from app.tenancy.registry import (
    QuotaExceededError, SourceTypeNotAllowedError,
    TenantNotFoundError, TenantSuspendedError,
)

app = FastAPI(
    title="企业知识库问答 API",
    description="多租户 RAG 问答服务：检索 → 重排 → 生成 → 溯源",
    version="0.2.0",
)

# 鉴权开关：生产必须开；本地教学demo可用 AUTH_ENABLED=false 关掉（不推荐，仅方便 curl）。
# 默认开启——这正是"安全默认"（secure by default）原则：不配置也是安全的。
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() not in ("false", "0", "no")
# demo 签发接口：仅本地教学开放，生产必须设 DEMO_TOKEN_ISSUER=false 换成真实登录。
DEMO_TOKEN_ISSUER = os.getenv("DEMO_TOKEN_ISSUER", "true").lower() not in ("false", "0", "no")

# 服务单例：避免每个请求重建 embedder/store（生产用依赖注入 + 连接池）
# enforce_tenant=True：外部 HTTP 请求是不可信输入，必须强制校验租户身份
_service = RAGService(enforce_tenant=True)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """每个 HTTP 请求生成 trace_id，响应头回传，方便前端报障时提供。"""
    tid = new_trace_id()
    response = await call_next(request)
    response.headers["X-Trace-Id"] = tid
    return response


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


# ===== 身份依赖：FastAPI 的 Depends 机制，把鉴权逻辑统一在一个地方 =====
def get_current_tenant(authorization: Optional[str] = Header(None)) -> TokenPayload:
    """从 Authorization: Bearer <token> 解析租户身份。

    这就是修掉越权漏洞的关键：tenant_id 来自服务端签发的 token，
    客户端改请求体里的 tenant_id 完全无效。
    """
    if not AUTH_ENABLED:
        # 仅教学 demo：关闭鉴权时默认落入 default 租户，并在日志里标记（避免误用不知情）
        return TokenPayload(tenant_id="default", exp=0, role="admin")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    token = authorization[len("Bearer "):].strip()
    try:
        return verify_token(token)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc),
                            headers={"WWW-Authenticate": "Bearer"}) from exc


def require_admin(identity: TokenPayload = Depends(get_current_tenant)) -> TokenPayload:
    """管理接口专用：要求 token 的 role=admin。普通 user token 访问返回 403。"""
    if identity.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return identity


class TokenRequest(BaseModel):
    """demo 签发请求。生产换成真实登录（账号密码/SSO/企业微信扫码）。"""
    tenant_id: str
    role: str = "user"


@app.post("/auth/token")
def create_demo_token(req: TokenRequest):
    """教学用的 token 签发接口——仅 DEMO_TOKEN_ISSUER=true 时开放。

    生产必须关掉这个接口，换成真正的登录逻辑：校验凭证后从账号归属关系里
    取 tenant_id 签发，绝不能让调用方自己指定任意 tenant_id，否则等于没鉴权。
    """
    if not DEMO_TOKEN_ISSUER:
        raise HTTPException(status_code=404, detail="未启用 demo 签发接口")
    if req.role == "admin" and not DEMO_TOKEN_ISSUER:
        raise HTTPException(status_code=403, detail="不允许签发管理员 token")
    token = issue_token(req.tenant_id, role=req.role)
    return {"access_token": token, "token_type": "bearer", "tenant_id": req.tenant_id}


@app.get("/health")
def health():
    """健康检查：K8s 探针 / 负载均衡探活用。"""
    return {"status": "ok"}


@app.post("/ask", response_model=Answer)
def ask(question: Question, identity: TokenPayload = Depends(get_current_tenant)):
    """问答主接口：POST /ask。

    请求体用 Question（question/top_k），响应体用 Answer。
    租户身份强制来自 JWT（identity.tenant_id），请求体里的 tenant_id 被忽略——
    这是修复越权漏洞的核心，也是与旧版最大的行为差异。
    """
    try:
        q = validate_question(question.question)
    except ValidationError as exc:
        log_event("input_rejected", tenant_id=identity.tenant_id, reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    answer = _service.answer(
        q,
        tenant_id=identity.tenant_id,   # 用 token 里的身份，不信 body
        top_k=question.top_k,
    )
    return answer


@app.get("/tenants")
def list_tenants(identity: TokenPayload = Depends(require_admin)):
    """管理接口：列出已注册租户（需 admin token）。"""
    return {"tenants": _service.registry.list_tenants()}


@app.get("/tenants/{tenant_id}/config")
def tenant_config(tenant_id: str, source_type: str = "pdf",
                  identity: TokenPayload = Depends(require_admin)):
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
    log_event("error", trace_id=get_trace_id(), error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"detail": "服务内部错误，请稍后重试", "error": str(exc)},
    )
