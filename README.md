# AI Agent 应用开发

> **第 1–2 周 · 工程基线 + 模型服务**
> 「12 周 AI Agent 应用开发 Roadmap」前两周的落地工程。Week 1 建立可复现的工程环境与最小模型调用服务；Week 2 将其升级为**稳定、可配置、可测试**的 FastAPI 模型服务（5 个端点、流式输出、并发限制、request_id 链路、统一错误映射、JSON 结构化日志）。

---

## 1. 项目内容

本仓库是「12 周 AI Agent 应用开发 Roadmap」**第 1–2 周**的落地工程。目标是建立可复现的 Python AI 应用环境、统一的代码质量与测试基线，并交付一个生产可用的模型调用服务，为后续 10 周打好地基。

**Week 1 · 工程基线（Day 1–5，打地基）**

- **Day 1 · 工程基线**：用 `uv` + `pyproject.toml` + `uv.lock` 搭建 Python 3.12 统一开发环境，引入 Ruff（lint + format）与 pytest 质量基线。
- **Day 2 · 配置与 JSON**：`src/config.py` 强类型配置加载（API key / base url / model name，来源 `.env` 或 `config.json`），Pydantic 校验。
- **Day 3 · 模型客户端**：`src/llm_client.py`，基于 `httpx` 异步调用 OpenAI 兼容的 chat-completions 接口，封装 `LlmAuthError` / `LlmTimeoutError` / `LlmServerError` 等错误信封。
- **Day 4 · 结构化输出**：`src/schemas.py`（`RequirementAnalysis` 等输出模型）与 `src/prompts.py`（系统提示 + 消息构造），JSON Mode 做结构化需求分析。
- **Day 5 · FastAPI 服务**：`src/app.py`（应用工厂 + `/health` `/chat` `/analyze-requirement` 端点 + 统一异常处理）与 `src/main.py`。

**Week 2 · 模型服务（Day 6–9，升级为稳定服务）**

- **Day 6 · 配置驱动**：`config.py` 升级为 `pydantic-settings` 从 `.env` 读取，新增 `max_concurrency` 等 8 个配置项；模型客户端支持 `LlmClient` 重试。
- **Day 7 · 客户端抽象 + 结构化输出联调**：`src/model_client.py`（`ModelClient` Protocol 抽象）+ `src/model_factory.py`（工厂）；`/analyze-requirement` 结构化闭环联调。
- **Day 8 · 模型清单 + 中间件 + 日志**：新增 `GET /models`；`src/middleware.py`（RequestId + 访问日志）、`src/logging_config.py`（JSON 日志 + `request_id` 链路追踪）；`/health` 扩展。
- **Day 9 · 流式 + 并发 + 错误统一**：新增 `POST /chat/stream`（SSE）；`asyncio.Semaphore` 并发限制；所有异常经 `_map_llm_error()` 统一映射为 `ErrorBody`（含 `request_id`）。

**最终实现**

- 一个 FastAPI 服务，暴露 5 个端点：健康检查、对话转发、流式对话、模型清单、结构化需求分析；
- 统一的错误信封（`ErrorBody`：`code` + `message` + `detail` + `status_code` + `request_id`），不向客户端泄露密钥与内部细节；
- 完整的 `request_id` 链路：从请求注入，贯穿所有日志（含第三方 httpx）与响应体/响应头；
- 完整的测试验证：截至 Week 2，`ruff` 通过、`pytest` **106 条全部通过**，远超 Roadmap「≥15 测试」要求。

---

## 2. 目录结构

```
week01_ai_basics/
├── README.md                # 本文件
├── pyproject.toml           # 项目元数据 + 依赖 + Ruff/pytest 配置
├── uv.lock                  # 依赖锁定（必须入库）
├── config.example.json      # 配置模板（真实 config.json 不入库）
├── .env.example             # 环境变量模板（真实 .env 不入库）
├── .gitignore               # Git 忽略规则
├── .python-version          # 锁定 Python 3.12（入库）
├── main.py                  # uv init 生成的最小 demo 入口（保留作 hello 示例）
├── src/                     # 业务代码（扁平包，Week 3 起将收进 src/llm_gateway_demo/）
│   ├── hello.py             # Day 1 hello（demo）
│   ├── config.py            # Day 2/6 配置（pydantic-settings，从 .env 读取）
│   ├── llm_client.py        # Day 3/6 Ollama 适配器（chat / chat_stream / 重试）
│   ├── schemas.py           # Day 4 LLM 输出契约（RequirementAnalysis 等）
│   ├── prompts.py           # Day 4 系统提示 + 消息构造
│   ├── api_models.py        # HTTP 线协议（ChatChunk/ModelInfo/ModelsResponse/ErrorBody 等）
│   ├── model_client.py      # Day 7 ModelClient 抽象（Protocol）
│   ├── model_factory.py     # Day 7 工厂：配置 → 客户端实例
│   ├── middleware.py        # Day 8 RequestId + AccessLog 中间件（纯 ASGI）
│   ├── logging_config.py    # Day 8 JSON 日志 + request_id 链路
│   ├── app.py               # Day 5/8/9 FastAPI 应用工厂 + 5 端点 + 异常映射
│   └── main.py              # Day 5 FastAPI 服务入口（fastapi dev/run 目标）
├── tests/                   # pytest 测试（8 个文件，106 用例）
│   ├── test_config.py            # AppConfig 配置加载 / 字段校验 / env 隔离
│   ├── test_model_client.py      # ModelClient Protocol 抽象与适配器契约
│   ├── test_llm_client.py        # LlmClient 异步 chat / chat_stream / 错误 / 重试
│   ├── test_streaming.py         # /chat/stream SSE 逐 chunk 推送
│   ├── test_concurrency.py       # asyncio.Semaphore 并发限制（MAX_CONCURRENCY）
│   ├── test_middleware.py        # RequestId / AccessLog 中间件（rid 注入与日志）
│   ├── test_api.py               # 5 端点集成测试（ASGITransport，覆盖成功/422/401/429/502/504/404）
│   └── test_structured_output.py # /analyze-requirement 结构化输出契约与校验
├── examples/                # 真实 / 样本数据
│   ├── requirement_samples.json    # 需求分析样本
│   └── structured_run_real.json    # 真实模型运行输出样本
├── scripts/                 # 一次性脚本（真实跑）
│   ├── run_real_ollama.py   # 真实调用 ollama 验证脚本（自带 src/ 路径）
│   └── serve.py             # 绕过 Windows AppLocker 的本地 ASGI launcher
├── logs/                    # 脚本运行日志（不入库）
│   └── ollama_run.log
└── docs/                    # 学习笔记、阶段交付物
    ├── day01_environment.md
    ├── day02_python_json.md
    ├── day03_http_async.md
    ├── day04_structured_output.md
    ├── week01_summary.md    # 第 1 周五天总结
    ├── week02_summary.md    # 第 2 周阶段总结
    └── poho/                # 运行 / 测试截图（不入库）
```

> 本项目的核心交付目录为 `src/`、`tests/`、`docs/`；`main.py`（demo 入口）、`config.example.json`、`scripts/`、`examples/`、`logs/` 为辅助文件。`Dockerfile` / `compose.yaml` 计划从第 9 周加入。
> **目录重构预告**：Week 3 起计划把扁平的 `src/` 收进 `src/llm_gateway_demo/` 包（按关注点分包：api / clients / agents / …），本次重构为一次性动作，不影响服务行为。

---

## 3. 技术栈

| 层次        | 选型                        | 用途                                  |
| ----------- | --------------------------- | ------------------------------------- |
| 运行环境    | Python 3.12                 | 主线语言；与 uv 锁定                  |
| 依赖管理    | uv + pyproject.toml         | 虚拟环境、依赖安装、锁文件            |
| 代码质量    | Ruff（lint + format）       | 静态检查 + 格式化 + import 排序        |
| 单元测试    | pytest + pytest-asyncio     | 单元 / 集成测试（asyncio_mode=auto）  |
| HTTP 客户端 | httpx                       | 异步模型接口调用（OpenAI 兼容）       |
| 数据模型    | Pydantic v2                 | 配置校验、结构化输出、HTTP 线协议      |
| 配置加载    | pydantic-settings           | 从 `.env` 读取 `AppConfig`（Week 2 起）|
| 流式响应    | sse-starlette               | `POST /chat/stream` 的 SSE 推送        |
| API 服务    | FastAPI[standard] + Uvicorn | 5 端点服务（含 `fastapi` CLI + uvicorn）|
| 环境变量    | python-dotenv               | 读取 `.env` 中的密钥与配置            |

---

## 4. 环境要求

| 工具            | 版本        | 校验命令                                                                                  |
| --------------- | ----------- | ----------------------------------------------------------------------------------------- |
| Python          | 3.12.13     | `uv run python --version`                                                                 |
| uv              | ≥ 0.5       | `uv --version`                                                                            |
| Git             | 任意新版    | `git --version`                                                                           |
| Ruff            | ≥ 0.15.22   | `uv run ruff --version`                                                                   |
| pytest          | ≥ 9.1.1     | `uv run pytest --version`                                                                 |
| pytest-asyncio  | ≥ 1.4.0     | `uv run python -c "import pytest_asyncio; print(pytest_asyncio.__version__)"`             |
| FastAPI[standard] | ≥ 0.139.2 | `uv run python -c "import fastapi; print(fastapi.__version__)"`                          |
| Uvicorn         | ≥ 0.51.0    | `uv run python -c "import uvicorn; print(uvicorn.__version__)"`                            |
| Pydantic        | ≥ 2.13.4    | `uv run python -c "import pydantic; print(pydantic.VERSION)"`                             |
| pydantic-settings | ≥ 2.14.2 | `uv run python -c "import pydantic_settings; print(pydantic_settings.__version__)"`      |
| sse-starlette   | ≥ 3.4.6     | `uv run python -c "import sse_starlette; print(sse_starlette.__version__)"`               |
| httpx           | ≥ 0.28.1    | `uv run python -c "import httpx; print(httpx.__version__)"`                               |
| python-dotenv   | ≥ 1.2.2     | `uv run python -c "import dotenv; print(dotenv.__version__)"`                             |

> **关于 `fastapi[standard]`（FastAPI 增强版）**：本项目锁定的是 `fastapi[standard]`，**不是**裸 `fastapi`。`[standard]` 额外捆绑了 `uvicorn` 与 `fastapi` 命令行工具（`fastapi dev` / `fastapi run`）。若只装了裸 `fastapi`，运行 `uv run fastapi dev src/main.py` 会报「需要安装 `fastapi[standard]`」的错误；请始终用 `uv add "fastapi[standard]"`，依赖已在 `pyproject.toml` 中声明。

> **不要在系统级 pip 安装依赖**。所有依赖都通过 `uv add` 加入 `pyproject.toml`，并由 `uv.lock` 锁定。

**本地模型服务（可选但推荐）**：`/chat` 等端点默认对接本地 Ollama。需自行安装并拉取模型，例如 `ollama pull qwen3`，并在 `.env` 中设置 `API_BASE_URL=http://localhost:11434/v1`、`MODEL_NAME=qwen3`。

---

## 5. 快速启动

```bash
# 1. 克隆仓库
git clone https://github.com/NicholasComa/AI-Agent.git
cd AI-agent

# 2. 同步依赖（首次会创建 .venv 并安装锁定的全部包）
uv sync

# 3. 复制环境变量模板(部分内容需自己配置)
cp .env.example .env         # Git Bash / PowerShell
# cmd 中：  copy .env.example .env

# 4. 启动 FastAPI 服务
uv run fastapi dev src/main.py      # 开发模式（自动重载）
uv run fastapi run src/main.py      # 生产模式
```

> 命令运行环境说明：
> - `mkdir` / `cp` / `rm` 走 **Git Bash**。
> - `copy` / `del` / `dir` 走 **cmd**。
> - `uv add` / `uv run` 在以上两种环境下命令一致。

---

## 6. API 服务

启动后默认监听 `http://127.0.0.1:8000`，共 5 个端点：

| 方法 | 路径 | 用途 | 输入 Schema | 输出 Schema |
| --- | --- | --- | --- | --- |
| GET  | `/health` | 服务状态 + 配置摘要（provider / max_concurrency 等） | - | `HealthResponse` |
| POST | `/chat` | 转发 chat-completions（非流式） | `ChatRequest` | `ChatResponse` |
| POST | `/chat/stream` | 流式对话，SSE 逐 chunk 推送 | `ChatRequest` | `text/event-stream`（`ChatChunk` 序列，末片 `done:true`） |
| GET  | `/models` | 列出可用模型 + 当前默认模型 | - | `ModelsResponse` |
| POST | `/analyze-requirement` | 结构化需求分析（JSON Mode + Pydantic 校验） | `AnalyzeRequirementRequest` | `AnalyzeRequirementResponse` (extends `RequirementAnalysis`) |

所有 I/O 模型定义在 `src/api_models.py`；所有错误统一为 `ErrorResponse`（内含 `ErrorBody`）：

```json
{
  "error": {
    "code": "llm_timeout",
    "message": "模型调用超时",
    "detail": "LlmTimeoutError",
    "status_code": 504,
    "request_id": "f925d1c50d1947b49c3e1339fca568dc"
  }
}
```

错误码 → HTTP 状态：

| code | HTTP | 触发场景 |
| --- | --- | --- |
| `validation_error`     | 422 | 请求体不符合 Pydantic 约束（空 messages / 越界 temperature / 非法 role / 空 text / 多传未定义字段等） |
| `not_found`            | 404 | 未知路由 |
| `method_not_allowed`   | 405 | 路由存在但方法不匹配 |
| `llm_auth`             | 401 | 401/403（API key 错） |
| `llm_rate_limit`       | 429 | 429 限流 |
| `llm_timeout`          | 504 | 模型超时（含重试耗尽） |
| `llm_server`           | 502 | 上游 5xx |
| `llm_bad_response`     | 502 | 响应不是合法 JSON / 不符合 `RequirementAnalysis` |
| `llm_error`            | 502 | 其它 `LlmError` |
| `internal_error`       | 500 | 未捕获异常（兜底） |

> `ChatRequest` 含 `model_config = ConfigDict(extra="forbid")`：**多传任何未定义字段都会直接 422 拒绝**。想流式请打 `/chat/stream`，不要往 `/chat` 的 body 里塞 `stream` 字段。

启动命令：

```bash
# 开发模式（自动重载）
uv run fastapi dev src/main.py

# 生产模式
uv run fastapi run src/main.py

# 直接用 Python 跑（走 src/main.py 的 __main__ 分支）
uv run python src/main.py
```

> 端点都**不写** API key 到日志 / 异常消息 / 响应体；`/health` 只返回 `key_configured: true/false`。所有响应体与响应头（`X-Request-ID`）都携带同源 `request_id`，可据此关联日志。

启动成功后可打开交互式 API 文档：`http://127.0.0.1:8000/docs`（Swagger UI）或 `http://127.0.0.1:8000/redoc`。API docs中的具体操作步骤如下：

```powershell
GET /health
# 直接点击“GET /health”栏后展开界面，点击“Try it out”后，点击“Execut”。

GET /models
# 操作同上 /health。

POST /chat
# 直接点击“POST /chat”栏后展开界面，点击“Try it out”后，会显示一个默认参数的“Request body”如下:
{
  "messages": [
    {
      "role": "system",
      "content": "string"
    }
  ],
  "model": "string",
  "temperature": 2,
  "max_tokens": 1
}
填写示例：
{
  "messages": [
    {
      "role": "user",
      "content": "用一句话介绍你自己"
    }
  ],
  "model": "qwen3"
}
填写完后点击“Execut”即可执行。

POST /chat/stream
# 主要操作同上 /chat

POST /analyze-requirement
# 直接点击“POST /analyze-requirement”栏后展开界面，点击“Try it out”后，会显示一个默认参数的“Request body”如下:
{
  "text": "string"
}
填写示例：
{
  "text":"我要做一个员工请假系统，支持手机端提交申请和领导审批"
}
填写完后点击“Execut”即可执行。

```

Git bash 中 API 服务对话操作步骤如下：
```bash
# /health
curl -s http://127.0.0.1:8000/health | python -m json.tool

# /chat（建议用 UTF-8 文件 + -d @req.json 传入中文，详见下方说明）
cat > req.json <<'EOF'
{"messages":[{"role":"user","content":"用一句话介绍你自己"}]}
EOF
curl -s -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d @req.json

# /chat/stream（必须加 -N 才能实时看到逐字输出）
curl -N -X POST http://127.0.0.1:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d @req.json

# /models
curl -s http://127.0.0.1:8000/models

# /analyze-requirement
cat > req2.json <<'EOF'
{"text":"我要做一个员工请假系统，支持手机端提交申请和领导审批"}
EOF
curl -s -X POST http://127.0.0.1:8000/analyze-requirement \
  -H "Content-Type: application/json" \
  -d @req2.json
```

> **中文测试提示（Windows 终端编码问题）**：Git Bash / cmd 默认控制台输入代码页为 936(GBK)，直接内联中文（如 `-d '{"content":"你好"}'`）会产生 GBK 字节，FastAPI 只认 UTF-8，于是报 `400 There was an error parsing the body`。**稳定解法**：
> 1. 用 VS Code 保存一个 **UTF-8** 的 `req.json`，再以 `-d @req.json` 传入（推荐）；
> 2. 或直接在浏览器 `http://127.0.0.1:8000/docs` 的 Swagger UI 里 Try it out 打中文（浏览器自动发 UTF-8）。
> 3. 直接使用英文进行对话。

---

## 7. 测试与质量

```bash
# 全部测试（Week 2 共 106 passed）
uv run pytest -q

# 按模块运行（部分示例）
uv run pytest -v tests/test_api.py
uv run pytest -v tests/test_streaming.py
uv run pytest -v tests/test_concurrency.py

# 单个用例
uv run pytest -v tests/test_api.py::test_chat_timeout_returns_504

# 静态检查 + 格式化
uv run ruff check .
uv run ruff format --check .

# 真实环境探活（要求 .env 已配置模型 API 或 Ollama API 且 ollama 在跑）
curl http://127.0.0.1:8000/health
```

**本周完整测试内容（8 个文件 / 106 用例）**

| 测试文件 | 覆盖主题 | 关键验证点 |
| --- | --- | --- |
| `test_config.py` | `AppConfig` 配置加载 | `.env` 读取、字段类型校验、`extra` 字段拒绝、env 隔离（不污染其它用例） |
| `test_model_client.py` | `ModelClient` 抽象 | Protocol 契约、适配器可被 `isinstance` 校验、与具体后端解耦 |
| `test_llm_client.py` | `LlmClient` 适配器 | 异步 `chat()` / `chat_stream()`、各类 `LlmError` 抛出、重试逻辑 |
| `test_streaming.py` | 流式接口 | `/chat/stream` 逐 `ChatChunk` 推送、末片 `done:true`、中断处理 |
| `test_concurrency.py` | 并发限制 | `asyncio.Semaphore` 限制同时打后端的请求数（`MAX_CONCURRENCY`） |
| `test_middleware.py` | 中间件 | `RequestIdASGIMiddleware` 注入/传播 rid、`AccessLogMiddleware` 结构化日志且不读敏感字段 |
| `test_api.py` | 5 端点集成 | 用 `httpx.ASGITransport` 在内存跑整个 ASGI 栈，覆盖 `/health` `/chat` `/chat/stream` `/models` `/analyze-requirement` 的成功与 422/401/429/502/504/404 全分支 |
| `test_structured_output.py` | 结构化输出 | `/analyze-requirement` 返回符合 `RequirementAnalysis` 契约、JSON Mode 闭环 |

**测试架构要点**

- API 测试用 `httpx.ASGITransport` 在内存里跑整个 ASGI 栈（启动 lifespan、调端点、关闭 lifespan），**不依赖网络**；通过 `create_app(llm_factory=..., config_loader=...)` 注入假 `LlmClient`，覆盖健康/成功/各类错误全分支。
- 并发 / 流式 / 中间件测试同样依赖注入假客户端，保证离线可跑、无副作用。
- `pytest-asyncio` 设为 `asyncio_mode="auto"`，异步用例无需显式标记。
- `pythonpath=["src"]` 已配置，测试内 `from app import create_app` 等扁平的 import 可直接解析。
