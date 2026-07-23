# Day 3 · HTTP 客户端与异步调用

> 范围：完成 Roadmap 第 1 周 Day 3 的 HTTP / 异步应用基础任务，落地异步 LLM 客户端 `LlmClient` 与 mock 测试。
> 适用：南宁软件组 AI Agent 应用开发 12 周 Roadmap 学员。
> 文档位置：`docs/day03_http_async.md`。


---

## 1. 当日目标

| # | 任务 | 状态 |
| --- | --- | --- |
| 1 | 理解 HTTP 请求/响应、Header、状态码、超时、重试 | ✅ |
| 2 | 理解 sync/async、asyncio、`httpx.Client` vs `AsyncClient` | ✅ |
| 3 | 实现 `LlmClient`：输入 messages，返回 `text / model / elapsed_ms` | ✅（`src/llm_client.py`） |
| 4 | 错误分类（401/403/429/5xx/超时/格式错）+ 仅重试临时失败 | ✅ |
| 5 | API Key 仅从环境变量读取，日志与异常消息脱敏 | ✅ |
| 6 | 提供 mock 测试，不依赖真实模型也能跑基本单元测试 | ✅（`tests/test_llm_client.py` |
| 7 | 接入真实模型（DeepSeek）做端到端验证 | ⚠️ 已接 `.env`，但当前使用**非真实 key** |

---

## 2. 当日交付物

```
week01_ai_basics/
├── src/
│   ├── hello.py             # Day 1
│   ├── config.py            # Day 2：AppConfig + load_config
│   └── llm_client.py        # ★ Day 3：LlmClient + LlmResult + 异常层级
├── tests/
│   ├── test_config.py       # Day 2：12 用例
│   └── test_llm_client.py   # ★ Day 3：25 用例（全部 mock）
├── .env                     # 真实 key（gitignored；当前为非真实值，仅用于联调/安全）
├── .env.example             # 模板，不填真实值
└── docs/
    ├── day01_environment.md
    ├── day02_python_json.md
    └── day03_http_async.md  # ★ 本文档
```

---

## 3. 理论要点

| 主题 | 一句话 |
| --- | --- |
| HTTP | 客户端发 Request（Method/Path/Header/Body），服务端回 Response（Status/Header/Body） |
| 状态码 | 2xx 成功；4xx 客户端错（不重试）；5xx/429/408 临时失败（重试） |
| 超时 | connect / read / total 三段，LLM 用分段超时避免长生成被切断 |
| 重试 | 只重试临时失败：429/5xx/408 + 连接/读超时；指数退避 `base * 2**attempt` |
| sync vs async | `async/await` 解决 I/O 密集并发，单线程可扛上千连接 |
| httpx 两套 API | `Client`（sync）/ `AsyncClient`（async），互不兼容 |
| 测试替身 | `httpx.MockTransport` 短路网络，无需第三方库 |

---

## 4. LlmClient 设计

文件：`src/llm_client.py`。这是一个对 OpenAI 兼容 `POST /chat/completions` 的**异步薄封装**，三大目标：健壮性、Key 安全、可测试。

### 4.1 返回结构 `LlmResult`（frozen dataclass）

```python
@dataclass(frozen=True)
class LlmResult:
    text: str                       # 助手回复文本
    model: str                      # 服务端回显的模型名（可能与请求不同）
    elapsed_ms: int                 # 整次调用耗时（含重试），毫秒
    usage: dict[str, int] | None = None   # token 用量（可选）
```

### 4.2 入口 `LlmClient.chat()`

```python
async def chat(
    self,
    messages: list[dict[str, str]],       # 输入：非空的 {role, content} 列表
    *,
    model: str | None = None,             # 可选：覆盖默认模型
    extra_body: dict[str, Any] | None = None,  # 可选：额外字段（如 temperature）
) -> LlmResult:                           # 返回：文本结果 + 模型名 + 耗时 + 用量
```

`messages` 为空会抛 `ValueError`；`Authorization: Bearer <key>` 头在内部拼好。

### 4.3 异常层级（5 个派生类 + 基类）

| 异常 | 触发 | 是否重试后抛 |
| --- | --- | --- |
| `LlmError` | 基类，其它 4xx（400/404…） | — |
| `LlmAuthError` | **401/403** 或缺 key | 否（立即） |
| `LlmRateLimitError` | **429** 重试耗尽 | 是 |
| `LlmServerError` | **5xx** 重试耗尽 | 是 |
| `LlmTimeoutError` | 连接/读超时重试耗尽 | 是 |
| `LlmResponseFormatError` | 响应不是合法 chat-completion | 否 |

### 4.4 重试策略

```python
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
DEFAULT_MAX_RETRIES = 2          # 最多 1 + 2 = 3 次尝试
DEFAULT_RETRY_BACKOFF = 0.5      # 真实延迟 = 0.5 * 2**attempt
```

- 重试白名单：408/429/5xx + `httpx.ConnectError` + `httpx.TimeoutException`。
- 4xx（除 408/429）是客户端错，**不重试**，直接抛出去让人改。
- 退避用指数退避，避免把后端打爆。

### 4.5 API Key 安全

- Key **默认从环境变量 `API_KEY` 读取**（`api_key=None` 时）；也可显式传 `api_key=`。
- 缺失 key → 抛 `LlmAuthError`。
- 日志与异常消息**只打 `http=... body=...`**，绝不打请求头（Authorization 永远留在 `_api_key` 属性里）。
- 响应体经 `_redact()` 处理：若服务端/代理意外回显了 key，自动替换成 `[REDACTED]`。

### 4.6 资源生命周期

`LlmClient` 是异步上下文管理器，也支持 `await client.aclose()`：

```python
async with LlmClient(base_url=..., model=...) as llm:
    result = await llm.chat([{"role": "user", "content": "你好"}])
```

注入 `client=httpx.AsyncClient(...)` 时（`_owns_client=False`），`aclose()` 是 no-op，由调用方负责关闭——测试就靠这个避免关闭 Mock 客户端。

---

## 5. Mock 测试

文件：`tests/test_llm_client.py`，**25 个用例**，全量测试（含 Day 2 配置 12 个）共 **37 passed**。

### 5.1 机制

核心是 `_make_client()`：把 `httpx.MockTransport` 注入 `LlmClient`，**请求被短路到 handler 函数，不发任何真网络**：

```python
def _make_client(handler, **kwargs) -> LlmClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return LlmClient(base_url="https://api.example.com/v1", model="ai-mini",
                     api_key="test-key", client=http_client, **kwargs)
```

每个用例自带一个 `handler` 返回指定状态码（401/429/5xx/超时…），自给自足。

### 5.2 用例覆盖

| 分支 | 代表性用例 |
| --- | --- |
| 正常返回 | `test_chat_returns_text_model_elapsed` |
| 401/403 | `test_401_raises_llm_auth_error` |
| 429 重试 | `test_429_retries_then_raises` / `test_429_eventually_succeeds` |
| 5xx 重试 | `test_500_retries_then_raises` / `test_503_retries_then_succeeds` |
| 超时 | `test_connect_timeout_retries_then_raises` |
| Key 不泄露 | `test_api_key_never_logged` / `test_api_key_not_in_exception_message` |
| 上下文管理器 | `test_context_manager_closes_client` |

### 5.3 不依赖真实环境

把 `.env` 临时重命名为 `.env.bak` 再跑测试，结果仍是 **37 passed**——证明测试完全不读 `os.environ` / `.env`，不连任何真实端点。即使机器上没有 key、没有网络，测试也能跑通。

---

## 6. API调用（当前为学习/安全用的非真实 key）

### 6.1 `.env` 配置（已被 `.gitignore` 忽略，不入库）

```dotenv
API_KEY=<非真实 key，仅用于联调/避免泄露真实凭据>
API_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-chat
TIMEOUT_SECONDS=30
ENABLE_STREAM=false
```

### 6.2 调用路径

`load_dotenv()` → `load_config()`（Day 2 `AppConfig`）→ `LlmClient.chat()`。`api_key` 不传时，客户端默认从 `API_KEY` 环境变量读取。

> ⚠️ 注意：`load_dotenv()` 不传参时从**脚本所在目录**往上找 `.env`，不是从 CWD。脚本放在项目外（如 TEMP）必须显式 `load_dotenv(dotenv_path=Path("<项目根>/.env"))`。

### 6.3 当前状态

- 当前 `.env` 使用**非真实 key**，真实调用 `api.deepseek.com` 会返回 **401/403** → 抛 `LlmAuthError`（正好走通鉴权失败分支，验证错误分类正确）。
- 换成真实 key 且账号有余额 → 返回 **200** + 模型文本，`result.text` 即真实回复。
- 真实 key 但余额不足 → 返回 **402** → 抛 `LlmError`（402 是 4xx、不在重试白名单，不重试，符合设计）。

> 注：早期端到端验证曾用真实 key 打通管道（返回 402 余额不足），证明 key 有效、网络通、客户端正确处理响应。现改为非真实 key 仅为安全/学习考虑。

---

## 7. 测试与运行命令

### 7.1. 运行测试

完整命令，常用：
**每次新开 Git Bash 窗口，第一步先加 PATH（否则 `uv` 找不到）：**
```bash
export PATH="/c/Users/Xsz/.local/bin:$PATH"
```
然后进入项目根目录：
```bash
cd /d/workspace/py_ai/week01_ai_basics

```bash
# 跑全部测试（pytest 已配 testpaths=["tests"]，自动发现）
uv run pytest -v

# 只跑 Day 3 客户端测试
uv run pytest tests/test_llm_client.py

# 风格检查
uv run ruff check . 
```
![测试结果](./poho/llm_client.png)

### 7.2. 跑指定用例 / 关键字筛选

```bash
# 指定单个用例（文件名::函数名）
uv run pytest tests/test_llm_client.py::test_401_raises_llm_auth_error

# 指定多个文件
uv run pytest tests/test_config.py tests/test_llm_client.py

# 按关键字匹配用例名（例如只跑 auth 或 retry 相关）
uv run pytest -k "auth or retry"

# 只跑“返回文本/模型/耗时”那条典型用例
uv run pytest -v tests/test_llm_client.py::test_chat_returns_text_model_elapsed
```

PowerShell 需先 `Activate.ps1` 激活 venv（提示符显示 `(week01-ai-basics)`），再直接 `pytest`；Git Bash 用 `uv run pytest`。

---

## 8. 参考

- 项目源码：`src/llm_client.py`（Day 3）/`src/config.py`（Day 2）
- 测试：`tests/test_llm_client.py`
- 官方参考：Python asyncio；FastAPI 异步说明；Ollama API（可选本地模型）

