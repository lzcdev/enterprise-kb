# enterprise-kb

面向企业的多租户知识库 RAG 问答系统 —— 从 0 构建、可平滑升级的工程实践项目。

> 学习目标：掌握 Agent/RAG 核心原理，产出一个可写进简历的 to B 工程项目。
> 参考产品形态：光云科技快麦小智萝卜塔 AI 智能体（2025 年推出）。

## 简历与面试材料

- **[docs/RESUME.md](docs/RESUME.md)** — 深度版简历项目描述：按设计决策组织，含逐条讲解要点与诚实边界
- **[docs/INTERVIEW.md](docs/INTERVIEW.md)** — 6 大主题面试题库，每题带追问链与第一人称口语化答案
- **[docs/PRODUCTION.md](docs/PRODUCTION.md)** — 架构体检、生产级改进清单（P0/P1/P2）、docker-compose 与 K8s 部署方案、蓝绿迁移落地流程
- **[docs/ROLLOUT.md](docs/ROLLOUT.md)** — 生产级改造执行手册：五波次串行步骤、每步验收命令、资源需求总表（哪些要 key/工具）
- **[docs/DEPLOY_GUIDE.md](docs/DEPLOY_GUIDE.md)** — 部署入门：五种部署方式对比、访问链路、PaaS 与云主机+Docker 两条可照抄的上线路线
- **[docs/EVAL.md](docs/EVAL.md)** — 评测集与回归门禁：AI 系统怎么验证效果、怎么做到不越改越差（含面试话术）
- **[docs/RELEASE.md](docs/RELEASE.md)** — CI/CD 与灰度蓝绿：流水线卡点、五步切流、护栏、workflow 配置与诚实边界

## 核心设计：可平滑升级的三层架构

```
业务代码（FastAPI）  →  抽象契约层（Protocol）  →  可替换实现层（按配置切换）
```

- **抽象隔离**：`app/interfaces.py` 定义 5 个 Protocol（Parser / Embedder / VectorStore / Reranker / LLM），业务代码只依赖接口，不依赖具体实现。
- **配置驱动**：`app/factory.py` 是唯一读配置、实例化实现的地方；换档位只改环境变量，业务代码零改动。
- **版本化迁移**：namespace 编码模型版本；换 embedding 走蓝绿迁移（建新索引→双写→影子验证→原子切流→保留回滚窗口）。

## 技术栈（同一套代码，切换配置）

| 档位 | 解析 | Embedding | 向量库 | 重排 | 状态 |
|---|---|---|---|---|---|
| prototype | PyMuPDF | 确定性哈希（无语义） | 内存字典 | 关键词启发式 | 默认档，零依赖跑通全链路 |
| local-prod | Docling | Qwen3-Embedding-0.6B | Chroma | Qwen3-Reranker | 接口就绪，需装 torch |
| enterprise | Docling Serve | Qwen3-8B + vLLM | Milvus | vLLM Rerank | 骨架，留生产接入点 |

> 原型档的哈希/启发式**只验证控制流、无语义质量**；真语义需切到 local-prod 并安装重依赖。这是刻意的教学设计，不是能力缺失——详见 RESUME.md 的"诚实边界"。

## 完成进度（7/7）

- [x] 任务 1 项目骨架：配置（pydantic-settings）、数据契约（pydantic）、抽象接口（Protocol）、组件工厂
- [x] 任务 2 文档解析与切分：PyMuPDF/Docling 双解析器、递归字符切分 + 标题上下文、多格式测试数据
- [x] 任务 3 Embedding 与向量存储：三档 Embedder/VectorStore、namespace 版本化、chunk→向量可视化（PCA 散点）
- [x] 任务 4 检索与重排：双塔召回 + 交叉编码重排级联、相似度阈值防幻觉、租户过滤
- [x] 任务 5 LLM 生成与 FastAPI：三段式 prompt、`[n]` 引用溯源、RAGService 编排、`/health` + `/ask` 接口
- [x] 任务 6 多租户体系：四层配置覆盖链、身份闸门、越权状态码映射、租户管理接口
- [x] 任务 7 简历与面试材料：RESUME.md + INTERVIEW.md

## 快速开始

```bash
# ★ 一键本地验证：7 层全链路（配置/解析/检索/生成/API/评测门禁/测试）
.venv/bin/python scripts/verify_local.py

# 全量单元测试（75 项）
.venv/bin/python -m pytest -q

# 蓝绿发布五步演示（影子读/灰度分流/切流/回滚）
.venv/bin/python scripts/show_release.py

# 效果评测 + 回归门禁（退化超容忍度则 exit 1，CI 可直接用）
.venv/bin/python scripts/run_eval.py --verbose

# 端到端问答（原型档，零依赖）
.venv/bin/python scripts/show_rag.py "七天无理由退款怎么申请？"

# 多租户三大能力演示（隔离 / 配置覆盖 / 越权拦截）
.venv/bin/python scripts/show_tenancy.py

# 启动 HTTP 服务并调试 Swagger 文档
.venv/bin/python -m uvicorn app.api.main:app --reload
# 浏览器打开 http://127.0.0.1:8000/docs
```

## 本地工程化（持续迭代的基础）

- **诚实默认**：`config.py` 默认 prototype 档（pymupdf/哈希/内存/mock），名义档位与实际后端一致，开箱可跑；`validate_runtime()` 启动时自检档位一致性与依赖齐全。
- **部署物料**：`Dockerfile`（API 容器，非 root + 健康检查）+ `deploy/docker-compose.yml`（postgres/redis/chroma/minio/api）+ `deploy/schema.sql`（租户表，PG 首次启动自动建表）。
- **评测门禁**：`data/eval/golden.json` 12 条标注集 + `app/eval/` 确定性指标与回归门禁。见 [docs/EVAL.md](docs/EVAL.md)。
- **CI/CD**：`.github/workflows/ci.yml` 四道卡点（编译/测试/全链路验证/评测门禁），PR 合并前自动拦退化。
- **灰度蓝绿**：`app/release/` 状态机 + `scripts/release_cli.py`，确定性租户分流、步长护栏、原子切流、秒级回滚；CD workflow 手动触发。见 [docs/RELEASE.md](docs/RELEASE.md)。

## 演示脚本一览

| 脚本 | 看什么 |
|---|---|
| `show_chunks.py` | 文档 → ParsedSection → chunk 全过程，可调 size/overlap |
| `show_video.py` | 视频切分：固定窗口 vs 场景边界，可对比 |
| `show_embedding.py` | chunk → 向量 → 入库 → 检索全链路 |
| `visualize_vectors.py` | 生成自包含 HTML：向量 PCA 散点 + 逐维热力图 + 检索排行 |
| `show_retrieval.py` | 粗召回 vs 精排的顺序变化 |
| `show_rag.py` | 端到端：问题 → 带引用答案 → 溯源 |
| `show_tenancy.py` | 数据隔离 / 配置覆盖 / 越权拦截 |
| `show_hybrid.py` | 纯向量 vs BM25+RRF 混合检索 |
| `show_release.py` | 蓝绿五步：影子读 / 灰度分流 / 切流 / 回滚 |
| `release_cli.py` | 发布控制命令行（workflow 与手动共用） |
| `verify_local.py` / `run_eval.py` | 一键验证 / 评测门禁（CI 卡点） |
| `migrate_embedding.py` | Embedding 蓝绿迁移骨架（历史教学版） |

## 目录结构

```
app/
  config.py           全局配置（pydantic-settings，配置外置）
  models.py           数据契约（DocumentMeta/Chunk/Source/Question/Answer）
  interfaces.py       抽象契约层（5 个 Protocol）
  factory.py          组件工厂（配置驱动 + 蓝绿切流锚点）
  rag_service.py      RAG 全链路编排（检索→重排→生成，空结果短路）
  ingestion/          文档与视频解析切分（loader + splitter + video_segmenter）
  retrieval/          向量化、存储、检索、重排
  generation/         prompt 构造、引用解析、LLM 封装
  tenancy/            租户注册表、配置覆盖链、越权校验、PG store
  security/           JWT 鉴权、输入校验（波次0）
  observability/      trace_id 全链路日志（波次0）
  eval/               评测框架 + 回归门禁
  release/            蓝绿状态机 + 灰度分流 + 回滚（可换配置中心）
  api/                FastAPI 服务（/health /ask /tenants /auth）
scripts/              演示脚本 + verify_local / run_eval / release_cli
docs/                 简历 + 面试 + 生产改造 + 部署 + 评测 + 发布
.github/workflows/    CI 质量门 + CD 部署与灰度发布
deploy/               docker-compose + schema.sql
data/eval/            评测集 golden.json + 基线 baseline.json + 报告
tests/                11 个测试文件，75 项测试
```

## 架构亮点（面试可讲）

1. **可拔插**：5 层 Protocol + 配置工厂，换模型/存储只改配置，业务代码零分支。
2. **级联检索**：双塔粗召回 + 交叉编码精排，兼顾速度与精度；rerank 分数独立重新排序。
3. **防幻觉**：相似度阈值 + 空结果短路 + prompt 约束 + `[n]` 引用溯源四重保障。
4. **多租户**：四层配置覆盖链（只存差异）+ 身份闸门 + 数据隔离，支持几万租户。
5. **平滑升级**：namespace 编码模型版本 + metadata 版本标记，蓝绿迁移可回滚。
6. **多模态**：可拔插视频切分策略，时间戳贯穿契约，支持跳转播放溯源。
