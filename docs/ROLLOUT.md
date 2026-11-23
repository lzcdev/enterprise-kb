# 生产级改造执行手册

> 把 `PRODUCTION.md` 的"改什么"落成"按什么顺序改、每步做什么、要准备什么、怎么验收通过"。
> 五个波次严格串行：**先修正确性与安全，再补基础设施，再换真模型，最后上规模**。
> 倒过来做（先上 K8s 再修鉴权）是新手最常见的浪费——把玩具部署到集群上，玩具还是玩具。

---

## 全局原则

1. **每一步都必须可回滚**：改配置开关能退回上一档，不删旧代码。
2. **每一步都有验收命令**：跑完贴出真实输出，不靠"我觉得对了"。
3. **每一步都补测试**：生产化不是加功能，是给已有功能加约束，测试数只增不减（当前 32）。
4. **依赖倒置不破**：新增实现一律走 `interfaces.py` 的 Protocol + `factory.py` 配置，不在业务代码里 `if backend == ...`。

---

## 波次 0 · 安全与可观测加固（零依赖，现在就能做）

**为什么排第一**：这是唯一"不改就 absolutely 不能上线"的一波，而且不需要任何 key、模型、Docker。先做它能立刻把项目从"裸奔"变成"有边界"。

### 0.1 租户身份从 token 解析（修真实越权漏洞）

- **现状**：`Question.tenant_id` 在请求体里，客户端随便填别人的 id 就能查别人数据。
- **改法**：写一个 `app/security/auth.py`，提供 `get_current_tenant(authorization: str = Header(...)) -> str`：校验 JWT 签名（HS256，密钥从环境变量 `JWT_SECRET` 读）→ 从 payload 取 `tenant_id`。API 层用 `Depends(get_current_tenant)` 注入，`Question` 里的 `tenant_id` 字段废弃或仅管理端可用。
- **签发端**：加一个 `/auth/token` 接口（仅测试/管理用），输入租户名返回签名 token，方便你本地 curl 验证。
- **验收**：不带 token 请求 `/ask` → 401；带 A 租户 token 但 body 里写 B → 返回的仍是 A 的数据（证明 body 被忽略）；篡改 token 签名 → 401。

### 0.2 输入校验与 prompt injection 基础防护

- 问题长度上限（如 1000 字符）、空问题拒绝、常见注入模式（"忽略上面的指令"）记录日志并拒绝。
- 放在 API 层，用 pydantic `Field(max_length=...)` + 一个 `validate_question()` 函数。
- **验收**：超长问题返回 422；注入串返回 400 且日志里有标记。

### 0.3 trace_id 贯穿全链路 + 结构化日志

- 每个请求生成 `trace_id`（UUID），塞进 logging 的 `extra`，从 API → RAGService → Retriever → Generator 一路传。
- 日志改成 JSON 行（`{"ts":..., "trace_id":..., "stage":..., "elapsed_ms":...}`），stdout 输出。
- **验收**：一次 `/ask` 能在日志里看到 4 个 stage 的耗时，且 trace_id 相同。

### 0.4 引用校验（防 LLM 编造引用）

- Generator 解析出 `[n]` 后，检查被引用 chunk 的文本与答案句子是否有实质重叠，不通过就丢掉该引用并在答案标"引用存疑"。
- **验收**：注入一条假引用（MockLLM 故意输出 `[9]`）→ 被拦下并告警。

### 0.5 BM25 混合检索（生产 RAG 标配）

- **为什么**：向量检索对"型号 XJ-200"、"订单号 20241123"这类精确串很弱（有损压缩把字符序列拍扁了），BM25 恰好擅长。生产都用两者融合。
- **改法**：`app/retrieval/hybrid.py`，用 `rank_bm25`（纯 Python，很小）+ `jieba` 中文分词；向量 Top20 与 BM25 Top20 用 **RRF（Reciprocal Rank Fusion）** 融合：`score(d) = Σ 1/(k + rank_i(d))`，k 常取 60。不比原始分数（量纲不同），只比排名。
- **验收**：查"XJ-200 保修期"能命中含该型号的 chunk（纯向量档此前命中不了）；`show_hybrid.py` 打印两路各自 top 与融合后 top。

### 波次 0 产出

新增 `security/auth.py`、`retrieval/hybrid.py`；改 `api/main.py`、`generation/generator.py`、日志配置；新增 `scripts/show_hybrid.py`、`tests/test_security.py`。
**测试数**：32 → 40+。**不需要任何 key 或下载**。

---

## 波次 1 · 数据持久化（需要 Docker，不需要 key）

**为什么排第二**：波次 0 把边界立住了，现在可以把"进程一停就没"的内存数据挪到磁盘。先做持久化再换模型，因为换模型要全量重建索引——总得先有个能持久的库。

### 1.1 起基础设施（docker-compose v1）

- 写 `deploy/docker-compose.yml`：postgres:16、redis:7、minio、chroma（先不上 Milvus，Chroma 够验证持久化链路，接口本来就兼容）。
- 你本机有 `docker-compose` v1（5.6.1），够用；没有 compose v2 插件不影响。
- 数据卷挂到 `deploy/volumes/`，写进 `.gitignore`。

### 1.2 租户注册表落 PostgreSQL

- `app/tenancy/store_pg.py`：实现与内存注册表一致的接口（换实现不改调用方，这是 `interfaces.py` 抽象的价值兑现）。三张表：`tenants`、`documents`、`ingest_tasks`。
- 用 `psycopg2-binary`（轻），连接池用 `psycopg2.pool`。
- **验收**：服务重启后租户配置还在；`show_tenancy.py` 改用 PG 后端跑通。

### 1.3 向量切 Chroma 持久化

- `VECTORSTORE_BACKEND=chroma`，namespace 仍编码模型版本。全量灌一遍样本数据，停服务再启动，数据还在。
- **验收**：重启前后同一 query 返回相同的 top chunk id。

### 1.4 原文存 MinIO

- 上传的原始文件放 MinIO，向量库 metadata 只存 `object_key`，不再依赖本地路径。
- **验收**：删掉本地 `data/测试产品手册.md`，服务仍能溯源到原文（从 MinIO 取）。
- **状态：未做**。本地 Docker 拉镜像被代理阻断（colima 内无法访问宿主机代理），
  且本机没有 MinIO 服务；不假装完成。

### 1.5 本波次实际完成与验收记录

| 子项 | 状态 | 验收命令 | 实测结果 |
|---|---|---|---|
| 注册表存储抽象 | ✅ | `pytest -q` | 75 passed + 5 skipped（默认内存档不变） |
| 租户配置落 PG | ✅ | `verify_pg_persistence.py` | 双进程模拟重启，配置与覆盖链都在 |
| 空库播种 | ✅ | 独立空库 + `TENANT_STORE_BACKEND=pg` | default/law_firm/startup 自动写入 |
| PG 集成测试 | ✅ | `KB_PG_TEST=1 pytest tests/test_tenancy_pg.py` | 5 passed |
| 向量落 Chroma | ✅ | `verify_chroma_persistence.py` | 双进程重启前后 top3 id 完全一致 |
| 原文存 MinIO | ❌ | — | 无 MinIO 服务，未做 |

**环境说明**：Docker daemon（colima）在虚拟机内，无法用宿主机的 HTTP 代理拉镜像；
改用本机已装的 PostgreSQL 18（brew）验证持久化，连接串 `postgresql://lzc@127.0.0.1:5432/enterprise_kb_test`。
这不影响代码正确性——`PGTenantStore` 只认 DSN，不关心 PG 跑在容器还是本机。

**踩坑记录**（都已写进测试）：
1. psycopg2 对 JSONB 列自动反序列化：读回已是 dict/list，再 `json.loads()` 会 TypeError。
2. Python list 传给 JSONB 列会被适配成 PG `text[]`，报类型不匹配；必须用 `psycopg2.extras.Json` 包装。
3. store 连接不关，测试删库报 `database is being accessed by other users`；补 `close()`。

### 资源需求
- Python 包：`psycopg2-binary`、`chromadb`、`minio`（都是轻/中量，无需 GPU）
- Docker 镜像：postgres/redis/minio/chroma 四个（约 1-2GB 下载）
- **不需要任何 key**

---

## 波次 2 · 真语义模型（需要下载模型，不需要 key）

**为什么排第三**：库持久化了，才有意义谈"换更好的 embedding 重建索引"。这一波是你从"看流程"跨到"看质量"的关键，也是简历上最值钱的一次升级——因为你能用真实数据对比前后差距。

### 2.1 装推理依赖

- `pip install torch transformers`（arm64 macOS 有原生 wheel，你 24G 内存跑 0.6B 模型没问题）。约 1-2GB 下载。

### 2.2 接入 Qwen3-Embedding-0.6B

- 已有的 `LocalEmbedder` 骨架就是为这一刻写的，补上真正的模型加载和 batch encode。
- 注意点：加 query 指令前缀、L2 归一化、batch 32（当前逐条 embed 是性能坑）。
- 模型权重 HuggingFace 公开，**不需要 token**；国内网络若慢可设 `HF_ENDPOINT=https://hf-mirror.com`。

### 2.3 接入 Qwen3-Reranker-0.6B

- `LocalReranker` 同理，交叉编码器批量打分。
- **关键验收**：同一批 query，对比 deterministic 档和真语义档的 Top1 与分数。之前的 `show_embedding.py` 分数只有 0.0488，真语义档应该显著更高。

### 2.4 用 namespace 蓝绿迁移，不覆盖旧索引

- 新模型建新 namespace → 全量重建 → 影子读对比 → 切 `ACTIVE_NAMESPACE` → 保留旧索引观察期。
- 这正是 `PRODUCTION.md` 3.4 的流程，这一波把它从纸面变成你做过的实事。

### 资源需求
- Python 包：`torch`、`transformers`、`accelerate`（约 1-2GB）
- 模型权重：Qwen3-Embedding-0.6B + Qwen3-Reranker-0.6B（约 2.4GB，**无需 token**）
- **不需要任何 API key**

### ✅ 实际完成状态（2026-11-24）

**已完成**：
- 2.1 推理依赖已装：`torch 2.15.0` / `transformers 4.57.6` / `sentence-transformers 5.7.0`
  （arm64 wheel，走阿里云 pip 镜像）。
- 2.2 Qwen3-Embedding-0.6B 已接入并真跑通：模型经 `hf-mirror.com` 下载（HF 直连 502），
  编码输出 1024 维、`model_id=qwen-qwen3-embedding-0.6b`，与 namespace 标记同源。
- 2.4 蓝绿迁移做成了可执行脚本 `scripts/migrate_qwen3.py`，完整五步（建绿→双写→影子读→
  灰度放量→切流→回滚）全部真跑，控制流与真模型质量对比都验证通过。
- **关键工程修复：模型-索引联动**。原 `show_release.py` 用同一 embedder 查蓝绿两套索引，
  这在真模型场景是错的（哈希 query 向量查语义索引 = 噪音）。新增
  `factory.embedder_for_namespace()` 从 namespace 编码的 model_id 反推对应 embedder，
  `Retriever(bind_embedder_to_namespace=True)` 自动绑定。15 项测试固化此约束。

**2.3 Qwen3-Reranker-0.6B 已接入（修了一个真 bug）**：
原 `LocalReranker` 用 `AutoModelForSequenceClassification` 加载，但 Qwen3-Reranker 的
架构是 `Qwen3ForCausalLM`（生成式，无分类头）——那样加载会随机初始化分类头，打分全是噪音。
改用 `sentence_transformers.CrossEncoder`（内置 Qwen3-Reranker 专用 chat template +
yes token logit 取值）。实测相关文档 7.17 分 / 无关 -10.36 分。

**真实验收数据**（`data/eval/model_migration_report.json`）：

| 维度 | 蓝 / 对照 | 绿 / Qwen3 | 差异 |
|---|---|---|---|
| Embedding top1 命中率（4 条字面不重合 query） | Deterministic 0.500 | **0.750** | **+0.250** |
| Reranker 纠正误召回（口语化 query "买的东西不想要了能退吗"） | 确定性重排全 0 分，top1 仍是无关的价格文档 | **Qwen3 把退款文档提到 top1（4.22 分）** | 从错排到正确 |

> 诚实边界：4 题小样本教学演示，非严格 benchmark（样本小、query 是设计的）。
> 证明的是"语义模型 vs 哈希在字面不重合时的质量差距真实存在"，不能当 SOTA 分数引用。

**未完成 / 降级项（诚实标注）**：
- 全量评测仍跑 prototype 档（哈希），门禁基线未刷新——换模型的收益用独立迁移报告体现，
  不动主评测基线，避免把"教学对比"混进回归门禁。
- batch encode 优化未做：当前仍逐条 embed，Qwen3 在 CPU 上 8 条约数秒；生产应改 batch 32。
- 未接 vLLM / TEI 独立推理服务（企业档），当前是进程内加载，适合本地生产档。

---

## 波次 3 · 真 LLM 与自动评测（**需要你提供 API key**）

**为什么排第四**：生成质量和评测都依赖一个真的 LLM。到这里我需要你提供一样东西。

### 3.1 你需要提供的（唯一必需项）

- **一个 OpenAI 兼容的 API key**，推荐 DeepSeek（`https://api.deepseek.com/v1`）。填到 `.env`：
  ```
  LLM_BACKEND=api
  LLM_API_KEY=sk-你的key
  LLM_BASE_URL=https://api.deepseek.com/v1
  LLM_MODEL=deepseek-chat
  ```
- 费用提示：评测跑 100 条问题大约几毛到几块钱，很便宜。
- **替代方案**：如果你不想给 key，可以用本地 vLLM 起一个 7B 模型当 judge，但要额外下载模型、配置更复杂——不推荐，除非你的数据不能出网。

### 3.2 接入真生成 + 流式输出

- `LLM_BACKEND=api` 已有 `OpenAILikeLLM` 骨架，补 SSE 流式接口 `/ask/stream`。
- **验收**：真 LLM 答案比 MockLLM 抽取式明显流畅，引用 `[n]` 正确。

### 3.3 RAGAS 自动评测闭环（简历最大加分项）

- 装 `ragas`（及其依赖），用 DeepSeek 同时当生成模型和 judge 模型。
- 评测集：把 `data/eval_questions.json` 从 10 条扩到 50-100 条（问题 + 标准答案 + 证据）。
- 四项指标：`faithfulness`（答案忠于证据）、`answer_relevancy`、`context_precision`、`context_recall`。
- 做成回归门禁脚本 `scripts/eval_gate.py`：换 prompt 或模型后跑，指标不降才准合并。
- **验收**：产出一份评测报告（JSON + Markdown），四个分数有基线值；故意改坏 prompt 再跑，指标下降能被测出来。

### 波次 3 产出
`scripts/eval_gate.py`、`scripts/show_llm_compare.py`、评测报告、`data/eval_questions.json` 扩容。

---

## 波次 4 · 部署上线（需要云服务器，可选）

**为什么排最后**：前面全对了，部署只是搬运；前面没对，部署就是把 bug 搬到云上按小时烧钱。

### 4.1 单机 docker-compose（本地可先验证）

- 写生产版 Dockerfile（多阶段构建、非 root 用户、依赖分层缓存）。
- `docker-compose.yml` 加 api 服务，环境变量从 `.env.prod` 读，密钥走 Docker secrets 不进镜像。
- 健康检查分 `/health/live`（进程活）和 `/health/ready`（依赖可用）。
- **验收**：`docker-compose up` 后 curl `/ask` 端到端通；`docker-compose down` 数据不丢。

### 4.2 K8s 集群（这一波才需要云服务器）

- 你需要提供：一台云主机（或托管 K8s 集群），建议 4C8G 起；域名可选。
- Deployment + HPA（无状态 API）、ConfigMap/Secret、Ingress（TLS）、Milvus Operator。
- CI/CD：GitHub Actions → 单测 → 评测门禁 → 构建镜像 → 推送 → 滚动更新。
- 配置中心（Nacos/Apollo）管理 `ACTIVE_NAMESPACE`，实现秒级蓝绿切流不重启。

### 资源需求
- 波次 4.1：**不需要云资源**，本机 Docker 就能做
- 波次 4.2：云服务器 1 台（4C8G 起）+ 可选域名/TLS 证书

---

## 资源需求总表（你问的"需要我提供什么"）

| 波次 | 需要你提供 key？ | 需要下载/工具 | 本机是否够用 |
|---|---|---|---|
| 0 安全可观测 | **不需要** | 无（纯代码，最多装个 PyJWT） | ✅ 现在就能做 |
| 1 持久化 | **不需要** | 4 个 Docker 镜像 + 3 个 Python 包 | ✅ Docker v1 够用 |
| 2 真语义模型 | **不需要**（HF 权重公开） | torch/transformers + 2 个模型约 4.4GB | ✅ 24G 内存跑 0.6B 没问题 |
| 3 真 LLM + 评测 | **需要：一个 DeepSeek API key** | ragas 包 | ✅ 有 key 即可 |
| 4 部署 | 4.1 不需要 / 4.2 需要云服务器 | Docker 镜像 | 4.1 本机可做 |

**一句话总结**：前 3 波你什么都不用给我，我直接做；第 3 波需要一个 DeepSeek key；第 4 波的集群部署需要一台云服务器（单机 docker-compose 版不需要）。

---

## 面试话术：怎么讲这次改造

> **问：你的项目从原型到生产做了哪些改造？**
>
> "我按依赖顺序分了五波。第一波先修安全——把租户身份从请求体挪到 JWT，因为客户端自报租户是越权漏洞；同时补了 trace_id 全链路日志和引用校验。第二波做持久化，租户表落 PostgreSQL、原文进 MinIO、向量切 Chroma，验证了重启不丢数据。第三波换真语义模型，用 Qwen3-Embedding 和 Reranker 替换哈希档，关键是我没覆盖旧索引，而是用 namespace 做了双写、影子读、评测达标再切流的蓝绿迁移。第四波接真 LLM 并上 RAGAS 自动评测，把评测做成回归门禁。第五波才容器化和上 K8s。这个顺序的核心逻辑是：先保证对，再保证快，最后保证规模。"

> **追问：为什么先做安全而不是先换模型？**
>
> "因为模型再准，越权漏洞一出就是数据事故，性质完全不同。而且换模型要全量重建索引，如果底层存储还没持久化，重建完一重启又没了，白干。所以顺序是安全 → 持久化 → 模型，每一步都是下一步的前提。"
