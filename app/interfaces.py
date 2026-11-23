"""抽象契约层：全系统唯一稳定的部分。

设计原则（面试要能讲）：
1. 用 typing.Protocol 做结构化子类型 —— 实现类不需要显式继承，只要有同名方法就满足接口，
   这样接入第三方库（Docling/Chroma）时不用改它们的源码去继承我们的类。
2. 每个可替换组件都暴露 model_id / name —— 这是"平滑迁移"的基石：
   数据里记录了它是被哪个模型生成的，才能做版本化、灰度、回滚。
3. VectorStore 引入 namespace 概念 —— 同一个库里可以并存多套索引，
   换 embedding 模型时建新 namespace 全量重建，验证通过后原子切流。

任何业务代码只 import 这里的接口，绝不 import 具体实现。
"""
from typing import Protocol, Any, Optional, runtime_checkable
from dataclasses import dataclass, field


@dataclass
class ParsedSection:
    """解析产物：一段带来源信息的文本，还没切 chunk。"""
    text: str
    source_type: str
    page: Optional[int] = None
    title_path: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class VectorRecord:
    """存入向量库的一条记录。metadata 必须带 embedding_model 版本标记。"""
    id: str
    vector: list[float]
    text: str
    metadata: dict


@dataclass
class ScoredChunk:
    """检索/重排后的候选 chunk。"""
    id: str
    text: str
    score: float
    metadata: dict


@runtime_checkable
class Parser(Protocol):
    """文档解析：文件 -> 带结构信息的文本段。proto/docling/serve 三档实现。

    @runtime_checkable：让 isinstance(obj, Parser) 可用。
    注意 Python 的 Protocol 默认不能在运行时做 isinstance 检查（会抛 TypeError），
    加了这个装饰器才可以。局限：它只检查方法/属性名是否存在，不校验签名和类型。
    """
    name: str

    def parse(self, path: str) -> list[ParsedSection]: ...


class Embedder(Protocol):
    """文本 -> 向量。换模型只需换实现，但要触发数据迁移。"""
    model_id: str
    dimension: int

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]: ...


class VectorStore(Protocol):
    """向量存取。namespace 用于隔离不同 embedding 版本的索引。"""
    name: str

    def create_namespace(self, namespace: str, dimension: int) -> None: ...
    def upsert(self, namespace: str, records: list[VectorRecord]) -> None: ...
    def search(
        self,
        namespace: str,
        query_vector: list[float],
        top_k: int,
        filters: Optional[dict] = None,
    ) -> list[ScoredChunk]: ...
    def count(self, namespace: str) -> int: ...
    def delete_namespace(self, namespace: str) -> None: ...
    def list_namespaces(self) -> list[str]: ...


class Reranker(Protocol):
    """精排：(query, 候选) -> 排序后的候选。可关闭/换模型。"""
    model_id: str

    def rerank(self, query: str, candidates: list[ScoredChunk], top_n: int) -> list[ScoredChunk]: ...


class LLM(Protocol):
    """生成：prompt -> 答案。最易替换的一层。"""
    model_id: str

    def generate(self, prompt: str, *, max_tokens: int = 1024) -> str: ...
