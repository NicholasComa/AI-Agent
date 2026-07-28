# Day 6 · 概念 + 工程脚手架 + Pydantic Settings

> 范围：Roadmap 第 2 周 Day 6。把第 1 周 Demo 改造为稳定、可配置、可测试的模型服务。
> 适用：南宁软件组 AI Agent 应用开发 12 周 Roadmap 学员。
> 文档位置：`docs/day06_concepts.md`。
> 本文档含：当日任务、5 个核心概念与示例、关键点、以及 **Day 6 测试命令与步骤**。

---

## 1. 当日目标

| # | 任务 | 状态 |
| --- | --- | --- |
| 1 | `pyproject.toml` 改名 `llm-gateway-demo` + 新增依赖 `pydantic-settings`、`sse-starlette` | ✅ |
| 2 | `src/config.py` 升级为 Pydantic Settings（`AppConfig(BaseSettings)`）+ 8 个 Week 02 字段 | ✅ |
| 3 | `tests/test_config.py` 加 3 个新用例（12 旧 + 3 新 = 15） | ✅ |
| 4 | `.env.example` 完整化（13 字段，含 `APP_NAME`） | ✅ |
| 5 | `ruff check` / `ruff format` / 全套 `pytest` 验收通过 | ✅ |

> 本周第 2 周：阶段任务要求完成 `llm_gateway_demo`（`/chat`、`/chat/stream`、`/models`、`/health`），并实现统一 `ModelClient` 接口。

---

## 2. 运行环境

| 工具 | 版本 / 说明 |
| --- | --- |
| 操作系统 | Windows 11（Git Bash + cmd） |
| uv | 默认不在 PATH，所有 `uv` 命令前先 `export PATH="/c/Users/Xsz/.local/bin:$PATH"` |
| Python | 3.12.13（uv 管理的 `.venv/`） |
| 新增依赖 | `pydantic-settings>=2.14.2`、`sse-starlette>=3.4.6` |
| 虚拟环境 | `D:\workspace\py_ai\week01_ai_basics\.venv\Scripts\python.exe` |

---

## 3. 核心概念

### 概念 1 · Pydantic Settings（`BaseSettings`）

`BaseSettings` 让配置自动从「进程环境变量 → `.env` 文件 → 字段默认值」加载，替代 week01 的手写 loader。

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class AppConfig(BaseSettings):
    model_name: str                      # 必填
    api_base_url: str                    # 必填
    timeout_seconds: float = 30.0        # 带默认
    enable_stream: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

# 直接用：自动读 env / .env
cfg = AppConfig()
```

**为什么 Day 6 没直接 `AppConfig()` 自动加载？** 见 §4 。

---

### 概念 2 · `BaseHTTPMiddleware`（请求/响应中间件）

用于注入 `request_id`、写访问日志、脱敏。是 Starlette 的中间件基类，FastAPI 直接复用。

```python
import uuid
from starlette.middleware.base import BaseHTTPMiddleware

class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id   # 回传响应头
        return response
```

> Day 8 会在此基础上加 `AccessLogMiddleware`：JSON 行日志，**绝不记** `Authorization` 头、**绝不记** `messages[].content`。

---

### 概念 3 · `StreamingResponse`（SSE 流式）

`/chat/stream` 用 `text/event-stream` 逐块返回 token，每片 `data: {...}\n\n`，末尾 `data: [DONE]\n\n`。

```python
from fastapi.responses import StreamingResponse
import asyncio, json

async def event_stream():
    for chunk in ["Hello", " ", "world"]:
        yield f"data: {json.dumps({'delta': chunk}, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.1)
    yield "data: [DONE]\n\n"

@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

> `sse-starlette` 在 Day 6 已装，Day 9 用于更稳健的 SSE 服务端推送。

---

### 概念 4 · `asyncio.Semaphore`（并发限制）

限制同时打到上游的请求数，超出排队等待，避免把上游打爆。

```python
import asyncio

limiter = asyncio.Semaphore(8)   # 对应 config.max_concurrency

async def call_upstream():
    async with limiter:                  # 进入临界区才真正发请求
        return await _do_request()
```

> 通过构造参数注入，便于测试用 `Semaphore(2)` 验证「3 并发排队」。

---

### 概念 5 · `CancelledError` 与取消请求 

客户端断开连接时，服务端生成器被取消（抛 `CancelledError`）。需检测断开并中止上游，避免空转。

```python
from fastapi import Request

@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    try:
        async for chunk in client.chat_stream(req.messages):
            if await request.is_disconnected():
                break            # 客户端断开 -> 中止上游
            yield chunk
    except asyncio.CancelledError:
        await client.aclose()    # 清理上游连接
        raise
```

---

## 4. 关键点：BaseSettings 自动读 env 会污染 `model_validate`

`BaseSettings.model_validate()` **会自动从 `os.environ` 填充缺失字段**。本机若设了 `API_KEY=ollama` / `API_BASE_URL=...`，会让 `load_config(source="json")` 的严格语义被破坏（JSON 里没写的字段被 env 偷偷补上）。

**修复**：用 `settings_customise_sources` 禁用自动 env / `.env` / secrets 加载，只保留 `init_settings`（显式 kwargs）。

```python
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

class AppConfig(BaseSettings):
    model_name: str
    api_base_url: str
    # ... 其他字段 ...

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings,
        dotenv_settings, file_secret_settings,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 只保留 init kwargs，禁用 env / .env / secrets 自动加载
        return (init_settings,)

    @classmethod
    def from_env_file(cls, env_file: str = ".env") -> "AppConfig":
        """显式从 .env 文件 + os.environ 加载（进程 env 覆盖文件）。"""
        # ... 解析 .env 并合并 os.environ，再 model_validate(data) ...
```

> 影响后续 Day 7-9：所有「显式注入」路径（`LlmClient(api_key=...)`、factory 构造 `AppConfig`）都要警惕 env 自动污染。统一用 `model_validate(data)` 或 `from_env()` / `from_env_file()` 显式入口。

---

## 5. Day 6 测试命令与步骤

> 所有命令从项目根 `D:\workspace\py_ai\week01_ai_basics` 执行。Git Bash 每条前先：
> `export PATH="/c/Users/Xsz/.local/bin:$PATH"`
> cmd 每条前先：`set PATH=C:\Users\Xsz\.local\bin;%PATH%`，且 `cd /d D:\workspace\py_ai\week01_ai_basics`。

### 任务项 1 · pyproject 改名 + 加依赖

```bash
export PATH="/c/Users/Xsz/.local/bin:$PATH"
cd /d/workspace/py_ai/week01_ai_basics

# 1a. 确认 name 已改
grep -n '^name = ' pyproject.toml

# 1b. 确认两个新依赖可导入
uv run python -c "import pydantic_settings, sse_starlette; print('deps ok')"
```

**预期**：`name = "llm-gateway-demo"`；输出 `deps ok`。

> cmd 对照：`grep -n '^name = ' pyproject.toml` → `findstr "name = " pyproject.toml`

### 任务项 2 · Pydantic Settings 升级 `src/config.py`

```bash
# 2a. model_validate 严格——不被 os.environ 污染（关键修复点）
uv run python -c "
from src.config import AppConfig
c = AppConfig.model_validate({'model_name': 'm', 'api_base_url': 'http://x/v1'})
print('api_key=', repr(c.api_key), '| provider=', c.provider, '| max_concurrency=', c.max_concurrency)
"

# 2b. 缺必填字段应抛 ValidationError
uv run python -c "
from src.config import AppConfig
try:
    AppConfig.model_validate({'model_name': 'm'})
    print('NO RAISE (wrong)')
except Exception as e:
    print('RAISED', type(e).__name__)
"

# 2c. from_env_file 能从 .env 加载（必须先填 MODEL_NAME，模板空值会按预期报错）
printf 'API_KEY=test-key\nAPI_BASE_URL=https://api.example.com/v1\nMODEL_NAME=test-model\n' > .env
uv run python -c "
from src.config import AppConfig
c = AppConfig.from_env_file()
print('model_name=', c.model_name, '| timeout=', c.timeout_seconds, '| enable_stream=', c.enable_stream)
"
rm -f .env
```

**预期**：
- 2a：`api_key=''`、`provider='openai_compatible'`、`max_concurrency=8`（无 env 污染）
- 2b：`RAISED ValidationError`
- 2c：`model_name= test-model | timeout= 30.0 | enable_stream= True`

> ⚠️ **注意**：`cp .env.example .env` 后**直接** `from_env_file()` 会报 `model_name required`，这是**正确行为**——模板里 `MODEL_NAME` 是空占位符（必填项）。必须填值后才能加载。这也演示了路线的「必填项校验」。

### 任务项 3 · 新增 3 个 Day 6 测试

```bash
uv run pytest tests/test_config.py -v
```

**预期**：`15 passed`，其中 3 个新用例为：
- `test_settings_does_not_read_os_environ`
- `test_from_env_file_reads_dotenv`
- `test_load_auto_env_missing_required_falls_back_to_json`

### 任务项 4 · 完整化 `.env.example`（13 字段含 `APP_NAME`）

```bash
uv run python -c "
from dotenv import dotenv_values
d = dotenv_values('.env.example')
keys = ['APP_NAME','API_KEY','API_BASE_URL','MODEL_NAME','TIMEOUT_SECONDS','ENABLE_STREAM','MAX_RETRIES','RETRY_BACKOFF','MAX_CONCURRENCY','PROVIDER','LOG_LEVEL','LOG_FORMAT','EXTRA_MODELS']
print('missing:', [k for k in keys if k not in d])
print('count:', len(d))
"
```

**预期**：`missing: []`、`count: 13`。

### 任务项 5 · ruff + 全套 pytest 验收

```bash
# 5a. lint
uv run ruff check src tests

# 5b. 格式检查
uv run ruff format --check src tests

# 5c. 全套测试
uv run pytest -q
```

**预期**：ruff `All checks passed!`；format `12 files already formatted`；pytest `68 passed`。

### 一键汇总脚本（Git Bash）

```bash
export PATH="/c/Users/Xsz/.local/bin:$PATH"
cd /d/workspace/py_ai/week01_ai_basics
echo "== deps ==" && uv run python -c "import pydantic_settings, sse_starlette; print('deps ok')"
echo "== config 测试 ==" && uv run pytest tests/test_config.py -q
echo "== 全套 ==" && uv run pytest -q
echo "== ruff ==" && uv run ruff check src tests && uv run ruff format --check src tests
```

---

## 6. 复验结果（Day 6 完结时）

| 检查项 | 结果 |
| --- | --- |
| `tests/test_config.py` | 15 passed |
| 全套 `pytest -q` | 68 passed（week01 的 65 + 新增 3） |
| `ruff check src tests` | All checks passed! |
| `ruff format --check` | 12 files already formatted |
| `.env.example` 字段数 | 13（missing: []） |

> 修复记录：初版 `.env.example` 漏 `APP_NAME`（实 12 字段），已补；`src/app.py` 格式经 `ruff format` 对齐（week01 遗留，非 Day 6 引入）。均未 commit（用户指令：助手不碰 git）。

---

## 7. 下一步

- **Day 7**：定义 `ModelClient(Protocol)` + 升级 `LlmClient`（加 `chat_stream`）+ factory；`tests/test_model_client.py` ≥ 6 例。
- **Day 8**：`RequestIdMiddleware` + `AccessLogMiddleware` + JSON 日志 + `contextvars` 注入 `request_id`。
- **Day 9**：`/chat/stream` + `/models` + `ConcurrencyLimiter` + 边界测试（超时 / 限流 / 错误 JSON / 空响应 / 取消请求）。
- **Day 10**：`docs/week02_summary.md` + Swagger UI 截图 + 通过标准自检（空目录重建 + 切换模型只改配置）。
