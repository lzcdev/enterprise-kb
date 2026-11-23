"""核心数据模型：定义整个系统里流转的数据结构。

这些模型就是模块之间的"契约"—— ingestion、retrieval、api 都传这些对象，
先定好它们，后面各模块可以独立开发和测试。
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime
import uuid


def gen_id() -> str:
    return str(uuid.uuid4())


class DocumentMeta(BaseModel):
    """文档的元数据：溯源和过滤都靠它"""
    doc_id: str = Field(default_factory=gen_id)
    tenant_id: str = "default"          # 阶段 1 先用默认值，阶段 3 做真正隔离
    filename: str
    # 扩展视频/音频：接入多模态时 Literal 必须同步扩展，否则 pydantic 校验直接拒绝 "video"。
    # 这就是"契约层要跟着演进"——只加 loader 不改契约，视频数据根本存不进来。
    source_type: Literal["pdf", "docx", "md", "txt", "video", "audio"]
    page: Optional[int] = None          # PDF 页码
    title_path: str = ""                # 标题层级，如 "产品手册 > 退款政策"
    # 音视频溯源：文档用 page（第几页），音视频用 start_ms/end_ms（第几毫秒）。
    # 两者可共存：一个 chunk 要么来自文档页码，要么来自音视频时间段。
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None


class Chunk(BaseModel):
    """切分后的文本块：向量化入库的最小单元"""
    chunk_id: str = Field(default_factory=gen_id)
    text: str
    meta: DocumentMeta


class IndexedChunk(BaseModel):
    """带向量的 chunk：存入向量库的形态"""
    chunk: Chunk
    embedding: list[float]


class Source(BaseModel):
    """答案引用来源：给 LLM 答案做溯源"""
    doc_id: str
    filename: str
    page: Optional[int] = None
    title_path: str = ""
    text: str                    # 被引用的原文片段
    score: float = 0.0           # 相关度分数
    # 音视频溯源：前端拿到 start_ms 可生成"跳转到 01:23"的播放链接
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None


class Question(BaseModel):
    """用户提问请求。

    注意 tenant_id 在请求体里只是为了向后兼容 demo 和测试；
    生产环境 API 层强制从 JWT 解析租户身份，body 里的值会被忽略。
    这是纵深防御：外部不可信输入的身份绝不能自报。
    """
    question: str = Field(min_length=1, max_length=1000, description="用户问题")
    tenant_id: str = "default"
    top_k: Optional[int] = Field(default=None, ge=1, le=100)


class Answer(BaseModel):
    """系统返回的答案"""
    answer: str
    sources: list[Source] = []
    question: str
    elapsed_ms: float = 0.0      # 端到端耗时，用于观测
