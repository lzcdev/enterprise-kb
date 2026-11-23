# API 服务容器镜像：把"代码 + Python 版本 + 依赖"打包成标准箱子，
# 解决"我这边能跑、你那边跑不了"的经典问题。
FROM python:3.13-slim

# 不装缓存、不写 pyc，镜像更小；PYTHONUNBUFFERED 让日志实时输出（容器日志排查必需）。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先只拷 requirements 并装依赖 —— Docker 分层缓存：依赖没变时这层命中缓存，
# 改业务代码不会重装一遍依赖（构建从分钟级降到秒级）。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷业务代码（放在依赖层之后，保证改代码不使依赖缓存失效）。
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY data/ ./data/

# 非 root 运行：容器逃逸时减小攻击面，生产安全基线。
RUN useradd --create-home --shell /bin/bash kb && chown -R kb:kb /app
USER kb

EXPOSE 8000

# 健康检查：K8s/编排器用它判断容器是否真正可用（比"进程活着"更准）。
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

# 启动命令。生产可通过环境变量/命令行覆盖；这里用 uvicorn，worker 数按 CPU 调。
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
