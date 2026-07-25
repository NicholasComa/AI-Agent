# AI Agent 应用开发

> **第 1 周 · AI 与 Python 应用基础**
> 12 周 AI Agent 应用开发 Roadmap 的起点。实现 Python AI 应用工程，建立可复现的工程环境、统一的代码质量与测试基线。

---

## 1. 项目内容

本仓库是「12 周 AI Agent 应用开发 Roadmap」**第 1 周 · AI 与 Python 应用基础** 的落地工程。目标是建立可复现的 Python AI 应用工程环境、统一的代码质量与测试基线，并交付一个最小可运行的模型调用服务，为后续 11 周打好地基。

五天任务由浅入深，逐步搭出完整链路：

- **Day 1 · 工程基线**：用 `uv` + `pyproject.toml` + `uv.lock` 搭建 Python 3.12 统一开发环境，引入 Ruff（lint + format）与 pytest 质量基线，输出最小 `src/hello.py` 与环境记录 `docs/day01_environment.md`。
- **Day 2 · 配置与 JSON**：实现 `src/config.py` 强类型配置加载（API key / base url / model name，来源 `.env` 或 `config.json`），用 Pydantic 校验，配套 `tests/test_config.py` 与 JSON 学习笔记。
- **Day 3 · 模型客户端**：实现 `src/llm_client.py`，基于 `httpx` 异步调用 OpenAI 兼容的 chat-completions 接口，封装 `LlmAuthError` / `LlmTimeoutError` / `LlmServerError` 等错误信封，配套 mock 测试。
- **Day 4 · 结构化输出**：定义 `src/schemas.py`（`RequirementAnalysis` 等输出模型）与 `src/prompts.py`（系统提示 + 消息构造），开启 JSON Mode 做结构化需求分析，配套 `tests/test_structured_output.py` 与真实 ollama 验证脚本。
- **Day 5 · FastAPI 服务**：交付 `src/app.py`（应用工厂 + `/health` `/chat` `/analyze-requirement` 三端点 + 统一异常处理）与 `src/main.py`（入口），用 Pydantic 做请求/响应校验，全部异常收敛为统一的 `ErrorResponse` 信封；配套 `tests/test_api.py`（20 用例，全仓累计 65 用例）。

**最终实现**

- 一个最小 FastAPI 服务，暴露健康检查、对话转发、结构化需求分析三个端点；
- 统一的错误信封（`code` + `message` + `detail`+`HTTP状态码`），不向客户端泄露密钥与内部细节；
- 完整的测试验证：截至 Day 5，`ruff` 通过、`pytest` 65条通过。

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
├── main.py                  # uv init 生成的最小入口（保留作 demo）
├── src/                     # 业务代码
│   ├── hello.py             # Day 1 hello
│   ├── config.py            # Day 2 typed 配置加载
│   ├── llm_client.py        # Day 3 async chat-completions 客户端
│   ├── schemas.py           # Day 4 LLM 输出契约
│   ├── prompts.py           # Day 4 系统提示 + 消息构造
│   ├── api_models.py        # Day 5 HTTP wire 契约（请求/响应/错误）
│   ├── app.py               # Day 5 FastAPI 应用工厂 + 端点 + 异常处理
│   └── main.py              # Day 5 FastAPI 服务入口（fastapi dev/run 目标）
├── tests/                   # pytest 测试
│   ├── test_config.py       # Day 2
│   ├── test_llm_client.py   # Day 3
│   ├── test_structured_output.py  # Day 4
│   └── test_api.py          # Day 5
├── examples/                # 真实 / 样本数据
│   ├── requirement_samples.json    # 需求分析样本
│   └── structured_run_real.json    # 真实模型运行输出样本
├── scripts/                 # 一次性脚本（真实跑）
│   ├── run_real_ollama.py   # 真实调用 ollama 验证脚本
│   └── serve.py             # 绕过 Windows AppLocker 的本地 ASGI launcher
├── logs/                    # 脚本运行日志（不入库）
│   └── ollama_run.log
└── docs/                    # 学习笔记、阶段交付物
    ├── day01_environment.md # Day 1 环境记录
    ├── day02_python_json.md # Day 2 Python/JSON/配置笔记
    ├── day03_http_async.md  # Day 3 HTTP/异步笔记
    ├── day04_structured_output.md # Day 4 结构化输出笔记
    ├── week01_summary.md    # 第 1 周五天总结
    └── poho/                # 运行 / 测试截图（不入库）
```

> 本项目的核心交付目录为 `src/`、`tests/`、`docs/`；`main.py`（demo 入口）、`config.example.json`、`scripts/`、`examples/`、`logs/` 为辅助文件。`Dockerfile` / `compose.yaml` 计划从第 9 周加入。

---

## 3. 技术栈

| 层次        | 选型                        | 用途                                  |
| ----------- | --------------------------- | ------------------------------------- |
| 运行环境    | Python 3.12.13               | 主线语言；与 uv 锁定                  |
| 依赖管理    | uv + pyproject.toml         | 虚拟环境、依赖安装、锁文件            |
| 代码质量    | Ruff（lint + format）       | 静态检查 + 格式化 + import 排序        |
| 单元测试    | pytest                      | 单元 / 集成测试                       |
| HTTP 客户端 | httpx                       | 同步 + 异步模型接口调用（Day 3 起）   |
| 数据模型    | Pydantic v2                 | 配置校验、结构化输出                   |
| API 服务    | FastAPI[standard] + Uvicorn | 第 1 周最小服务（增强版含 `fastapi` CLI + uvicorn） |
| 配置加载    | python-dotenv               | 读取 `.env` 中的密钥与配置            |

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
| httpx           | ≥ 0.28.1    | `uv run python -c "import httpx; print(httpx.__version__)"`                               |
| python-dotenv   | ≥ 1.2.2     | `uv run python -c "import dotenv; print(dotenv.__version__)"`                             |

> **关于 `fastapi[standard]`（FastAPI 增强版）**：本项目锁定的是 `fastapi[standard]`，**不是**裸 `fastapi`。`[standard]` 额外捆绑了 `uvicorn` 与 `fastapi` 命令行工具（`fastapi dev` / `fastapi run`）。若只装了裸 `fastapi`，运行 `uv run fastapi dev src/main.py` 会报「需要安装 `fastapi[standard]`」的错误；请始终用 `uv add "fastapi[standard]"` 安装，依赖已在 `pyproject.toml` 中声明。

> **不要在系统级 pip 安装依赖**。所有依赖都通过 `uv add` 加入 `pyproject.toml`，并由 `uv.lock` 锁定。

---

## 5. 快速启动

```bash
# 1. 克隆仓库
git clone https://github.com/NicholasComa/AI-Agent.git
cd week01_ai_basics

# 2. 同步依赖（首次会创建 .venv 并安装锁定的全部包）
uv sync

# 3. 复制环境变量模板
cp .env.example .env         # Git Bash / PowerShell
# cmd 中：  copy .env.example .env

# 4. 探活 / 运行主入口
uv run python main.py
# Day 5 起：FastAPI 服务
uv run fastapi dev src/main.py      # 开发模式（自动重载）
uv run fastapi run src/main.py      # 生产模式
```
第一次使用`uv`运行需要执行:
```bash
uv init --python 3.12
uv add httpx pydantic python-dotenv fastapi "uvicorn[standard]"
uv add --dev ruff pytest
uv run python --version
uv run ruff --version
uv run pytest --version
```

> 命令运行环境说明：
> - `mkdir` / `cp` / `rm` 走 **Git Bash**。
> - `copy` / `del` / `dir` 走 **cmd**。
> - `uv add` / `uv run` 在以上两种环境下命令一致。
> - fastapi也可以用这个安装：`uv pip install "fastapi[standard]"`。
---

## 6. API 服务

启动后默认监听 `http://127.0.0.1:8000`，三个端点：

| 方法 | 路径 | 用途 | 输入 Schema | 输出 Schema |
| --- | --- | --- | --- | --- |
| GET  | `/health` | 服务状态 + 配置摘要 | - | `HealthResponse` |
| POST | `/chat` | 转发 chat-completions | `ChatRequest` | `ChatResponse` |
| POST | `/analyze-requirement` | 结构化需求分析（JSON Mode + Pydantic 校验） | `AnalyzeRequirementRequest` | `AnalyzeRequirementResponse` (extends `RequirementAnalysis`) |

所有 I/O 模型定义在 `src/api_models.py`；所有错误统一为 `ErrorResponse`：

```json
{"error": {"code": "llm_timeout", "message": "...", "detail": "...", "status_code": "..."}}
```

错误码 → HTTP 状态：

| code | HTTP | 触发场景 |
| --- | --- | --- |
| `validation_error`     | 422 | 请求体不符合 Pydantic 约束（空 messages / 越界 temperature / 非法 role / 空 text 等） |
| `not_found`            | 404 | 未知路由 |
| `method_not_allowed`   | 405 | 路由存在但方法不匹配 |
| `llm_auth`             | 401 | 401/403（API key 错） |
| `llm_rate_limit`       | 429 | 429 限流 |
| `llm_timeout`          | 504 | 模型超时（含重试耗尽） |
| `llm_server`           | 502 | 上游 5xx |
| `llm_bad_response`     | 502 | 响应不是合法 JSON / 不符合 `RequirementAnalysis` |
| `llm_error`            | 502 | 其它 `LlmError` |
| `internal_error`       | 500 | 未捕获异常（兜底） |

启动 Fastapi 服务器命令：

```bash
# 开发模式（自动重载）
uv run fastapi dev src/main.py

# 生产模式
uv run fastapi run src/main.py

# 直接用 Python 跑（走 src/main.py 的 __main__ 分支）
uv run python src/main.py
```

> 端点都**不写** API key 到日志 / 异常消息 / 响应体；`/health` 只返回 `key_configured: true/false`。

启动成功后显示后台服务页面：

```bash
 ⚡️ Starting FastAPI in development mode

 🐍 Using import string: main:app

 🌐 Server started at http://127.0.0.1:8000
    Documentation at http://127.0.0.1:8000/docs

  Logs:

 ▕  Will watch for changes in these directories: ['D:\\workspace\\py_ai\\week01_ai_basics']
 ▕  Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
 ▕  Started reloader process [12192] using WatchFiles
 ▕  Started server process [16256]
 ▕  Waiting for application startup.
 ▕  Application startup complete.
```

此时可以直接使用Fastapi自带的交互式API文档：http://127.0.0.1:8000/docs ，或者可以打开另一个终端输入指令进行对话。
```bash
#health记录：
curl -s http://127.0.0.1:8000/health | python -m json.tool


#chat示例:
cat > req.json <<'EOF'
{"messages":[{"role":"user","content":"用一句话介绍你自己"}]}
EOF

curl -s -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d @req.json

#analyze-requirement示例:
cat > req2.json <<'EOF'
{"text":"我要做一个员工请假系统，支持手机端提交申请和领导审批"}
EOF

curl -s -X POST http://127.0.0.1:8000/analyze-requirement \
  -H "Content-Type: application/json" \
  -d @req2.json

#测试“输入校验失败”：
curl -s -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" -d '{"messages":[]}'

#测试“路径不存在”:
curl -s http://127.0.0.1:8000/nope
```

---

## 7. 测试与质量

```bash
# 全部测试（截至 Day 5 共 65 passed）
uv run pytest -q

# 仅 API 层
uv run pytest -v tests/test_api.py

# 单个用例
uv run pytest -v tests/test_api.py::test_chat_timeout_returns_504

# 静态检查 + 格式化
uv run ruff check .
uv run ruff format --check .

# 真实环境探活（要求 .env 已配置）
curl http://127.0.0.1:8000/health
```

API 测试用 `httpx.ASGITransport` 在内存里跑整个 ASGI 栈（启动 lifespan、调端点、关闭 lifespan），不依赖网络；通过 `create_app(llm_factory=..., config_loader=...)` 注入假 LlmClient，覆盖健康/成功/422/401/429/502/504/404 全分支。

