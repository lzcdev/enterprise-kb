# CI/CD 与灰度蓝绿发布

> 本文回答三个问题：CI/CD 流水线怎么跑、蓝绿发布怎么操作、哪些是真实现哪些是骨架。

## 一、整体链路

```
提交代码 → CI 质量门 → 构建镜像 → 部署 staging → 灰度放量 → 全量切流
              ↑                                        ↓
         测试/验证/评测                            异常秒级回滚
```

**为什么这个顺序**：先保证"对"（质量门），再保证"可回溯"（回滚能力），最后才放大流量。
把没验证过的版本推上生产，等于把 bug 按流量比例放大。

## 二、CI 质量门（`.github/workflows/ci.yml`）

push 到 main 或对 main 的 PR 自动触发，四个卡点：

| 步骤 | 命令 | 拦什么 |
|---|---|---|
| 语法编译 | `python -m compileall -q app scripts` | 明显语法错误 |
| 单元测试 | `python -m pytest -q` | 75 项控制流/鉴权/租户/发布测试 |
| 全链路验证 | `python scripts/verify_local.py` | 配置一致性、链路断裂、API 不可用 |
| 评测门禁 | `python scripts/run_eval.py` | 检索/生成质量相对基线退化 |

**关键设计**：前一轮把这三个脚本都设计成"退出码 + JSON 输出"，所以接 CI 只需写一个 workflow，
业务代码零改动。这就是当初"CI-friendly 设计"兑现的价值。

依赖用 `requirements-ci.txt`（轻量），不装 docling/torch/chromadb——
那些包在 prototype 档根本用不上，装了只会让 CI 从 1 分钟变成 10 分钟。

本地模拟 CI（推代码前先跑一遍，避免等 GitHub 结果）：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_local.py
.venv/bin/python scripts/run_eval.py
```

## 三、CD 部署与发布（`.github/workflows/cd.yml`）

用 `workflow_dispatch` 手动触发，不自动上生产。原因：to B 产品客户现场复杂，
需要人工卡点确认——这是"持续交付"和"持续部署"的区别。

可选动作：`deploy-staging` / `deploy-production` / `canary` / `cutover` / `rollback` / `status`。

**前置配置**（仓库 Settings → Secrets and variables → Actions）：

| 类型 | 名称 | 用途 |
|---|---|---|
| Secret | `SSH_HOST` / `SSH_USER` / `SSH_KEY` | 目标服务器登录 |
| Secret | `GHCR_TOKEN` | 服务器拉取私有镜像 |
| Variable | `APP_DIR` | 服务器上项目目录，如 `/opt/enterprise-kb` |

没有这些配置时 workflow 会明确失败——**这是预期的**：生产部署必须有真实基础设施，
不能假装本地跑通就等于能上线。

### 3.1 网页端配置步骤（照着点）

1. **打开设置**：仓库页面 → Settings → Secrets and variables → Actions。
2. **加 Secrets**（New repository secret，逐个添加）：
   - `SSH_HOST`：服务器公网 IP。
   - `SSH_USER`：SSH 登录用户。
   - `SSH_KEY`：私钥全文（不是 .pub），开头 `-----BEGIN OPENSSH PRIVATE KEY-----`。
   - `GHCR_TOKEN`：有 `read:packages` 的 GitHub PAT（私有包时必填；公开包可省略）。
3. **加 Variables**（Variables 标签 → New repository variable）：
   - `APP_DIR`：服务器上项目目录，如 `/opt/enterprise-kb`，该目录下需有 `deploy/docker-compose.yml` 和 `.env`。
4. **打开镜像写权限**：Settings → Actions → General → Workflow permissions，选
   **Read and write permissions**——build job 依赖 `packages: write` 推镜像到 GHCR。
5. **配分支保护**（让 CI 真正成为卡点）：Settings → Branches → Add branch ruleset，
   目标 `main`，勾选 "Require status checks to pass"，选中 `quality-gate`，
   并勾选 "Require branches to be up to date"。这样评测退化时 PR 合不进来。
6. **验证触发**：`git push` 后到 Actions 标签看 CI 是否绿；CD 到 Actions →
   CD 部署与灰度发布 → Run workflow，先选 `status` 冒烟（只读、安全）。

### 3.2 两个已修的部署坑（面试可讲）

| 坑 | 现象 | 修法 |
|---|---|---|
| 推了镜像但不会被用 | CD 把 `:${sha}` 镜像推到 GHCR，compose 的 api 服务只有 `build` 没有 `image`，`docker compose pull api` 拉不到 | compose 增加 `image: ${IMAGE_NAME}:${IMAGE_TAG}`，CD 导出 `IMAGE_TAG=${sha}` 并 `up -d --no-build`，强制用拉下来的镜像 |
| 灰度状态容器重建即丢 | 切流比例写在 `/app/data/release/state.json`，容器重建后回到初始，灰度白做 | compose 挂 `release_data:/app/data/release` 卷，状态跨容器生命周期持久化 |

**为什么值得讲**：这两个都不是"代码逻辑错"，而是**交付链路的接缝错**——单测和本地验证都发现不了，
只有真实跑过部署才会暴露。to B 落地时这类问题比算法 bug 更常见。

## 四、蓝绿发布五步（本地可演示）

```bash
.venv/bin/python scripts/show_release.py     # 完整演示
```

对应到 `app/release/controller.py`：

1. **init(blue_ns)**：记录当前线上索引，幂等（已有状态不覆盖）。
2. **prepare(green_ns)**：登记绿索引，此时线上仍 100% 走蓝，零影响。
3. **shadow_compare()**：同批 query 查两套索引，算 Top-K 重合率。只观察不返回。
4. **set_canary(pct)**：按比例分流。**同一租户稳定命中同一版本**（md5 分桶，
   不用 Python 内置 hash——它带进程随机盐，重启后用户会"跳版本"）。
5. **cut_over()**：active 指针改成 green，`factory.active_namespace()` 立刻返回新 namespace，
   **新请求无需重启就走绿索引**。旧索引保留观察窗口。
6. **rollback()**：指针改回 blue，秒级恢复，绿索引不删（留着排障）。

命令行逐操作：

```bash
.venv/bin/python scripts/release_cli.py status
.venv/bin/python scripts/release_cli.py init --blue kb__old__v1
.venv/bin/python scripts/release_cli.py prepare --green kb__new__v2
.venv/bin/python scripts/release_cli.py shadow --queries "退款,保修"
.venv/bin/python scripts/release_cli.py canary --percent 10
.venv/bin/python scripts/release_cli.py canary --percent 50
.venv/bin/python scripts/release_cli.py canary --percent 100
.venv/bin/python scripts/release_cli.py cutover
.venv/bin/python scripts/release_cli.py rollback
```

## 五、护栏（把流程编码进代码，不靠人记得）

| 护栏 | 触发条件 | 意义 |
|---|---|---|
| namespace 存在性 | `prepare()` 时索引不存在或为空 | 防止切到空索引，造成"切流成功但检索全空" |
| 单次步长 ≤ 50% | `0% → 100%` 被拒，除非 `force=True` | 强制分步观察，缩小爆炸半径 |
| 切流前须满灰度 | `canary<100` 时 `cut_over()` 被拒 | 防止跳过影子验证阶段 |
| 切流中禁改绿索引 | `active==green` 时 `prepare()` 被拒 | 防止运行中换索引造成状态错乱 |
| 损坏配置回退 | JSON 文件坏掉时 `status()` 返回 None | 配置中心故障时服务仍可用（可用性优先） |

所有动作写入 `state.history` 审计日志——出事能复盘谁在什么时候切了什么。

## 六、诚实边界（面试必须说清）

| 能力 | 当前状态 |
|---|---|
| 状态机 / 护栏 / 确定性分流 / 审计 | ✅ 真实现，有 13 项测试 |
| 切流生效 | ✅ 真生效：`factory.active_namespace()` 读发布状态 |
| 配置中心 | ⚠️ 本地用 JSON 文件原子写入模拟；生产换 Nacos/Apollo 只换 `ReleaseStateStore` 实现 |
| 影子读质量判断 | ⚠️ 当前原型档只有一个 embedding，蓝绿同源，重合率必然 1.0——**只验证控制流，不能证明质量提升**。切 Qwen3 后才有真实对比 |
| CD 工作流 | ⚠️ 已写好但未实跑（需要服务器和 secrets），这是生产接入点，不是"已部署" |
| 指标自动判定 | ❌ 目前靠人看错误率/P95 决定是否继续放量，未接 Prometheus 自动门禁 |

## 七、面试话术（第一人称）

> **问：你怎么做到持续部署又能频繁迭代？**
>
> "核心是把每次变更的爆炸半径压到最小。流程上：提交触发 CI，跑单测、全链路验证和评测回归门禁，
> 退化超容忍度就阻断合并；过了打包镜像部署 staging，再灰度放量 10%→50%→100%，每步观察，
> 异常秒级回滚——没有回滚能力的部署等于赌博。我们项目里换 embedding 就是这套：namespace 编码模型版本，
> 建绿索引全量重建、双写、影子读对比、原子切流、保留回滚窗口。"
>
> **追问：灰度分流怎么保证同一个用户不会一会儿新版本一会儿旧版本？**
>
> "用租户 ID 做 md5 分桶，稳定命中。不能用 Python 内置 hash，它对字符串有随机盐，
> 进程重启后结果会变——用户会遇到跳版本。另外护栏写进代码：不允许从 0 直接跳 100%，
> 单次最多 50 个百分点，强制分步观察。"
>
> **追问：CI 里怎么测 AI 系统的效果？**
>
> "两条轨道。传统测试用 assert 测控制流；AI 效果用评测集加指标，关键是和基线对比而不是看绝对分数。
> 我把评测脚本设计成退出码 + JSON 输出，退化超容忍度直接让 CI 失败。不能自动刷基线，
> 否则每次退化就调低门槛等于没有门禁。"
