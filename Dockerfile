# Agent 服务镜像：多阶段构建，运行阶段只带虚拟环境与源码。
#
# 本机 `uv` 版本为 0.11.30，构建阶段从官方镜像取同版本二进制，保证
# `uv.lock` 的解析结果与开发机一致。若该标签不可用，改成 `latest` 或
# `uv --version` 输出的版本号即可。

# ---------------------------------------------------------------------------
# 构建阶段：按 uv.lock 装依赖到 /opt/venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# 先只拷依赖清单：源码改动不会让依赖层缓存失效。
# --no-install-project：项目没有声明构建后端，应用直接从源码运行，
# 不需要把自己装成分发。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# 运行阶段：只带虚拟环境与运行所需文件
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY data/ ./data/
COPY examples/ ./examples/
COPY README.md config.example.json ./

# 非 root 运行：即使容器被突破，攻击面也只限于 appuser 的权限。
# 运行期要写的目录先建好并交给 appuser——具名卷首次挂载时会继承镜像里该路径的
# 属主；目录不存在则会被建成 root 所有，导致会话存储写不进去、/ready 直接 503。
RUN mkdir -p /app/data/agent_service_sessions /app/data/mcp_sandbox /app/logs \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /opt/venv

USER appuser

EXPOSE 8080

# 探针用标准库发起，避免为 healthcheck 额外安装 curl。
# 打 /health 而不是 /ready：前者只判进程存活，依赖故障时容器不应被反复重启。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"]

CMD ["python", "-m", "uvicorn", "src.agent_service.app:app", "--host", "0.0.0.0", "--port", "8080"]
