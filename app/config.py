"""全局配置：用 pydantic-settings 从环境变量/.env 读取配置。

为什么用 pydantic-settings？
- 类型校验：配置写错类型启动时就报错，不会跑到一半才崩
- 环境隔离：开发/生产用不同 .env，代码不用改
- 这是企业项目的标准做法，面试/简历都加分
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


class Settings(BaseSettings):
    # 项目根目录
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    DATA_DIR: Path = BASE_DIR / "data"

    # ===== 向量库 =====
    CHROMA_PERSIST_DIR: Path = DATA_DIR / "chroma"
    COLLECTION_NAME: str = "knowledge_base"

    # ===== 档位切换：prototype / local-prod / enterprise =====
    # 同一套代码，换这个配置即可切换整条链路的实现，不改业务代码
    TIER: str = "local-prod"

    # ===== 文档解析配置 =====
    # backend: "docling"（结构感知+表格+溯源坐标）或 "pymupdf"（快速原型）
    PARSER_BACKEND: str = "docling"

    # ===== Embedding 配置 =====
    # backend: "local"（sentence-transformers）或 "api"（OpenAI 兼容接口）
    EMBEDDING_BACKEND: str = "local"
    # 本地生产档用 Qwen3-0.6B：MTEB 多语言分数超过 text-embedding-3-large，CPU 可跑
    # 原型档可改 bge-small-zh-v1.5 更快
    EMBEDDING_MODEL: str = "Qwen/Qwen3-Embedding-0.6B"
    EMBEDDING_DIM: int = 1024                         # 0.6B 原生维度，支持 MRL 截断
    # 指令感知：Qwen3 加指令可提升 1~5% 检索效果
    EMBEDDING_QUERY_INSTRUCTION: str = "Given a user question, retrieve relevant passages that answer it"

    # ===== Rerank 配置（企业档/本地生产档启用）=====
    RERANK_ENABLED: bool = True
    RERANK_MODEL: str = "Qwen/Qwen3-Reranker-0.6B"

    # ===== LLM 配置 =====
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MODEL: str = "deepseek-chat"

    # ===== 切分参数 =====
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50

    # ===== 检索参数 =====
    TOP_K: int = 10          # 粗召回数量
    TOP_N: int = 3           # 重排后送入 LLM 的数量
    SCORE_THRESHOLD: float = 0.3   # 相似度低于此值认为"没找到"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
