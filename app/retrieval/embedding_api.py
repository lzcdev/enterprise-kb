"""API Embedding 实现：调用 OpenAI 兼容的嵌入接口（DeepSeek/通义/OpenAI）。

和 LocalEmbedder 满足同一协议，工厂按 EMBEDDING_BACKEND 切换。
优雅降级：没配 API key 或没装 openai 时，真正调用才报错。
"""
import math

from app.interfaces import Embedder


class ApiEmbedder:
    def __init__(
        self,
        model_name: str = "text-embedding-3-small",
        dimension: int = 1024,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
    ):
        self.model_name = model_name
        self.model_id = f"api-{model_name}"
        self.dimension = dimension
        self.api_key = api_key
        self.base_url = base_url
        self._client = None

    def _load(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("API Embedding 需要 openai SDK：pip install openai") from exc
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        client = self._load()
        resp = client.embeddings.create(
            model=self.model_name,
            input=texts,
            dimensions=self.dimension,  # 支持 MRL 的 API 可指定维度
        )
        # 按 index 排序，防止 API 返回乱序
        ordered = sorted(resp.data, key=lambda d: d.index)
        result = []
        for item in ordered:
            v = list(map(float, item.embedding))[: self.dimension]
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            result.append([x / norm for x in v])  # 归一化，检索用内积=余弦
        return result
