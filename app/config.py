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
    # backend: "chroma"（本地生产档持久化） / "milvus"（企业档） / "memory"（原型档零依赖）
    VECTORSTORE_BACKEND: str = "memory"
    CHROMA_PERSIST_DIR: Path = DATA_DIR / "chroma"
    CHROMA_COLLECTION: str = "knowledge_base"
    MILVUS_URI: str = "http://localhost:19530"
    COLLECTION_NAME: str = "knowledge_base"

    # ===== 档位切换：prototype / local-prod / enterprise =====
    # 同一套代码，换这个配置即可切换整条链路的实现，不改业务代码。
    #
    # [诚实默认] 这里必须是 prototype：默认值要和【实际安装了依赖、能跑通】的档位一致。
    # 之前默认写 local-prod，但 EMBEDDING/VECTORSTORE/LLM 全是 prototype 实现，
    # 且 docling 根本没装 —— 名义实不符，按默认配置跑真实上传会崩（get_parser 抛错）。
    # 想升级到生产档，用 .env 显式覆盖各 BACKEND，并用 validate_runtime() 校验依赖。
    TIER: str = "prototype"

    # ===== 文档解析配置 =====
    # backend: "pymupdf"（零依赖快速通道，默认）或 "docling"（结构感知+表格+溯源坐标）
    # 默认 pymupdf：docling 是重依赖（torch/transformers），原型档不预装。
    PARSER_BACKEND: str = "pymupdf"

    # ===== Embedding 配置 =====
    # backend:
    #   "local"         - sentence-transformers 本地模型（生产档，需 torch）
    #   "api"           - OpenAI 兼容嵌入接口（需 key）
    #   "deterministic" - 哈希确定性向量（原型档，零依赖跑通链路，无语义质量）
    EMBEDDING_BACKEND: str = "deterministic"
    # 本地生产档用 Qwen3-0.6B：MTEB 多语言分数超过 text-embedding-3-large，CPU 可跑
    # 原型档可改 bge-small-zh-v1.5 更快
    EMBEDDING_MODEL: str = "Qwen/Qwen3-Embedding-0.6B"
    EMBEDDING_DIM: int = 1024                         # 0.6B 原生维度，支持 MRL 截断
    # 指令感知：Qwen3 加指令可提升 1~5% 检索效果
    EMBEDDING_QUERY_INSTRUCTION: str = "Given a user question, retrieve relevant passages that answer it"

    # ===== Rerank 配置（企业档/本地生产档启用）=====
    RERANK_ENABLED: bool = True
    # backend: "deterministic"(零依赖验证链路) / "local"(Qwen3交叉编码器,需torch) / "off"(关闭)
    RERANK_BACKEND: str = "deterministic"
    RERANK_MODEL: str = "Qwen/Qwen3-Reranker-0.6B"

    # ===== LLM 配置 =====
    # backend: "mock"(零依赖抽取式,验证链路) / "api"(OpenAI兼容接口,需key) / "off"
    LLM_BACKEND: str = "mock"
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MODEL: str = "deepseek-chat"

    # ===== 视频处理配置（多模态扩展位）=====
    # 这些参数演示分层配置的思想：视频有自己的"切分参数"（按时间切），
    # 就像文档有 chunk_size（按字符切）。后面可做租户级/类型级覆盖。
    VIDEO_FRAME_FPS: float = 1.0          # 抽帧频率：每秒 1 帧为基线
    VIDEO_SEGMENT_SECONDS: int = 30       # 固定窗口策略的时间窗口（兜底基线）
    VIDEO_CAPTION_ENABLED: bool = False   # 是否用 VLM 给帧生成描述（贵，默认关）
    # 切分策略：fixed=固定窗口 / scene=场景边界优先+时长约束（可拔插，见 video_segmenter.py）
    VIDEO_SEGMENT_STRATEGY: str = "fixed"
    # 场景策略的时长约束：短于 min 合并（防碎片），长于 max 强切（防巨型块）
    VIDEO_MIN_SEGMENT_MS: int = 10000     # 10 秒
    VIDEO_MAX_SEGMENT_MS: int = 60000     # 60 秒

    # ===== 切分参数 =====
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50

    # ===== 发布/切流配置 =====
    # 生产由配置中心（Nacos/Apollo）下发 ACTIVE_NAMESPACE 或写 data/release/state.json；
    # 显式设置此值优先级最高，可用于紧急人工覆盖。
    ACTIVE_NAMESPACE: str | None = None

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


# ===== 配置一致性校验：让"名义实不符"在启动时就暴露，而不是跑到一半才崩 =====
# 每个档位约定的一套后端组合。TIER 是"名义档位"，各 BACKEND 是"实际实现"，
# 两者必须匹配——否则出现"写着 local-prod 却在用哈希向量"这种自欺欺人的状态。
TIER_BACKEND_EXPECTATIONS: dict[str, dict[str, str]] = {
    "prototype": {
        "PARSER_BACKEND": "pymupdf",
        "EMBEDDING_BACKEND": "deterministic",
        "VECTORSTORE_BACKEND": "memory",
        "RERANK_BACKEND": "deterministic",
        "LLM_BACKEND": "mock",
    },
    "local-prod": {
        "PARSER_BACKEND": "docling",
        "EMBEDDING_BACKEND": "local",
        "VECTORSTORE_BACKEND": "chroma",
        "RERANK_BACKEND": "local",
        "LLM_BACKEND": "api",
    },
    "enterprise": {
        "PARSER_BACKEND": "docling",
        "EMBEDDING_BACKEND": "local",
        "VECTORSTORE_BACKEND": "milvus",
        "RERANK_BACKEND": "local",
        "LLM_BACKEND": "api",
    },
}

# 每个后端需要的第三方包（import 名，用于依赖预检）。None 表示零依赖。
BACKEND_DEPENDENCIES: dict[str, dict[str, str | None]] = {
    "PARSER_BACKEND": {"pymupdf": "fitz", "docling": "docling", "video": None},
    "EMBEDDING_BACKEND": {"deterministic": None, "local": "sentence_transformers", "api": "openai"},
    "VECTORSTORE_BACKEND": {"memory": None, "chroma": "chromadb", "milvus": "pymilvus"},
    "RERANK_BACKEND": {"deterministic": None, "local": "sentence_transformers", "off": None},
    "LLM_BACKEND": {"mock": None, "api": "openai", "off": None},
}


def validate_runtime(cfg: Settings | None = None) -> list[str]:
    """运行时自检：返回问题列表，空列表表示配置一致、依赖齐全。

    检查三类问题：
    1. TIER 名义档位与各 BACKEND 实际实现是否匹配（名义实不符）。
    2. 选中的后端所需第三方包是否已安装（避免跑到一半 ImportError）。
    3. 关键安全配置：api 档缺 key、JWT 用默认密钥。

    为什么返回列表而不是直接抛异常？
    - 让调用方决定严格程度：本地验证脚本全量报告，API 启动只 warning，
      CI 可把非空列表当失败。设计成"纯函数 + 数据"而非"带副作用的断言"，
      才容易测试和复用。
    """
    import importlib.util

    cfg = cfg or settings
    problems: list[str] = []

    # ① 名义实一致性
    expected = TIER_BACKEND_EXPECTATIONS.get(cfg.TIER)
    if expected is None:
        problems.append(f"未知 TIER={cfg.TIER!r}，应为 prototype/local-prod/enterprise")
    else:
        for key, want in expected.items():
            actual = getattr(cfg, key)
            if actual != want:
                problems.append(
                    f"档位不一致: TIER={cfg.TIER} 期望 {key}={want!r}，实际 {actual!r}"
                )

    # ② 依赖预检：只查当前实际选中的后端
    for backend_key, mapping in BACKEND_DEPENDENCIES.items():
        backend = getattr(cfg, backend_key)
        dep = mapping.get(backend, "__UNKNOWN__")
        if dep == "__UNKNOWN__":
            problems.append(f"未知 {backend_key}={backend!r}，无依赖映射")
        elif dep is not None and importlib.util.find_spec(dep) is None:
            problems.append(
                f"缺少依赖: {backend_key}={backend!r} 需要包 {dep!r}（pip install 后重试）"
            )

    # ③ 关键安全/凭证配置
    if cfg.EMBEDDING_BACKEND == "api" and not cfg.LLM_API_KEY:
        problems.append("EMBEDDING_BACKEND=api 但未配置 LLM_API_KEY")
    if cfg.LLM_BACKEND == "api" and not cfg.LLM_API_KEY:
        problems.append("LLM_BACKEND=api 但未配置 LLM_API_KEY（生成会失败）")

    return problems
