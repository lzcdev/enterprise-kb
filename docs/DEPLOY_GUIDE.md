# 部署入门：从零到能访问

> 面向"没部署过、不想部署在本机"的读者。先建立心智模型，再给两条可照抄的路线。
> 目标不是让你成为运维，而是让你**看懂全局、亲手跑通一次、面试能讲清流程**。

---

## 一、先建立心智模型：部署到底在干什么

把你电脑上跑通的代码，搬到一台**一直开机、有公网 IP、别人能访问**的机器上跑。

所有部署方式都在回答三个问题：

| 问题 | 不同答案 → 不同方式 |
|---|---|
| 代码放哪台机器？ | 你的电脑 / 平台托管 / 租的云主机 / 一堆机器(K8s) |
| 别人怎么找到它？ | 临时网址(ngrok) / 平台给的网址 / 自己买域名+DNS |
| 依赖怎么办？ | 手动装 / 平台自动装 / 容器打包 |

理解了这三问，部署的"各种名词"就都有位置了。

## 二、五种方式对比（从易到难）

| 方式 | 本质 | 成本 | 适合 | 你现在该做吗 |
|---|---|---|---|---|
| **内网穿透** | 本机跑，开公网隧道 | 免费~几十/月 | 临时演示 | ⚠️ 只演示，电脑关就断 |
| **PaaS（Render/Railway）** | 连 GitHub，平台自动构建托管 | 免费额度~几十/月 | 新手练手 | ✅ **先走这条看到效果** |
| **云主机 ECS** | 租远程 Linux，自己配环境 | 30~100元/月 | 理解生产 | ✅ **再走这条懂全貌** |
| **Docker 容器** | 环境+代码打包成镜像 | 云主机费+免费工具 | 单机生产标准 | ✅ 配合 ECS 一起 |
| **K8s 集群** | 多机编排、自动扩缩 | 数百元/月起 | 大规模 | ❌ 了解概念即可 |

## 三、访问链路：一次请求经过什么

```
浏览器 → DNS解析(域名转IP) → HTTPS加密 → 云主机安全组(放行端口)
       → Nginx反向代理(SSL终结/转发) → FastAPI应用 → 向量库/PostgreSQL/LLM
```

术语速查：
- **域名 + DNS**：`ask.example.com` → 服务器 IP，买域名后加 A 记录。
- **HTTPS 证书**：Let's Encrypt 免费，Nginx 或平台自动配。
- **安全组**：云厂商虚拟防火墙，默认全关，必须手动放行 80/443。新手最常卡这里。
- **反向代理 Nginx**：应用前面的"前台"，处理证书、转发请求，应用不直接暴露公网。
- **容器**：代码+环境打包的标准箱子，消除"我这能跑你那跑不了"。

---

## 四、路线 A：PaaS 平台（推荐先做，最省心）

以 **Render** 为例（Railway/Fly.io 类似），全程不用碰服务器命令行。

### 前提
1. 代码已推到 GitHub（本项目 remote 已有，需要先 push 当前分支）。
2. 注册 Render 账号（可用 GitHub 登录）。

### 步骤
1. **准备启动命令**：Render 用 `uvicorn`，在根目录建 `Procfile`：
   ```
   web: uvicorn app.api.main:app --host 0.0.0.0 --port $PORT
   ```
   注意 `--host 0.0.0.0`（不能是 127.0.0.1，否则外部访问不到）和 `$PORT`（平台动态分配）。

2. **锁定依赖**：确保 `requirements.txt` 完整（本项目已有）。

3. **创建服务**：Render 控制台 → New → Web Service → 连接 GitHub 仓库 →
   - Runtime: Python 3
   - Build Command: `pip install -r requirements.txt`
   - Start Command: 上面的 uvicorn 命令
   - 点 Create Web Service

4. **配环境变量**：在 Environment 里加：
   - `AUTH_ENABLED=true`
   - `JWT_SECRET=<随机长字符串>`
   - `DEMO_TOKEN_ISSUER=false`（生产关掉 demo 签发！）
   - `LLM_BACKEND=api` + `LLM_API_KEY=<你的key>`

5. **拿网址**：部署成功后平台给 `https://xxx.onrender.com`，直接访问 `/docs` 验证。

### 坑
- **冷启动**：免费档久不访问会休眠，首次请求等 15~30 秒。
- **大模型文件**：如果要跑本地 Qwen3（几 GB），PaaS 传模型很麻烦——这条路线适合先用 mock/deterministic 档或纯 API 档验证。
- **数据不持久**：免费档重启后文件丢失，正式数据要接外部数据库（波次 1 的 PostgreSQL）。

---

## 五、路线 B：云主机 + Docker（理解生产全貌）

以**阿里云/腾讯云 ECS** 为例，租一台最便宜的（2核4G，新用户约 100 元/年）。

### 步骤 1：买服务器并连上
- 买的时候选 Ubuntu 22.04，设置 root 密码。
- 在"安全组"放行 22(SSH)、80、443 端口。
- 本机终端 SSH 登录：`ssh root@你的公网IP`

### 步骤 2：装 Docker
```bash
# Ubuntu 一键装 Docker
curl -fsSL https://get.docker.com | sh
docker --version
```

### 步骤 3：上传代码
```bash
# 本机执行：把项目传到服务器（不含 .venv）
cd /Users/lzc/Desktop/github
rsync -av --exclude '.venv' --exclude '.git' enterprise-kb/ root@你的公网IP:/opt/enterprise-kb/
```

### 步骤 4：把 API 加进 compose
在 `deploy/docker-compose.yml` 的 services 里加（这是波次 4 的活）：
```yaml
  api:
    build: ..
    command: uvicorn app.api.main:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    env_file: ../.env.prod
    depends_on:
      - postgres
      - redis
      - chroma
```

### 步骤 5：写 Dockerfile（项目根目录）
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 步骤 6：启动
```bash
cd /opt/enterprise-kb
docker-compose -f deploy/docker-compose.yml up -d --build
docker-compose logs -f api   # 看日志确认启动
curl http://localhost:8000/health   # 服务器上自测
```

### 步骤 7：配域名 + HTTPS（可选但推荐）
- 买域名 → 加 A 记录指向服务器 IP → 等 DNS 生效。
- 装 Nginx + Certbot 免费证书：
```bash
apt install nginx certbot python3-certbot-nginx -y
certbot --nginx -d ask.example.com   # 自动配 HTTPS
```
- Nginx 配置把 443 的请求转发到 `http://localhost:8000`。

### 步骤 8：验证
浏览器访问 `https://ask.example.com/docs`，调 `/auth/token` 拿 token，再调 `/ask`。

### 坑
- **安全组没放行** → 一直连不上，先查这个。
- **host 写 127.0.0.1** → 容器外访问不到，必须 0.0.0.0。
- **密钥明文进代码** → 用 `.env.prod` + Docker secrets，别提交到 git。
- **数据卷没挂** → 容器删了数据没，compose 里务必挂 volume。

---

## 六、路线 C：K8s（了解即可，现在别动手）

当你要管理几十个容器、需要自动扩缩容和故障自愈时才需要 K8s。
流程是：写 Deployment/Service YAML → 打包镜像推到镜像仓库 → kubectl apply →
配 Ingress 和 HPA。对当前项目是"杀鸡用牛刀"，但面试要能讲清**为什么需要它**
（无状态 API 水平扩容、模型服务独立扩缩、滚动发布、自愈）。

---

## 七、给你的行动顺序

1. **今天**：路线 A（Render）用 mock 档跑通，拿到一个能给别人看的网址。半小时搞定。
2. **本周**：路线 B 租台 ECS，把 Docker compose 跑起来，理解安全组/域名/Nginx。
3. **之后**：按 ROLLOUT.md 波次 1~3 补持久化、真模型、RAGAS，再重新部署。
4. **K8s**：等你真的要规模化了再说，现在只要求"能讲清".

## 八、面试话术

> **问：你把项目部署过吗？怎么部署的？**
>
> "部署过。我先用 PaaS 平台（Render/Railway）把服务跑起来验证链路——连 GitHub 仓库，配好 uvicorn 启动命令和环境变量，平台自动构建，给它给的网址就能访问。然后我在云主机上用 Docker Compose 部署了完整版：postgres、redis、chroma、minio 和 api 都在 compose 里编排，数据卷持久化，Nginx 做反向代理和 HTTPS 终结，安全组只放行 80/443。API 服务是无状态的，可以水平扩容；真正到多机规模才需要 K8s 做自动扩缩和滚动发布。"
>
> **追问：为什么用反向代理，不直接暴露 FastAPI？**
>
> "几个原因：HTTPS 证书在 Nginx 这一层统一处理，应用不用关心；Nginx 能做负载均衡和限流，把多个 API 实例挡在后面；还能隐藏后端端口和结构，减少攻击面。应用只监听本机端口，不直接暴露公网，这是基本的安全实践。"
