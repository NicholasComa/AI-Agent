# 第九周服务测试命令

命令按**服务如何启动**分组：

- 第一节 不需要服务（离线测试）
- 第二节 用 `uv run python scripts/serve.py` 启动，配合 curl 手工测
- 第三节 用 `docker compose up -d --build` 启动，配合 curl 手工测
- 第四节 一条命令跑完的测试脚本（脚本自己起服务）
- 第五节 对照表与已知问题

所有命令在 **Git Bash** 执行，工作目录 `D:\workspace\py_ai\week01_ai_basics`。

```bash
# Git Bash —— 先加 PATH 并进目录
export PATH="/c/Users/Xsz/.local/bin:$PATH"
cd /d/workspace/py_ai/week01_ai_basics
```

两个必须记住的点：

1. **curl 一律加 `--noproxy '*'`**。本机 `HTTP_PROXY` 指向本地代理，不加会把发往
   `127.0.0.1` 的请求交给代理，得到 `HTTP 000` 或 502。仓库内脚本已在 `httpx` 上设
   `trust_env=False`，脚本调用无需处理。
2. **中文请求体用 `printf` 写文件 + `--data-binary @file`**，不要内联 `-d '..."中文"...'`。
   终端代码页非 UTF-8 时内联会送出非法字节，服务端在解析阶段返回 400（`detail` 为 null）。

---

## 一、不需要服务

### 全量单元测试

```bash
# Git Bash
uv run pytest -q
```

预期：`462 passed, 1 skipped`（skip 是符号链接用例，环境限制，与本周改动无关）。

### 只看服务测试

```bash
# Git Bash
uv run pytest tests/agent_service -q      # 预期 88 passed
```

### 路由冒烟（不占端口、不联网）

用 ASGITransport 直打 app 对象，覆盖全部接口。

```bash
# Git Bash
uv run python scripts/service_week9_smoke.py           # 预期输出「冒烟全部通过」
uv run python scripts/service_week9_smoke.py --real    # 接真实 Ollama + Qdrant
```

### 并发与超时（不需要 Qdrant，超时组不需要服务）

```bash
# Git Bash
# 两档并发 + 超时组，共 13 项断言
uv run python scripts/service_week9_load.py --levels 20,50 --requests 40 --min-score 1.0

# 只跑超时组，最快（5 项断言）
uv run python scripts/service_week9_load.py --only timeout
```

`--min-score 1.0` 让 RAG 走低分拒答分支、不调模型，只压检索与并发闸门；默认 `0.0`
会走完整链路，本机 CPU 上一次数十秒。

预期：

```
[PASS] 20 并发 · 无 5xx
[PASS] 20 并发 · 限流只出现在超配额场景
[PASS] 504 timeout          detail=request_timeout=2.0s
工程测试通过：13 项断言全部通过
```

---

## 二、先起服务（本机直跑），再手工 curl

### 启动

```bash
# Git Bash
uv run python scripts/serve.py --app src.agent_service.app:app --port 8080
```

RAG 相关接口需要 Qdrant（另开一个窗口）：

```bash
# Git Bash
docker start qdrant_server
```

按需在启动前加环境变量覆盖配置：

```bash
# Git Bash
export AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS=600   # 工作流长链路
export AGENT_SERVICE_ENABLE_MCP=true               # 启用 MCP 工具
export AGENT_SERVICE_API_KEY=demo-key              # 开启认证
uv run python scripts/serve.py --app src.agent_service.app:app --port 8080
```

### 探针

```bash
# Git Bash
curl -s -w "\nHTTP %{http_code}\n" --noproxy '*' http://127.0.0.1:8080/health
curl -s -w "\nHTTP %{http_code}\n" --noproxy '*' http://127.0.0.1:8080/ready
curl -s --noproxy '*' http://127.0.0.1:8080/metrics-summary | python -m json.tool
```

`/health` 只答进程存活，依赖挂了仍 200 且 `status=degraded`。`/ready` 逐依赖判定，
必需依赖未就绪返回 503（设计行为，非故障）。三者都不参与认证。

### RAG 问答

```bash
# Git Bash
printf '%s' '{"question":"Qdrant 是什么数据库？","min_score":0.0}' > req.json
curl -s -w "\nHTTP %{http_code}\n" --noproxy '*' \
  -X POST http://127.0.0.1:8080/v1/rag/answer \
  -H 'Content-Type: application/json' \
  --data-binary @req.json
rm req.json
```

预期 200，body 含 `has_answer` 与 `citations`。本机真实模型单次约 50 秒。

### 流式 SSE

```bash
# Git Bash
printf '%s' '{"question":"Qdrant 是什么数据库？","min_score":0.0,"stream":true}' > req.json
curl -s -N --noproxy '*' -X POST http://127.0.0.1:8080/v1/rag/answer \
  -H 'Content-Type: application/json' \
  --data-binary @req.json
rm req.json
```

`-N` 关缓冲，否则看不到逐帧。帧序：`delta` 若干 → `citations` → `done`。

### 幂等回放

```bash
# Git Bash
printf '%s' '{"question":"Qdrant 是什么数据库？","min_score":0.0}' > req.json
for i in 1 2; do
  curl -s -o /dev/null -D - --noproxy '*' -X POST http://127.0.0.1:8080/v1/rag/answer \
    -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
    --data-binary @req.json | grep -i 'HTTP/\|Idempotency'
done
rm req.json
```

第二次响应头多出 `Idempotency-Replayed: true`。

### 工作流

```bash
# Git Bash
printf '%s' '{"requirement_text":"开发一个电商网站，包含商品浏览、购物车与支付功能。"}' > wf.json
curl -s -w "\nHTTP %{http_code}\n" --noproxy '*' \
  -X POST http://127.0.0.1:8080/v1/workflow/requirement-analysis \
  -H 'Content-Type: application/json' \
  --data-binary @wf.json
rm wf.json
```

续跑路径是 `/v1/workflow/{thread_id}/resume`，**不在** `requirement-analysis` 之下。
把 `<THREAD_ID>` 换成上一步返回值：

```bash
# Git Bash
printf '%s' '{"answers":["仅 Web 版，不做移动端"]}' > resume.json
curl -s -w "\nHTTP %{http_code}\n" --noproxy '*' \
  -X POST http://127.0.0.1:8080/v1/workflow/<THREAD_ID>/resume \
  -H 'Content-Type: application/json' \
  --data-binary @resume.json
rm resume.json
```

本机真实模型下工作流常返回 **504**（见第五节），需要先在启动前 `export
AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS=600`。

### 工具接口

```bash
# Git Bash
curl -s -w "\nHTTP %{http_code}\n" --noproxy '*' http://127.0.0.1:8080/v1/tools
```

MCP 未启用时返回 503 `dependency_unavailable`。启用见本节启动部分。

### 错误信封

```bash
# Git Bash
curl -s -w "\nHTTP %{http_code}\n" --noproxy '*' http://127.0.0.1:8080/no-such-path
```

预期 404，body 为统一信封 `{"error": {"code": "invalid_argument", ...}}`。

---

## 三、先起服务（Docker Compose），再手工 curl

### 启动

compose 的 `qdrant` 与宿主机常驻的 `qdrant_server` 抢 **6333**，必须先让出：

```bash
# Git Bash
docker stop qdrant_server        # 让出 6333
docker compose up -d --build     # 一键起 api + qdrant；代码变更后必须 rebuild
```

等两个服务 healthy（约 10-20 秒）：

```bash
# Git Bash
docker compose ps
curl -s --noproxy '*' http://127.0.0.1:8080/health | python -m json.tool
```

首次需要在容器内灌库（否则 RAG 召回为空）：

```bash
# Git Bash
docker compose exec api python scripts/rag_week6_ingest.py \
    --ingest data/raw --collection jwipc_v3 --chunk-size 800 --overlap 80 --rebuild
```

### 手工 curl

第二节的所有 curl 命令**原样可用**（端口都是 8080），只需注意两点：

- 容器配置与宿主机不同：容器内只有 `AGENT_SERVICE_SESSION_DIR`，**密钥 / 体积上限 /
  配额都是缺省值**（无密钥、262144、60）。想改就写进 `compose.yaml` 的 `environment` 再
  `docker compose up -d`。
- 容器连宿主机模型服务靠 `API_BASE_URL=http://host.docker.internal:11434/v1`，
  需宿主机 Ollama 监听 `0.0.0.0:11434`。

### 查看日志

```bash
# Git Bash
docker compose logs -f api
docker compose logs --no-color api > logs/service_week9.log   # 导出交付物
```

### 收尾（顺序不能反）

```bash
# Git Bash
docker compose down          # 释放 6333 与 8080，不删卷（数据仍在）
docker start qdrant_server   # 还原本机常驻 Qdrant
```

先 `start` 会报 `Bind for 0.0.0.0:6333 failed: port is already allocated`。

### 容器重启与持久化（自动）

```bash
# Git Bash —— 会自行 up 并等待就绪，结束时栈仍在跑，需自己 down
uv run python scripts/service_week9_compose_check.py

# 栈已在跑，跳过 up 直接测
uv run python scripts/service_week9_compose_check.py --skip-up
```

预期 12 项断言全过（含 `points_count=51` 前后一致、`down`/`up` 后仍能召回）。

---

## 四、一键测试脚本（脚本自己起服务）

这三个脚本**自建自收**服务子进程，跑之前请先 `docker compose down`（否则会报端口占用）。

### 接口验收

```bash
# Git Bash —— 8 分组，全自动
uv run python scripts/service_week9_acceptance.py

# 跳过长链路（工作流 resume 真实模型下可达数分钟）
uv run python scripts/service_week9_acceptance.py --no-heavy

# 只跑指定分组
uv run python scripts/service_week9_acceptance.py --only probe,rag,tools

# 入站防护专项（401 / 413 / 429）
uv run python scripts/service_week9_acceptance.py \
    --only security --api-key s3cret --rate-limit 3 --max-body-bytes 4096
```

分组可选值：`probe`、`rag`、`idempotency`、`stream`、`workflow`、`tools`、`envelope`、`security`。

预期：`probe`/`rag`/`envelope`/`stream`/`tools` 全 PASS；`workflow` 三项因真实模型超时
FAIL（已知问题，第五节）；`security` 未传 `--api-key` 时记 2 PASS + 3 SKIP。

`security` 会打满限流配额，固定排在最后执行，**不要单独先跑它再跑别的分组**，
否则后续分组会全被限流成 429。

### 被测服务已开着（不重启）

```bash
# Git Bash
uv run python scripts/service_week9_acceptance.py --no-spawn --port 8080 --only probe,tools
```

`--no-spawn` 时脚本无法控制目标配置，故传了 `--api-key` / `--max-body-bytes` 时
会先探一下认证口径，不一致直接给出「很可能连到了缺省配置的旧服务或容器」的结论。

### 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--host` / `--port` | 127.0.0.1 / 8080 | 绑定地址与端口 |
| `--no-spawn` | 关 | 服务已开着，不重复启动 |
| `--only` | 全部 | 只跑指定分组，逗号分隔 |
| `--no-heavy` | 关 | 跳过 resume 等长链路用例 |
| `--client-timeout` | 900 | 客户端单请求超时秒数 |
| `--timeout-seconds` | 0 | 覆盖服务端 `AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS`，0 表示不覆盖 |
| `--api-key` | 空 | 传 `AGENT_SERVICE_API_KEY`（既作请求头，也注入子进程） |
| `--max-body-bytes` | 空 | 覆盖 `AGENT_SERVICE_MAX_BODY_BYTES`，用于 413 用例 |
| `--rate-limit` | 空 | 覆盖 `AGENT_SERVICE_RATE_LIMIT_PER_MINUTE`，用于 429 用例 |

`--client-timeout` 是「客户端愿意等多久」，`--timeout-seconds` 改的是服务端时限，两者独立。

### 结果解读

| 标记 | 含义 |
| --- | --- |
| `[PASS]` | 断言通过 |
| `[FAIL]` | 断言失败，退出码 1 |
| `[SKIP]` | 前置条件不满足（如未传 `--api-key`） |

退出码：`0` 全部通过，`1` 有失败项，`2` 服务未能启动（含端口被占用）。

---

## 五、对照表与已知问题

### 状态码

| 状态码 | 错误码 | 触发场景 |
| --- | --- | --- |
| 400 | invalid_argument | 请求体不是合法 JSON（`detail` 为空，见下） |
| 401 | unauthorized | 未带或带错 Bearer token（仅 `AGENT_SERVICE_API_KEY` 非空时生效） |
| 404 / 405 | invalid_argument | 路径不存在 / 方法不匹配 |
| 413 | payload_too_large | 超过 `AGENT_SERVICE_MAX_BODY_BYTES` |
| 422 | invalid_argument | 参数校验失败，`detail` 为 `N validation error(s)` |
| 429 | rate_limited | 超过每分钟配额或并发排队超时；带 `Retry-After` 头 |
| 499 | cancelled | 流式连接被客户端断开 |
| 500 | internal | 未预期异常 |
| 503 | dependency_unavailable | 必需依赖不可用，`detail` 指名依赖层 |
| 504 | timeout | 超过服务端时限，`detail` 为 `request_timeout=Ns` |

### 三个已知问题

**1. 400 且 `detail` 为空**：请求含中文 body 时返回 400，`message` 为 `There was an
error parsing the body`。原因是 Starlette 在 body 解析阶段就抛错，未进入 Pydantic 校验，
故不是 422。触发条件是终端代码页非 UTF-8 时内联 `-d '..."中文"...'`。按本文档第一段
的写法（`printf` 写文件 + `--data-binary`）即可规避。

**2. 工作流 504**：`detail=request_timeout=120.0s`。真实 qwen3 在 CPU 上单节点约 79 秒，
7 节点超过 120 秒时限；实测同一请求连续三次为 504、504、200，是耗时波动而非必然失败。
临时规避：启动前 `export AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS=600`。
根治方向是按路由分级超时（工作流约 600 秒），尚未实现。

**3. 需求分类不稳定**：`"开发一个电商网站，包含商品浏览、购物车与支付功能。"` 曾被分到
`other` 且置信度 0.30。近期 `tests/agent_service/test_service_workflow.py` 实跑显示真实
模型对该需求给出 `category=web`、`conf=0.85~0.92`，故重测前先确认该问题是否仍存在。

### 速查

| 用途 | 命令 | 依赖 |
| --- | --- | --- |
| 全量单测 | `uv run pytest -q` | 无 |
| 服务单测 | `uv run pytest tests/agent_service -q` | 无 |
| 路由冒烟 | `uv run python scripts/service_week9_smoke.py` | 无 |
| 真实冒烟 | `uv run python scripts/service_week9_smoke.py --real` | Ollama + Qdrant |
| 并发 + 超时 | `uv run python scripts/service_week9_load.py --levels 20,50 --requests 40 --min-score 1.0` | 无 |
| 只跑超时组 | `uv run python scripts/service_week9_load.py --only timeout` | 无 |
| 接口验收 | `uv run python scripts/service_week9_acceptance.py` | Qdrant |
| 安全验收 | `uv run python scripts/service_week9_acceptance.py --only security --api-key secret` | Qdrant |
| 重启 + 持久化 | `uv run python scripts/service_week9_compose_check.py --skip-up` | Docker |
| 容器前置检查 | `uv run python scripts/service_week9_compose_check.py --check-only` | Docker |
| 本机起服务 | `uv run python scripts/serve.py --app src.agent_service.app:app --port 8080` | 无 |
| 容器起服务 | `docker compose up -d --build` | Docker |
