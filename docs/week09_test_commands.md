# 第九周服务测试命令

本文档汇总 agent-service 的全部测试方式。命令分三类：离线测试（不依赖外部服务）、在线测试（脚本自动起服务）、手工排查（curl 逐条打）。

所有命令默认在 **Git Bash** 中执行，工作目录 `D:\workspace\py_ai\week01_ai_basics`。

## 环境准备

Git Bash 需要先把 uv 加进 PATH：

```bash
export PATH="/c/Users/Xsz/.local/bin:$PATH"
cd /d/workspace/py_ai/week01_ai_basics
```

RAG 相关测试需要 Qdrant。启动命令（Git Bash，需 Docker Desktop 已运行）：

```bash
docker start qdrant_server
```

## 一、离线测试

不依赖 Qdrant、不占端口、不联网，用替身与内存向量库完成。

### 全量单元测试

```bash
# Git Bash
uv run pytest -q
```

预期：不低于 386 passed + 1 skipped。

### 只看服务测试

```bash
# Git Bash
uv run pytest tests/agent_service -q
```

### 看用例明细

```bash
# Git Bash
uv run pytest tests/agent_service -v
```

### 路由冒烟

用 ASGITransport 直打 app 对象，覆盖全部接口，不经过网络。

```bash
# Git Bash
uv run python scripts/service_week9_smoke.py
```

预期：输出 `冒烟全部通过`，退出码 0。

### 真实链路冒烟

按 .env 接真实 Ollama 与 Qdrant。依赖构造失败时自动回退替身并在输出里说明原因。

```bash
# Git Bash
uv run python scripts/service_week9_smoke.py --real
```

## 二、自动验收

`scripts/service_week9_acceptance.py` 自己拉起服务子进程、轮询就绪、按分组打接口、打印结果表、最后收掉进程。一条命令跑完全部在线测试。

### 全自动（推荐）

```bash
# Git Bash
uv run python scripts/service_week9_acceptance.py
```

脚本自动在 8080 端口起服务，跑完后关闭。

### 快速验收

跳过长链路用例（工作流 resume 在真实模型下单次可达数分钟）。

```bash
# Git Bash
uv run python scripts/service_week9_acceptance.py --no-heavy
```

### 服务已开着手动验收

```bash
# Git Bash
uv run python scripts/service_week9_acceptance.py --no-spawn --port 8080
```

### 只跑指定分组

```bash
# Git Bash
uv run python scripts/service_week9_acceptance.py --no-spawn --port 8080 --only probe,tools,envelope
```

分组可选值：`probe`、`rag`、`idempotency`、`stream`、`workflow`、`tools`、`envelope`。

### 参数说明

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | 127.0.0.1 | 绑定地址 |
| `--port` | 8080 | 绑定端口 |
| `--no-spawn` | 关 | 服务已开着，不重复启动 |
| `--only` | 全部 | 只跑指定分组，逗号分隔 |
| `--no-heavy` | 关 | 跳过 resume 等长链路用例 |
| `--client-timeout` | 900 | 客户端单请求超时秒数 |
| `--timeout-seconds` | 0 | 覆盖服务端请求超时，0 表示不覆盖 |

`--client-timeout` 与 `--timeout-seconds` 是两件事：前者是「客户端愿意等多久」，后者改的是服务端 `AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS`。机器慢就把客户端超时调大。

### 结果解读

| 标记 | 含义 |
| --- | --- |
| `[PASS]` | 断言通过 |
| `[FAIL]` | 断言失败，退出码 1 |
| `[SKIP]` | 前置条件不满足（如 Qdrant 未启动） |

退出码：0 全部通过，1 有失败项，2 服务未能启动。

## 三、手工排查

需要逐条看响应原文时用 curl。唯一的注意事项是**中文请求体**。

### 探针

```bash
# Git Bash
curl -s -w "\nHTTP %{http_code}\n" http://127.0.0.1:8080/health
curl -s -w "\nHTTP %{http_code}\n" http://127.0.0.1:8080/ready
```

`/health` 只答进程存活，依赖挂了仍是 200 且 `status=degraded`。`/ready` 逐依赖判定，必需依赖未就绪返回 503。

### RAG 问答

中文请求体必须用文件发送，不要用 `-d '...'` 内联。

```bash
# Git Bash
printf '%s' '{"question":"Qdrant 是什么数据库？","min_score":0.0}' > req.json
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/v1/rag/answer \
  -H 'Content-Type: application/json' \
  --data-binary @req.json
rm req.json
```

用 `--data-binary` 而不是 `-d`，且文件用 `printf` 写入（不带 BOM）。

若仍想确认编码问题，改用 Python 发送，绕开终端代码页：

```bash
# Git Bash
uv run python -c "
import httpx, json
r = httpx.post('http://127.0.0.1:8080/v1/rag/answer',
               json={'question': 'Qdrant 是什么数据库？', 'min_score': 0.0},
               timeout=180)
print('HTTP', r.status_code)
print(json.dumps(r.json(), ensure_ascii=False, indent=2))
"
```

### 流式 SSE

```bash
# Git Bash
printf '%s' '{"question":"Qdrant 是什么数据库？","min_score":0.0,"stream":true}' > req_stream.json
curl -s -N -X POST http://127.0.0.1:8080/v1/rag/answer \
  -H 'Content-Type: application/json' \
  --data-binary @req_stream.json
rm req_stream.json
```

`-N` 关闭 curl 缓冲，否则看不到逐帧效果。帧序为 `delta` 若干 → `citations` → `done`。

### 幂等回放

```bash
# Git Bash
printf '%s' '{"question":"Qdrant 是什么数据库？","min_score":0.0}' > req.json
curl -s -o /dev/null -D - -X POST http://127.0.0.1:8080/v1/rag/answer \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' \
  --data-binary @req.json | grep -i 'HTTP/\|Idempotency'
curl -s -o /dev/null -D - -X POST http://127.0.0.1:8080/v1/rag/answer \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' \
  --data-binary @req.json | grep -i 'HTTP/\|Idempotency'
rm req.json
```

第二次响应头多出 `Idempotency-Replayed: true`。

### 工作流

```bash
# Git Bash
printf '%s' '{"requirement_text":"开发一个电商网站，包含商品浏览、购物车与支付功能。"}' > wf1.json
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/v1/workflow/requirement-analysis \
  -H 'Content-Type: application/json' \
  --data-binary @wf1.json
rm wf1.json
```

续跑路径是 `/v1/workflow/{thread_id}/resume`，**不在** `requirement-analysis` 之下。

```bash
# Git Bash —— 把 <THREAD_ID> 换成上一步返回的值
printf '%s' '{"answers":["仅 Web 版，不做移动端"]}' > resume.json
curl -s -w "\nHTTP %{http_code}\n" \
  -X POST http://127.0.0.1:8080/v1/workflow/<THREAD_ID>/resume \
  -H 'Content-Type: application/json' \
  --data-binary @resume.json
rm resume.json
```

真实链路下工作流耗时长，需要先调大服务端超时：

```bash
# Git Bash
export AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS=600
uv run python scripts/serve.py --app src.agent_service.app:app --port 8080
```

### 工具接口

```bash
# Git Bash
curl -s -w "\nHTTP %{http_code}\n" http://127.0.0.1:8080/v1/tools
```

MCP 未启用返回 503 `dependency_unavailable`。启用方式：

```bash
# Git Bash
export AGENT_SERVICE_ENABLE_MCP=true
uv run python scripts/serve.py --app src.agent_service.app:app --port 8080
```

### 统一错误信封

```bash
# Git Bash
curl -s -w "\nHTTP %{http_code}\n" http://127.0.0.1:8080/no-such-path
```

## 四、状态码与错误码对照

| 状态码 | 错误码 | 触发场景 |
| --- | --- | --- |
| 400 | invalid_argument | 请求体不是合法 JSON |
| 422 | invalid_argument | 参数校验失败 |
| 404 | invalid_argument | 路径不存在 |
| 429 | rate_limited | 并发排队超时 |
| 503 | dependency_unavailable | 必需依赖不可用 |
| 504 | timeout | 请求超过服务端时限 |
| 500 | internal | 未预期异常 |

`detail` 字段的可读性：

| 状态码 | detail 内容 |
| --- | --- |
| 400 | 空。这是 Starlette 在 body 解析阶段抛出的错误，未进入参数校验 |
| 422 | `N validation error(s)` |
| 503 | 具体的依赖层名与原因 |
| 504 | `request_timeout=Ns` |

## 五、已知问题

### 400 报错且 detail 为空

现象：请求含中文的 body 时返回 400 `invalid_argument`，`message` 为 `There was an error parsing the body`，`detail` 为 `null`。

原因：body 在 JSON 解析阶段就失败，Starlette 抛 `HTTPException(400, "There was an error parsing the body")`，走的是 `StarletteHTTPException` 处理分支，该分支不填 `detail`。请求未进入 Pydantic 校验，因此不会是 422。

触发条件：Git Bash 终端代码页非 UTF-8 时，内联 `-d '{"question":"中文"}'` 送出的字节不是合法 UTF-8。

规避：按第三节的写法用 `printf` 写文件 + `--data-binary @file`，或改用 Python 发请求。

### 工作流 504 超时

现象：`resume` 接口返回 504，`detail` 为 `request_timeout=120.0s`。

原因：真实 qwen3 在 CPU 上单节点约 79 秒，7 个节点超过服务端 120 秒时限。实测同一请求连续三次的结果是 504、504、200，说明是耗时波动而非必然失败。

当前口径：RAG 问答与服务级超时同为 120 秒，工作流未单独放宽。

处理方向：超时按路由分级，工作流路由单独给更长预算（约 600 秒），配置拆成两个键。此项尚未实现。

临时规避：启动前设置 `AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS=600`。

### 需求分类结果不稳定

现象：`"开发一个电商网站，包含商品浏览、购物车与支付功能。"` 被分到 `other` 类且置信度仅 0.30，而 `"帮我做个东西"` 反而拿到了完整的 7 节点报告。

影响：分类结果决定后续节点走向，误分类会使报告内容与需求不匹配。

状态：未定位根因，待排查。

## 六、命令速查

| 用途 | 命令 | 依赖 |
| --- | --- | --- |
| 全量单测 | `uv run pytest -q` | 无 |
| 服务单测 | `uv run pytest tests/agent_service -q` | 无 |
| 路由冒烟 | `uv run python scripts/service_week9_smoke.py` | 无 |
| 真实冒烟 | `uv run python scripts/service_week9_smoke.py --real` | Ollama + Qdrant |
| 自动验收 | `uv run python scripts/service_week9_acceptance.py` | Qdrant |
| 快速验收 | `uv run python scripts/service_week9_acceptance.py --no-heavy` | Qdrant |
| 起服务 | `uv run python scripts/serve.py --app src.agent_service.app:app --port 8080` | 无 |
