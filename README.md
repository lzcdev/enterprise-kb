# enterprise-kb

面向企业的多租户知识库 RAG 问答系统 —— 从 0 构建、可平滑升级的工程实践项目。

> 学习目标：掌握 Agent/RAG 核心原理，产出一个可写进简历的 to B 工程项目。
> 参考产品形态：光云科技快麦小智萝卜塔 AI 智能体（2025 年推出）。

## 核心设计：可平滑升级的三层架构

```
业务代码（FastAPI）  →  抽象契约层（Protocol）  →  可替换实现层（按 TIER 配置切换）
```

- **抽象隔离**：`app/interfaces.py` 定义 5 个 Protocol（Parser / Embedder / VectorStore / Reranker / LLM），业务代码只依赖接口，不依赖具体实现。
- **配置驱动**：`app/factory.py` 是唯一读配置、实例化实现的地方；换档位只改 `TIER` 环境变量，业务代码零改动。
- **版本化迁移**：namespace 编码模型版本；换 embedding 模型走蓝绿迁移（建新索引→双写→影子验证→原子切流→保留回滚窗口）。

## 技术栈（三档，同一套代码切换）

| 档位 | 解析 | Embedding | 向量库 | 用途 |
|---|---|---|---|---|
| prototype | PyMuPDF | bge-small | Chroma | 本地快速原型 |
| local-prod | Docling | Qwen3-0.6B | Chroma | 本地生产验证 |
| enterprise | Docling Serve | Qwen3-8B + vLLM | Milvus | 企业生产 |

## 当前进度

- [x] 项目骨架：配置（pydantic-settings）、数据契约（pydantic）、抽象接口（Protocol）、组件工厂
- [x] 任务 2 文档解析与切分：PyMuPDF/Docling 双解析器、递归字符切分 + 标题上下文、单元测试
- [ ] 任务 3 Embedding 与向量存储
- [ ] 任务 4 检索与重排
- [ ] 任务 5 LLM 生成与 FastAPI 服务
- [ ] 任务 6 多租户隔离与鉴权
- [ ] 任务 7 简历项目描述与系统设计题

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 跑测试
python3 -m pytest tests/ -v

# 看切分效果（把文档解析成带标题上下文的 chunk）
PARSER_BACKEND=pymupdf python3 -c "
from app.factory import get_parser
from app.ingestion.splitter import split_document
chunks = split_document('data/测试产品手册.md', get_parser())
for c in chunks[:2]:
    print(c.meta.title_path, '|', c.text[:50])
"

# 注意：默认 TIER=local-prod 用 Docling（需 pip install docling==2.37.0）
# 未安装时可用 PARSER_BACKEND=pymupdf 走快速通道
```

## 目录结构

```
app/
  config.py         全局配置（pydantic-settings，配置外置）
  models.py         数据契约（DocumentMeta/Chunk/Source/Question/Answer）
  interfaces.py     抽象契约层（5 个 Protocol）
  factory.py        组件工厂（配置驱动 + 蓝绿切流锚点）
  ingestion/        文档解析与切分（loader + splitter）  ← 当前进度
  retrieval/        向量化、存储、检索、重排  ← 待建
  generation/       LLM 生成  ← 待建
  api/              FastAPI 服务  ← 待建
scripts/
  migrate_embedding.py   Embedding 模型蓝绿迁移骨架
data/                 测试文档与运行时数据
tests/                单元测试
```
