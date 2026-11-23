# 从原型到生产：架构诊断、改进清单与部署方案

> 本文回答三个问题：当前架构到底处在什么水平？上生产要补什么？怎么部署？
> 所有结论基于对 `app/` 全部实现的逐行复核，不是泛泛而谈。

---

## 一、架构诊断：诚实的自我评价

### 1.1 已经具备的生产级设计（这些是简历主角）

| 设计 | 代码位置 | 为什么是生产级的 |
|---|---|---|
| 抽象契约层 | `app/interfaces.py` | 5 个 `Protocol` 用结构化子类型，第三方库（Docling/Chroma）无需继承我们的类即可接入，符合依赖倒置原则 |
| 配置驱动工厂 | `app/factory.py` | 唯一读配置处、懒加载重依赖，换实现只改环境变量，业务代码零 `if TIER ==` |
| 版本化 namespace | `factory.active_namespace()` | 索引名来自**实际 embedder 的 model_id**，与 metadata 版本标记同源，蓝绿迁移不误判 |
| 级联检索 | `retriever.py` + `reranker_local.py` | 双塔召回 + 交叉编码精排，rerank 分数独立重排，兼顾速度与精度 |
| 分层配置覆盖 | `tenancy/registry.py` | 只存差异、来源追踪，解决几万租户配置爆炸 |
| 空结果短路 | `rag_service.py` | 无证据不调 LLM，防幻觉的第一性设计 |

### 1.2 原型替身（面试必须主动说清，否则一追就穿帮）

| 组件 | 当前实现 | 生产必须替换为 |
|---|---|---|
| Embedding | `DeterministicEmbedder`（md5 哈希，无语义） | Qwen3-Embedding 本地部署或 TEI 服务 |
| Rerank | `DeterministicReranker`（2-gram 重叠） | Qwen3-Reranker / bge-reranker 交叉编码服务 |
| LLM | `MockLLM`（抽取式，会选错证据） | DeepSeek / 自建 vLLM |
| 向量库 | `InMemoryStore`（进程内存，重启丢失） | Milvus（partition_key 租户分区） |
| 租户注册表 | 内存 dict（重启丢失） | PostgreSQL + Redis 缓存 |
| 迁移脚本 | 只打印日志的骨架 | 真正的双写 + 影子流量 + 配置中心切流 |

### 1.3 一个真实安全漏洞（优先修）

**现状**：`Question.tenant_id` 在 POST body 里，客户端可任意伪造，冒充其他租户查询。
**修法**：租户身份从服务端签发的 JWT / API Key 中解析（`Depends(get_current_tenant)`），请求体里的 tenant_id 只作为管理端调试用，普通用户请求一律忽略。这是 to B 产品的红线。

---

## 二、生产级改进清单（按优先级分层）

### P0：不补不能上线

**① 身份与安全**
- JWT/API Key 鉴权，租户身份从 token 解析（见 1.3）
- per-tenant 限流（Redis 令牌桶）：防止单租户打爆 LLM 配额
- 输入校验：问题长度上限、敏感词、prompt injection 检测
- 密钥管理：LLM_API_KEY 走 Secret Manager，不进代码和 .env 明文

**② 数据持久化**
- 租户注册表落库：PostgreSQL 存 tenants / documents / ingest_tasks 三张表
- 原文存储：MinIO/S3 存原始文件，向量库存向量，关系库存元数据——各司其职
- 向量库切 Milvus：`create_collection` 带 `partition_key_field="tenant_id"` 做物理分区
- 备份与恢复：向量库快照 + 关系库定期备份

**③ 可观测性**
- 结构化日志（JSON）+ trace_id 贯穿全链路
- OpenTelemetry 链路追踪：解析→检索→重排→生成每段耗时
- 核心指标：QPS、P95 延迟、首字延迟、召回空结果率、引用校验失败率、租户配额使用率
- 健康检查分 readiness（依赖是否可用）和 liveness（进程是否活着）

### P1：上生产后影响质量

**④ 离线建库流水线**
- 异步任务队列：Celery/RQ + Redis，上传文档返回 task_id，异步处理
- 状态机：`pending → parsing → chunking → embedding → done/failed`，失败可断点重跑
- 增量更新：文档 content_hash 比对，没变跳过；变了按 doc_id 删旧 chunk 再插新
- 批Embedding：当前逐条 `embed([text])`，生产要 batch 32/64，吞吐差几十倍
- 幂等 chunk_id：`hash(doc_id + chunk_index + content_hash)`，重试不产生重复向量

**⑤ 检索质量增强**
- **混合检索**：向量 + BM25（中文用 jieba/IK 分词），RRF 融合排序。解决"型号、编号、专有名词"向量检索弱的问题——这是生产 RAG 的标配
- 查询改写：口语化追问改写成独立检索句（多轮对话必需）
- small-to-big：用小 chunk 检索保证精度，返回父 chunk 给 LLM 保证上下文完整
- 元数据过滤下推：tenant_id、时间范围、文档类型在向量库层过滤
- 结果缓存：Redis 缓存 query → top chunk ids，热点问题秒回
- rerank 服务化：交叉编码器独立部署、批量打分，与在线服务解耦

**⑥ 生成质量增强**
- 流式输出（SSE）：边生成边返回，降低首字延迟感知
- 引用校验：检查答案里的 `[n]` 是否真对应 chunk 内容，防止 LLM 编造引用
- 结构化输出：JSON schema 约束，答案、引用、置信度分离
- prompt 版本管理：prompt 当代码版本化，支持 A/B 测试
- 多轮对话：会话历史存储 + query rewrite

### P2：规模化与团队效率

**⑦ 弹性与高可用**
- 在线服务无状态化，HPA 按 QPS 扩缩容
- 模型推理服务（vLLM）独立扩缩，GPU/CPU 分离
- Milvus 集群化，collection 分片
- 熔断降级：LLM 超时熔断，降级到"只返回检索片段"或转人工

**⑧ 评测与反馈闭环**（简历最大加分项）
- 评测集：问题-标准答案-证据三元组（当前已有 `data/eval_questions.json` 10 条，需扩到 100+）
- RAGAS 指标自动评测：faithfulness（答案是否忠于证据）、answer relevancy、context precision/recall
- 回归门禁：改 prompt / 换模型必须跑评测，指标不降才准上线
- 线上反馈：点赞/点踩 + 会话日志 → 自动挖掘 bad case → 补进评测集
- 这与萝卜塔的"会话日志自动挖掘新知识点"是同一套思想

**⑨ CI/CD**
- GitHub Actions：push → 单测 → 评测 → 构建镜像 → 部署
- 配置中心（Nacos/Apollo）：ACTIVE_NAMESPACE 秒级切流不重启，支撑蓝绿迁移

---

## 三、部署方案

### 3.1 三级演进路径

```
阶段 A 本地原型：单进程 uvicorn + 内存/Chroma + mock 档（当前状态）
   ↓ 验证业务逻辑
阶段 B 单机生产：docker-compose 起全套依赖，单节点部署
   ↓ 验证正确性与性能
阶段 C 集群生产：K8s + Milvus 集群 + vLLM 推理服务 + 配置中心
```

### 3.2 阶段 B：docker-compose 单机部署

服务组成：
| 服务 | 镜像 | 作用 |
|---|---|---|
| api | 自建 Dockerfile | FastAPI 在线服务，2~4 worker |
| postgres | postgres:16 | 租户/文档/任务元数据 |
| redis | redis:7 | 任务队列 + 缓存 + 限流 |
| chroma/milvus | 对应官方镜像 | 向量库 |
| minio | minio | 原始文件对象存储 |
| embed/rerank | vLLM 或 TEI | 模型推理服务（可先合进 api） |

关键配置：
- 环境变量分 `.env.prod`，密钥用 secrets 挂载
- 日志 stdout 输出，交给 logrotate / 采集器
- 健康检查探活 `/health/ready` 和 `/health/live`
- 数据卷持久化：postgres data、milvus data、minio data

### 3.3 阶段 C：K8s 集群部署

- **无状态 API**：Deployment + HPA（按 CPU/QPS 扩容），ConfigMap 存配置、Secret 存密钥
- **模型服务**：vLLM 独立 Deployment，embedding/rerank/LLM 三个服务分开扩缩
- **向量库**：Milvus Operator 或 Helm Chart 部署集群，独立存储卷
- **异步任务**：Celery worker 独立 Deployment，队列长度驱动扩容
- **灰度发布**：namespace 版本化 + 配置中心切流，天然支持蓝绿；配合 K8s 的滚动更新做双层灰度
- **Ingress**：按租户域名/路径路由，TLS 终止

### 3.4 迁移上线流程（把蓝绿迁移真正落地）

1. 用新 embedding 建绿 collection（新 namespace），离线全量重建
2. 双写：增量文档同时写蓝绿两套
3. 影子读：线上 query 双查，只返回蓝结果，记录绿结果做质量对比
4. RAGAS 评测达标 → 配置中心原子切流到绿
5. 观察期保留蓝索引 1~2 周，异常秒级回滚

---

## 四、给简历的启示

**技术深度不是靠堆组件名，而是靠"我知道每一层在生产上会出什么问题、怎么解"。**

写"用了向量检索"是初级；写"用双塔召回+交叉编码重排级联，因为纯向量有损压缩、全库精排太慢，并用 RRF 融合 BM25 解决型号类查询"是中级；写"设计了 namespace 版本化+双写+影子读的蓝绿迁移，让换 embedding 模型不停机可回滚"是高级。

本项目已经在"高级"这条路上走了一半（抽象层与迁移设计），补齐 P0/P1 后，就可以理直气壮地写成生产级项目。详见 `RESUME.md` 的深度版描述。
