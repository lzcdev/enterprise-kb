"""本地 Embedding 实现（生产档）+ 确定性演示实现（原型档）。

两个类都满足 Embedder 协议：model_id / dimension / embed(texts, is_query)。

LocalEmbedder（生产档）
    用 sentence-transformers 加载本地模型（默认 Qwen3-Embedding-0.6B）。
    关键点：
    - 指令感知：Qwen3 支持给 query 加 instruction 提升 1~5% 效果，
      且 query 和 passage 用不同前缀——这是 Qwen3 官方要求的用法。
    - 归一化：向量做 L2 归一化后，余弦相似度等价于内积，检索更快。
    - 优雅降级：没装 sentence-transformers 时不在导入期报错，
      真正调用才提示——避免拖垮整个项目的导入。

DeterministicEmbedder（原型档）
    用哈希把文本映射成固定维度向量，不需要任何模型/依赖。
    注意：它不理解语义，只用于"验证存取检索链路是否打通"，
    绝不能用于评估检索质量。生产必须换 LocalEmbedder/ApiEmbedder。
"""
from hashlib import md5
import math

from app.interfaces import Embedder


class LocalEmbedder:
    """本地模型实现：sentence-transformers 加载 Qwen3/BGE 等嵌入模型。"""

    def __init__(
        self,
        model_name: str = "Qwen/Qwen3-Embedding-0.6B",
        dimension: int = 1024,
        query_instruction: str = "",
        normalize: bool = True,
    ):
        self.model_name = model_name
        # model_id 会被写进向量 metadata —— 蓝绿迁移按它判断是否需要重建
        self.model_id = model_name.replace("/", "-").lower()
        self.dimension = dimension
        self.query_instruction = query_instruction
        self.normalize = normalize
        self._model = None  # 懒加载：真正 embed 时才加载模型

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "本地 Embedding 需要 sentence-transformers + torch。"
                    "原型档可改 EMBEDDING_BACKEND=deterministic 无需依赖跑通链路。"
                ) from exc
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        """文本 -> 向量。query 加指令前缀，passage 不加（Qwen3 官方用法）。"""
        model = self._load()
        inputs = texts
        if is_query and self.query_instruction:
            inputs = [self.query_instruction + t for t in texts]
        vectors = model.encode(
            inputs,
            normalize_embeddings=self.normalize,
            convert_to_numpy=True,
        )
        # 支持 MRL 套娃维度：截断到配置维度，再重新归一化
        result = []
        for vec in vectors:
            v = list(map(float, vec[: self.dimension]))
            if self.normalize:
                norm = math.sqrt(sum(x * x for x in v)) or 1.0
                v = [x / norm for x in v]
            result.append(v)
        return result


class DeterministicEmbedder:
    """无依赖的确定性向量：仅为验证链路，不保证语义质量。

    原理：把文本切成字符 n-gram，用 md5 哈希投到固定维度的桶里累加，
    最后 L2 归一化。相同文本必得相同向量，相似文本（共享 n-gram）向量接近。
    """
    model_id = "deterministic-hash-v1"

    def __init__(self, dimension: int = 1024, ngram: int = 2):
        self.dimension = dimension
        self.ngram = ngram

    def _vector(self, text: str) -> list[float]:
        buckets = [0.0] * self.dimension
        # 加边界符，保证短文本也有 n-gram
        padded = f"^{text}$"
        grams = [padded[i:i + self.ngram] for i in range(len(padded) - self.ngram + 1)]
        for g in grams:
            idx = int(md5(g.encode("utf-8")).hexdigest(), 16) % self.dimension
            buckets[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in buckets)) or 1.0
        return [x / norm for x in buckets]

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        # 演示档忽略 is_query（无模型可加指令）；保留参数是为了协议一致
        return [self._vector(t) for t in texts]
