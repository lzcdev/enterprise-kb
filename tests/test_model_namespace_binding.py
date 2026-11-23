"""模型-索引绑定测试：防止"切了 namespace 却用错 embedder"这个致命回归。

这是蓝绿迁移最容易出错、单测最该覆盖的点：
蓝索引是 Deterministic 向量、绿索引是 Qwen3 向量，二者的 query 向量必须用
各自建索引的模型生成。若哪天有人图省事让 Retriever 永远用全局 embedder，
切流后检索会静默变成噪音——但测试不报错、用户只会觉得"答案变差了"。
这些测试把约束固化成断言，让错误在 CI 就暴露。

全部零依赖：只实例化 embedder，不加载 torch 模型。
"""
import pytest

from app.factory import (
    embedder_for_namespace,
    parse_model_id,
    _model_id_to_name,
)
from app.retrieval.embedding_local import DeterministicEmbedder, LocalEmbedder
from app.retrieval.retriever import Retriever

BLUE_NS = "kb__deterministic-hash-v1__v1"
GREEN_NS = "kb__qwen-qwen3-embedding-0.6b__v1"


class TestParseModelId:
    def test_标准格式(self):
        assert parse_model_id(BLUE_NS) == "deterministic-hash-v1"
        assert parse_model_id(GREEN_NS) == "qwen-qwen3-embedding-0.6b"

    def test_带版本号v2(self):
        assert parse_model_id("kb__deterministic-hash-v1__v2") == "deterministic-hash-v1"

    @pytest.mark.parametrize("bad", ["bad_format", "", "kb__onlytwo", "nokb__x__v1"])
    def test_非法格式返回None(self, bad):
        assert parse_model_id(bad) is None


class TestEmbedderForNamespace:
    def test_蓝索引用确定性模型(self):
        emb = embedder_for_namespace(BLUE_NS)
        assert isinstance(emb, DeterministicEmbedder)
        assert emb.model_id == "deterministic-hash-v1"

    def test_绿索引用Qwen3模型(self):
        emb = embedder_for_namespace(GREEN_NS)
        assert isinstance(emb, LocalEmbedder)
        assert emb.model_id == "qwen-qwen3-embedding-0.6b"

    def test_同namespace复用实例(self):
        e1 = embedder_for_namespace(GREEN_NS)
        e2 = embedder_for_namespace(GREEN_NS)
        assert e1 is e2, "模型加载很贵，同 namespace 必须复用实例"

    def test_未知namespace回退全局不崩(self):
        # 可用性优先：解析失败不能让检索服务挂掉
        emb = embedder_for_namespace("unknown__format")
        assert hasattr(emb, "embed") and hasattr(emb, "model_id")

    def test_model_id反推模型名(self):
        name = _model_id_to_name("qwen-qwen3-embedding-0.6b")
        # 配置里的 EMBEDDING_MODEL 就是它，应原样返回
        assert name.lower().replace("/", "-") == "qwen-qwen3-embedding-0.6b"


class TestRetrieverBinding:
    def test_默认不联动保持向后兼容(self):
        r = Retriever(namespace=GREEN_NS)
        # 默认用全局 embedder（deterministic），不因 namespace 改变
        assert isinstance(r.embedder, DeterministicEmbedder)

    def test_开启联动后绿索引绑定Qwen3(self):
        r = Retriever(namespace=GREEN_NS, bind_embedder_to_namespace=True)
        assert isinstance(r.embedder, LocalEmbedder)

    def test_显式传入embedder优先级最高(self):
        explicit = DeterministicEmbedder()
        r = Retriever(embedder=explicit, namespace=GREEN_NS,
                      bind_embedder_to_namespace=True)
        assert r.embedder is explicit, "显式注入应覆盖自动绑定"

    def test_蓝绿两套绑定不同模型(self):
        blue = Retriever(namespace=BLUE_NS, bind_embedder_to_namespace=True)
        green = Retriever(namespace=GREEN_NS, bind_embedder_to_namespace=True)
        assert isinstance(blue.embedder, DeterministicEmbedder)
        assert isinstance(green.embedder, LocalEmbedder)
        assert blue.embedder.model_id != green.embedder.model_id
