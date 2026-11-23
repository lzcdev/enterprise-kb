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
    source_type: Literal["pdf", "docx", "md", "txt"]
    page: Optional[int] = None          # PDF 页码
    title_path: str = ""                # 标题层级，如 "产品手册 > 退款政策"


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


class Question(BaseModel):
    """用户提问请求"""
    question: str
    tenant_id: str = "default"
    top_k: Optional[int] = None


class Answer(BaseModel):
    """系统返回的答案"""
    answer: str
    sources: list[Source] = []
    question: str
    elapsed_ms: float = 0.0      # 端到端耗时，用于观测
