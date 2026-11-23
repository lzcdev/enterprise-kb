# 数据存在哪里 & 如何用可视化客户端查看

本文回答两个问题：**项目的数据现在落在哪**、**怎么用一个 GUI 客户端直接看到它们**。
你机器上已装 DBeaver 26.2.3（能同时连 PostgreSQL 和 SQLite），下面的步骤照着点即可。

---

## 一、数据全景：四类数据，四个去处

先理解一个关键前提：**默认档（prototype/memory）的数据大多只在内存里**，进程结束就没了，
GUI 无从看起。只有显式切到持久化后端、或用播种脚本写入的数据才会落盘。

| 数据种类 | 默认档（prototype） | 持久化后落在哪 | 能否 GUI 看 |
|---|---|---|---|
| **租户配置**（四层覆盖链） | 内存 dict | PostgreSQL `enterprise_kb_test.tenants` 表 | ✅ DBeaver 连 PG |
| **文档 chunk + 向量** | 内存 list | Chroma：`data/chroma/chroma.sqlite3` + `*.bin` | ⚠️ 文本能看，向量本体是二进制 |
| **原始文档** | 磁盘文件 | `data/*.md/.txt/.docx/.pdf` | ✅ 直接打开文件 |
| **评测/发布状态** | 磁盘 JSON | `data/eval/*.json`、`data/release/state.json` | ✅ 文本编辑器 |

还有一个**不在库里**但你天天在用的：HuggingFace 下载的 Qwen3 模型权重，
在 `~/.cache/huggingface/hub/models--Qwen--*/`（各 1.1GB），不进项目仓库。

### 当前实际状态（已用播种脚本灌入）
- PG `enterprise_kb_test.tenants`：4 行（default / law_firm / startup / persist_probe）
- Chroma `data/chroma`：26 条真实 chunk 向量，collection=`kb__deterministic-hash-v1__v1`，128 维
- 这两份数据由 `scripts/seed_visual_data.py` 生成，幂等可重跑

> 想自己重建：`cd 项目根目录 && .venv/bin/python scripts/seed_visual_data.py`

---

## 二、用 DBeaver 看 PostgreSQL（租户配置）

### 连接步骤
1. 打开 DBeaver → `Database` → `New Database Connection`
2. 选 **PostgreSQL** → Next
3. 填写：
   - Server Host: `127.0.0.1`
   - Port: `5432`
   - Database: `enterprise_kb_test`
   - User: `lzc`
   - Password: 留空（本机 brew PG 是 trust 认证，不需要密码）
4. 点 `Test Connection`，第一次会提示下载 PostgreSQL 驱动，点确认下载；成功后 `Finish`

### 看什么
展开 `enterprise_kb_test` → `public` → `tables` → `tenants`，双击打开数据：

| 列 | 含义 |
|---|---|
| `tenant_id` | 租户唯一 ID（主键） |
| `allowed_source_types` | **JSONB** 列：允许的文档类型白名单 |
| `overrides` | **JSONB**：租户级参数覆盖（如 `chunk_size=300`） |
| `type_overrides` | **JSONB**：按文档类型的覆盖（如 PDF 用 400） |
| `doc_overrides` | **JSONB**：按具体文档的覆盖 |

**值得注意**：DBeaver 里 JSONB 列直接显示成 JSON 文本。对比 `law_firm` 这行——
它的 `overrides`、`type_overrides` 展示了项目"四层配置覆盖链"里租户层和类型层的真实数据。
这就是面试讲多租户配置隔离时能截图的东西。

可以跑 SQL 看覆盖链：
```sql
SELECT tenant_id, name,
       overrides->>'chunk_size' AS tenant_chunk_size,
       type_overrides->'pdf'->>'chunk_size' AS pdf_chunk_size
FROM tenants ORDER BY tenant_id;
```

---

## 三、用 DBeaver 看 Chroma（chunk + 文本 + metadata）

Chroma 的持久化**底层是 SQLite**，所以 DBeaver 用 SQLite 连接即可。

### 连接步骤
1. `New Database Connection` → 选 **SQLite** → Next
2. Database path 填（绝对路径）：
   `/Users/lzc/Desktop/github/enterprise-kb/data/chroma/chroma.sqlite3`
3. `Test Connection` → `Finish`

### ⚠️ 最容易踩的坑：查询跑错连接（`relation "embeddings" does not exist`）

**现象**：在 SQL 编辑器里跑了下面这段 join，报
`SQL Error [42P01]: ERROR: relation "embeddings" does not exist`。

**根因**：当前 SQL 编辑器连的是 **PostgreSQL**（`enterprise_kb_test`），
但 `embeddings` / `embedding_metadata` / `embedding_fulltext_search_content`
这三张表在 **Chroma 的 SQLite 文件**里，PG 里根本没有。这是"表里没有这张表"，不是语法错。

**怎么确认当前编辑器连的是哪个库**：
- 看编辑器标签页标题：`enterprise_kb_test > ...sql` 表示连的是 PG；
  `chroma.sqlite3 > ...sql` 才是连的 Chroma SQLite。
- 看编辑器工具栏右上角的连接下拉框（显示当前连接名）。

**正确做法**：
1. 按上面的步骤新建一个 **SQLite** 连接指向 `chroma.sqlite3`
2. 在左侧连接树里**右键这个 SQLite 连接 → `SQL Editor` → 新建 SQL 编辑器**
3. 把 join 查询粘到**这个**编辑器里执行
4. PG 的租户查询和 Chroma 的 chunk 查询要分别在各自连接的编辑器里跑，
   不要放在同一个编辑器里——它们是两个完全独立的数据库

也可以不写 SQL：展开 SQLite 连接 → `tables`，双击 `collections` /
`embedding_metadata` / `embedding_fulltext_search_content`，直接看原始数据。

### ⚠️ 先看懂一个边界（很重要）
Chroma 1.5.x 把数据拆成两处存放：
- **SQLite**（`chroma.sqlite3`）：collection 定义、chunk ID、**文本全文**、metadata 键值对
- **二进制文件**（`data_level0.bin` 等）：**向量本体**（float32），SQLite 里没有

所以 DBeaver **能看到文本和 metadata，但看不到向量数值**——向量在 `.bin` 里，
需要用 Chroma 的 Python API 或项目的可视化脚本读。这不是 DBeaver 的问题，是 Chroma 的存储设计。

### 看什么（三张关键表）
1. **`collections`**：一个 namespace 一行，这里应看到 `kb__deterministic-hash-v1__v1`
2. **`embedding_metadata`**：每个 chunk 的 metadata 键值对（长表结构：`key` / `string_value`）
3. **`embedding_fulltext_search_content`**：chunk 全文（`c0` 列）

DBeaver 里跑这段 SQL，把三张表拼成"人能读"的视图（已实测可用）：
```sql
SELECT
  e.embedding_id AS chunk_id,
  substr(fc.c0, 1, 60) AS text_preview,
  max(CASE WHEN m.key='filename' THEN m.string_value END) AS filename,
  max(CASE WHEN m.key='embedding_model' THEN m.string_value END) AS model,
  max(CASE WHEN m.key='page' THEN m.int_value END) AS page
FROM embeddings e
JOIN embedding_fulltext_search_content fc ON fc.id = e.id
LEFT JOIN embedding_metadata m ON m.id = e.id
GROUP BY e.id;
```

你能看到 26 条 chunk 的：来源文件（如 `产品规格.md`）、模型标记、页码、正文预览。
**`embedding_model` 这一列就是蓝绿迁移的锚点**——未来建了 Qwen3 绿索引，这里会出现
第二个 collection、metadata 变成 `qwen-qwen3-embedding-0.6b`，一眼可辨。

---

## 四、想看"向量本身"怎么办？（DBeaver 看不到的部分）

DBeaver 看不了向量数值和相似度关系。项目里有两个专门工具：

### 1. 向量的 PCA 散点图（看聚类关系）
```bash
.venv/bin/python scripts/visualize_vectors.py
```
产出 `data/viz/vectors.html`，浏览器双击打开，能把高维向量降到 2D 看语义聚类。

### 2. chunk → 向量全过程拆解
同一份 HTML 里还展示了文本怎么一步步变成向量（n-gram → 哈希桶 → 归一化），
这是 GUI 客户端做不到的教学可视化。

### 3. 用 Python API 直接查向量
```python
from app.retrieval.vectorstore_chroma import ChromaStore
store = ChromaStore(persist_dir="data/chroma")
print(store.list_namespaces())              # 所有 collection
print(store.count("kb__deterministic-hash-v1__v1"))  # 26
# 拿原始向量（DBeaver 看不到的部分）
import chromadb
c = chromadb.PersistentClient(path="data/chroma")
col = c.get_collection("kb__deterministic-hash-v1__v1")
raw = col.get(include=["embeddings"], limit=1)
print(len(raw["embeddings"][0]))            # 128 维
```

---

## 五、数据流向小结（面试话术）

> "我们的数据分四类落在不同地方：原始文件在磁盘、评测和发布状态是 JSON、
> 租户配置落 PostgreSQL 的 JSONB 列、chunk 和向量落 Chroma。
> Chroma 有个容易踩的点——它把文本和 metadata 存 SQLite、向量本体存二进制文件，
> 所以用 DBeaver 连 SQLite 只能看到文本和元数据，向量得用 API 读。
> 我还写了个幂等的播种脚本，把真实文档灌进两个持久化库，
> 这样打开 GUI 看到的是有血有肉的多租户覆盖链和 26 条真实 chunk，而不是空表。"

---

## 六、常见疑问
- **为什么 PG 里还有个 `persist_probe` 租户？** 那是波次1 双进程持久化验证写的探针，
  用来证明"进程重启后配置还在"。可以删：`DELETE FROM tenants WHERE tenant_id='persist_probe';`
- **`data/eval/last_report.json` 每次跑都变，要不要入库？** 不入库（已在 .gitignore），
  它是运行产物；`baseline.json`（基线）和 `golden.json`（标注集）才版本化。
- **能不能让默认档就落盘？** 可以，改环境变量
  `VECTORSTORE_BACKEND=chroma` + `TENANT_STORE_BACKEND=pg`，
  但 CI 用轻量依赖不装 chromadb/psycopg2，所以默认保持 memory 让 CI 能跑。
