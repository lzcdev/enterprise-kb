"""多租户注册与配置中心。

职责（面试要能讲清三层）：
1. 身份识别：租户是否存在、是否被禁用 —— 挡住"随便传个 tenant_id 就能查"的漏洞。
2. 权限/配额校验：租户能用哪些文档类型、文档数/请求数是否超额。
3. 配置分层覆盖：全局 → 租户 → 文档类型 → 文档，逐层覆盖，只存差异。

为什么要单独一个模块，而不是塞进 config.py？
- config.py 是【全局静态配置】，进程启动时就定了；租户配置是【运行时数据】，
  可以来自数据库，随时增删改。两者生命周期不同，必须分开。
- 注册表是"唯一知道租户长什么样"的地方，API/Retriever/建库都来问它，
  避免 tenant 判断散落各处（散落 = 漏判 = 越权漏洞）。

设计原则：
- 只存差异(override)：租户配置里不复制全套默认值，只写和默认不一样的那几个键。
  几万个租户也不会产生几万份重复配置。
- 覆盖链返回"来源标记"：每个生效参数来自哪一层，可视化/排障时能一眼看清。
"""
from dataclasses import dataclass, field
from typing import Optional, Literal, Protocol, runtime_checkable

from app.config import settings

SourceType = Literal["pdf", "docx", "md", "txt", "video", "audio"]


# ===== 存储后端协议：注册表逻辑与存储介质解耦的关键 =====
# 为什么要抽象出这一层？
#   TenantRegistry 的身份校验、四层覆盖链是【业务逻辑】，不该关心数据存在内存还是数据库。
#   定义这个 Protocol 后，内存 dict 和 PostgreSQL 都只是"实现"，
#   换存储只改工厂里的一行实例化，业务代码（API/RAGService）零改动。
#   这就是 interfaces.py 那套可拔插思想在租户层的复用。
@runtime_checkable
class TenantStore(Protocol):
    """租户配置的存取契约。任何实现只需提供这三个方法。"""

    def get(self, tenant_id: str) -> Optional["TenantConfig"]: ...
    def upsert(self, config: "TenantConfig") -> None: ...
    def list_all(self) -> list["TenantConfig"]: ...


class InMemoryTenantStore:
    """默认存储：进程内 dict。零依赖，适合原型档与单元测试。"""

    def __init__(self, tenants: Optional[list["TenantConfig"]] = None):
        self._data: dict[str, TenantConfig] = {}
        for t in tenants or []:
            self.upsert(t)

    def get(self, tenant_id: str) -> Optional["TenantConfig"]:
        return self._data.get(tenant_id)

    def upsert(self, config: "TenantConfig") -> None:
        self._data[config.tenant_id] = config

    def list_all(self) -> list["TenantConfig"]:
        return list(self._data.values())


# ===== 异常类型：区分不同的拒绝原因，API 层据此返回不同状态码 =====
class TenantError(Exception):
    """租户相关错误的基类，方便 API 层统一捕获。"""


class TenantNotFoundError(TenantError):
    """租户未注册。"""


class TenantSuspendedError(TenantError):
    """租户被禁用（欠费/违规/过期）。"""


class SourceTypeNotAllowedError(TenantError):
    """租户无权使用该文档类型（比如没买视频功能）。"""


class QuotaExceededError(TenantError):
    """租户超出配额（文档数上限）。"""


@dataclass
class TenantConfig:
    """单个租户的配置。

    Attributes:
        tenant_id: 租户唯一标识，写进每条向量记录的 metadata。
        name: 展示名，仅用于可读性。
        status: "active" / "suspended"，禁用后所有请求直接拒绝。
        allowed_source_types: 该租户允许上传的文档类型；None 表示不限制。
        max_documents: 文档数配额；None 表示不限。
        overrides: 租户级参数覆盖（只写差异键）。
        type_overrides: 文档类型级覆盖 —— 差异最大的来源，吸收大部分定制。
            形如 {"video": {"chunk_size": 300}, "pdf": {"chunk_size": 800}}。
        doc_overrides: 单文档级覆盖（扩展位，极少用）。
    """
    tenant_id: str
    name: str = ""
    status: Literal["active", "suspended"] = "active"
    allowed_source_types: Optional[list[SourceType]] = None
    max_documents: Optional[int] = None
    overrides: dict = field(default_factory=dict)
    type_overrides: dict[str, dict] = field(default_factory=dict)
    doc_overrides: dict[str, dict] = field(default_factory=dict)


@dataclass
class ResolvedConfig:
    """覆盖链的解析结果：最终生效值 + 每个值的来源。

    sources 形如 {"chunk_size": "tenant", "top_k": "global"}，
    表示 chunk_size 被租户层覆盖，top_k 仍用全局默认。
    这个"来源标记"是排障利器：客户说参数没生效，一眼看出被哪层盖了。
    """
    chunk_size: int
    chunk_overlap: int
    top_k: int
    top_n: int
    score_threshold: float
    sources: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        """转成普通字典（不含 sources），方便传给函数参数。"""
        return {
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "top_k": self.top_k,
            "top_n": self.top_n,
            "score_threshold": self.score_threshold,
        }


class TenantRegistry:
    """租户注册表：身份识别 + 配置覆盖的唯一入口。

    教学版用内存字典；生产版把 _tenants 换成数据库查询即可，
    对外接口不变 —— 这就是"契约稳定、实现可换"的价值。
    """

    # 全局默认参数：从 settings 取，避免和 config.py 两处维护。
    GLOBAL_DEFAULTS = {
        "chunk_size": settings.CHUNK_SIZE,
        "chunk_overlap": settings.CHUNK_OVERLAP,
        "top_k": settings.TOP_K,
        "top_n": settings.TOP_N,
        "score_threshold": settings.SCORE_THRESHOLD,
    }

    def __init__(self, tenants: Optional[list[TenantConfig]] = None,
                 store: Optional[TenantStore] = None):
        # 依赖注入：默认用内存 store，生产由工厂注入 PG store。
        # 业务逻辑（下面所有方法）只跟 store 接口打交道，不知道背后是 dict 还是数据库。
        self._store: TenantStore = store if store is not None else InMemoryTenantStore()
        for t in tenants or []:
            self.register(t)

    def register(self, tenant: TenantConfig) -> None:
        """注册/更新租户。写操作委托给存储后端（内存版写 dict，PG 版 UPSERT）。"""
        self._store.upsert(tenant)

    def get(self, tenant_id: str) -> Optional[TenantConfig]:
        """查询租户，不存在返回 None（不抛异常，供非强制场景用）。"""
        return self._store.get(tenant_id)

    def list_tenants(self) -> list[str]:
        """列出所有租户 id，管理接口用。"""
        return [t.tenant_id for t in self._store.list_all()]

    def list_all_tenants(self) -> list["TenantConfig"]:
        """列出完整租户配置（播种空库时用）。"""
        return self._store.list_all()

    def require_active(self, tenant_id: str) -> TenantConfig:
        """身份校验：必须已注册且状态为 active，否则抛异常。

        这是防越权的第一道闸门 —— 不再让任意 tenant_id 直接查库。
        """
        tenant = self.get(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(f"租户未注册: {tenant_id}")
        if tenant.status != "active":
            raise TenantSuspendedError(f"租户已禁用: {tenant_id}")
        return tenant

    def check_ingest_allowed(self, tenant_id: str, source_type: SourceType,
                             current_documents: int = 0) -> None:
        """建库前置校验：类型权限 + 文档配额。

        Args:
            tenant_id: 租户 id。
            source_type: 要入库的文档类型。
            current_documents: 该租户当前已有文档数，用于配额判断。
        """
        tenant = self.require_active(tenant_id)
        if (tenant.allowed_source_types is not None
                and source_type not in tenant.allowed_source_types):
            raise SourceTypeNotAllowedError(
                f"租户 {tenant_id} 无权上传 {source_type} 类型文档"
            )
        if (tenant.max_documents is not None
                and current_documents >= tenant.max_documents):
            raise QuotaExceededError(
                f"租户 {tenant_id} 文档数超额: {current_documents}/{tenant.max_documents}"
            )

    def resolve_config(self, tenant_id: str, *, source_type: Optional[SourceType] = None,
                       doc_id: Optional[str] = None) -> ResolvedConfig:
        """四层覆盖链：全局 → 租户 → 文档类型 → 文档。

        实现就是逐层 dict.update()：越后执行的层覆盖同名键。
        同时记录每个键最后被哪层写入，形成来源追踪。
        """
        tenant = self.require_active(tenant_id)
        merged = dict(self.GLOBAL_DEFAULTS)
        sources = {k: "global" for k in merged}

        # 逐层覆盖：把每一层的差异写进 merged，同名键后者胜出。
        layers = [
            ("tenant", tenant.overrides),
            ("type", tenant.type_overrides.get(source_type, {}) if source_type else {}),
            ("doc", tenant.doc_overrides.get(doc_id, {}) if doc_id else {}),
        ]
        for layer_name, overrides in layers:
            for key, value in overrides.items():
                if key not in merged:
                    continue  # 忽略未知键，防止拼错的参数污染配置
                merged[key] = value
                sources[key] = layer_name

        return ResolvedConfig(**merged, sources=sources)

    def resolve_config_safe(self, tenant_id: str, **kwargs) -> ResolvedConfig:
        """非强制场景（内部编排层）：租户未注册/禁用时降级到全局默认，不抛异常。

        为什么要有这个"软版本"？
        - 信任边界在 API 层：外部请求进来时强制 require_active() 校验身份。
        - 一旦请求已被上层校验放行，内部 RAGService 只关心"这个租户有没有定制参数"，
          没注册就用默认值，不该因为查配置再把请求打挂。
        - 两者职责分开：硬校验守门，软解析取参。
        """
        try:
            return self.resolve_config(tenant_id, **kwargs)
        except TenantError:
            defaults = dict(self.GLOBAL_DEFAULTS)
            return ResolvedConfig(
                **defaults, sources={k: "global" for k in defaults}
            )


def default_registry() -> TenantRegistry:
    """教学 demo 的预置租户。生产环境应从数据库加载。

    特意造三个角色，覆盖三种典型场景：
    - default: 兜底租户，无覆盖，全用全局默认。
    - law_firm: 律所大客户，租户级细化切分 + 提高召回（花钱定制）。
    - startup: 小客户，只买了 pdf/md，且视频类型未授权（演示越权拦截）。
    """
    return TenantRegistry([
        TenantConfig(tenant_id="default", name="默认租户"),
        TenantConfig(
            tenant_id="law_firm",
            name="某某律师事务所",
            overrides={"chunk_size": 300, "chunk_overlap": 30, "top_k": 20},
            type_overrides={"pdf": {"chunk_size": 400}},
        ),
        TenantConfig(
            tenant_id="startup",
            name="初创公司",
            allowed_source_types=["pdf", "md", "txt"],
            max_documents=50,
        ),
    ])


# 模块级单例：全进程共享同一份注册表。
# 生产档通过 TENANT_STORE_BACKEND=pg 把数据源换成 PostgreSQL。
_registry: Optional[TenantRegistry] = None


def build_registry() -> TenantRegistry:
    """按配置装配注册表——这是"存储可拔插"的唯一决策点。

    - memory（默认）：内存 store + 预置 demo 租户，零依赖，测试/原型档用。
    - pg：PostgreSQL store；此时租户数据来自数据库，不再用 default_registry 的预置值。
      数据库为空时（首次启动）才用预置租户初始化，保证开箱即用。

    延迟 import PGTenantStore：psycopg2 是可选依赖，prototype 档不装也能 import registry。
    """
    backend = getattr(settings, "TENANT_STORE_BACKEND", "memory").lower()
    if backend == "memory":
        return default_registry()
    if backend == "pg":
        from app.tenancy.store_pg import PGTenantStore

        store = PGTenantStore(dsn=settings.PG_DSN)
        store.init_schema()
        registry = TenantRegistry(store=store)
        # 空库播种：把 demo 租户写进去，避免首次启动时没有任何租户可用。
        if not store.list_all():
            for t in default_registry().list_all_tenants():
                store.upsert(t)
        return registry
    raise ValueError(f"未知的 TENANT_STORE_BACKEND: {backend!r}（应为 memory / pg）")


def get_registry() -> TenantRegistry:
    """获取全局注册表单例。测试可用 _reset_registry() 重置。"""
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def _reset_registry() -> None:
    """仅供测试：清空单例，让每个测试用干净的注册表。"""
    global _registry
    _registry = None
