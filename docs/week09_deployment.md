# Week 9 Agent 服务部署手册

> 覆盖本机（uv 直跑）与容器（Docker Compose）两套流程的前置条件、配置、启动、验证与排障。
> 所有命令均标注运行环境；未标注者默认在 **Git Bash**（Windows，MINGW64）执行。

---

## 1. 两套运行方式如何选择

| | 本机直跑 | 容器（Docker Compose） |
| --- | --- | --- |
| 启动命令 | `uv run python scripts/serve.py` | `docker compose up -d` |
| 适用场景 | 改代码后立刻验证、打断点调试 | 验证可分发性与卷持久化、对齐生产形态 |
| Qdrant 来源 | 本机 `qdrant_server` 容器（6333） | compose 内的 `qdrant` 服务（也发布 6333） |
| 模型服务来源 | `localhost:11434`（Ollama） | `host.docker.internal:11434`（经网关映射） |
| 代码生效方式 | 保存即生效 | **需要 `docker compose build api` 重建** |
| 会话数据位置 | 宿主机 `data/agent_service_sessions/` | 具名卷 `agent_service_sessions` |

两套方式的端口都是 8080，**不能同时运行**。

---

## 2. 前置条件

### 2.1 本机直跑

```bash
# Git Bash：确认 Python 环境与依赖（首次或 pyproject.toml 变更后执行）
cd /d/workspace/py_ai/week01_ai_basics
uv sync
```

需要的外部依赖：

```bash
# Git Bash：Ollama 在跑，且模型齐备（qwen3 生成、mxbai-embed-large 嵌入）
ollama list

# Git Bash：Qdrant 在跑（本机非容器工作流的常驻容器）
docker ps --filter name=qdrant_server
```

### 2.2 容器方式

```bash
# Git Bash：Docker 可用
docker version
docker compose version
```

本机 Docker **无法直连 `registry-1.docker.io`**。若拉取镜像失败，改用可用镜像源：

```bash
# Git Bash：从可用源拉取后改成本地名
docker pull docker.m.daocloud.io/library/python:3.12-slim
docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim
docker pull ghcr.io/astral-sh/uv:0.11.30
```

可用源：`docker.m.daocloud.io`、`docker.1panel.live`、`ghcr.io`。
不可用：`hub-mirror.c.163.com`、`dockerproxy.cn`。

---

## 3. 配置键全表

### 3.1 服务级配置（`AGENT_SERVICE_` 前缀）

来源优先级：**进程环境变量 > 项目根 `.env` > 字段默认值**。大小写不敏感。

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `AGENT_SERVICE_API_KEY` | `""`（不启用认证） | Bearer 认证密钥；非空即强制校验 |
| `AGENT_SERVICE_MAX_CONCURRENCY` | `8` | 服务级并发上限，超出在此排队 |
| `AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS` | `5.0` | 排队最长等待；超时返回 `429` + `rate_limited` |
| `AGENT_SERVICE_RATE_LIMIT_PER_MINUTE` | `60` | 单客户端每分钟配额 |
| `AGENT_SERVICE_MAX_BODY_BYTES` | `262144` | 请求体字节上限；超出 `413` + `payload_too_large` |
| `AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS` | `120.0` | 单请求总时限；超时返回 `504` + `timeout` |
| `AGENT_SERVICE_SESSION_DIR` | `data/agent_service_sessions` | 会话与幂等缓存目录（容器内挂卷） |
| `AGENT_SERVICE_ENABLE_MCP` | `false` | 是否启动时建立 MCP 会话 |
| `AGENT_SERVICE_MCP_SERVER_SCRIPT` | `scripts/mcp_week7_server.py` | stdio 传输拉起的 Server 脚本 |
| `AGENT_SERVICE_COLLECTION_NAME` | `jwipc_v3` | 知识库集合名 |

### 3.2 基础设施配置（沿用项目既有键）

| 键 | `.env` 本机取值 | 容器内覆盖值 | 说明 |
| --- | --- | --- | --- |
| `API_BASE_URL` | `http://localhost:11434/v1` | `http://host.docker.internal:11434/v1` | 生成模型接口 |
| `EMBEDDING_API_BASE_URL` | `http://localhost:11434/v1` | `http://host.docker.internal:11434/v1` | 嵌入模型接口 |
| `MODEL_NAME` | `qwen3` | 同左 | 生成模型 |
| `EMBEDDING_MODEL_NAME` | `mxbai-embed-large` | 同左 | 嵌入模型 |
| `QDRANT_HOST` | `localhost` | `qdrant` | 容器内必须用服务名 |
| `QDRANT_PORT` | `6333` | `6333` | |
| `QDRANT_MODE` | `docker` | `docker` | |
| `MCP_SANDBOX_ROOT` | `data/mcp_sandbox` | `/app/data/mcp_sandbox` | |

**两个容易出错的点**：

1. 项目代码里**没有任何地方读取 `OLLAMA_BASE_URL`**，覆盖它不会生效。容器连宿主机模型服务必须覆盖 `API_BASE_URL` 与 `EMBEDDING_API_BASE_URL`。
2. 服务读的是 `AGENT_SERVICE_COLLECTION_NAME`（缺省 `jwipc_v3`），**不读** `.env` 里的 `QDRANT_COLLECTION_NAME`（其为 `rag_chunks`）。灌库与查询必须指同一个集合。

---

## 4. 本机直跑流程

```bash
# Git Bash —— 1) 灌库（首次或语料变更后执行；--ingest 为必填参数）
cd /d/workspace/py_ai/week01_ai_basics
uv run python scripts/rag_week6_ingest.py --ingest data/raw --collection jwipc_v3 \
    --chunk-size 800 --overlap 80 --rebuild
```

首次灌库约 0.85 秒/片段，51 个片段约需 45 秒。

```bash
# Git Bash —— 2) 启动服务（前台运行，Ctrl+C 停止）
uv run python scripts/serve.py --app src.agent_service.app:app --host 127.0.0.1 --port 8080
```

```bash
# Git Bash —— 3) 另开终端验证
curl -s http://127.0.0.1:8080/health   --noproxy '*'
curl -s http://127.0.0.1:8080/ready    --noproxy '*' | python -m json.tool
curl -s http://127.0.0.1:8080/metrics-summary --noproxy '*' | python -m json.tool
```

`--noproxy '*'` 在本机是必需的：环境中存在 `HTTP_PROXY/HTTPS_PROXY` 指向本地代理
（`127.0.0.1:57348`），不绕过会把发往本机的请求交给代理，得到 502 或连接重置。

---

## 5. 容器流程

### 5.1 启停

```bash
# Git Bash —— 启动（首次或代码变更后加 --build）
cd /d/workspace/py_ai/week01_ai_basics
docker compose up -d --build

# Git Bash —— 查看状态
docker compose ps

# Git Bash —— 停止（保留卷，数据不丢）
docker compose down

# Git Bash —— 停止并删卷（会清空知识库与会话，仅调试用）
docker compose down -v
```

### 5.2 端口冲突：必须先停本机常驻的 Qdrant

本机常驻一个名为 `qdrant_server` 的容器占用 6333，compose 的 `qdrant` 服务也发布该端口。
两者数据在**不同的卷**上（`qdrant_data` vs `week01_ai_basics_qdrant_data`），互不影响。
但端口只有一个，因此：

```bash
# Git Bash —— up 之前：让出 6333
docker stop qdrant_server

# ...跑测试...

# Git Bash —— 用完还原：必须先 down 再 start，顺序不能反
docker compose down          # 释放 6333 与 8080（不加 -v 故不删卷）
docker start qdrant_server
```

顺序反了会失败：

```
Error response from daemon: failed to set up container networking:
Bind for 0.0.0.0:6333 failed: port is already allocated
```

原因就是 compose 的 `qdrant` 容器仍占着 6333。测试脚本跑完**不会**替你停栈
（持久化组需要栈保持运行），所以 `docker compose down` 必须自己执行。

同理，8080 也只有一个：compose 的 `api` 容器发布 8080，而本机直跑的服务同样
用 8080。**compose 栈在跑时不要在本机再起一个服务**，两者会抢端口。

```bash
# Git Bash —— 确认 8080 现在归谁
netstat -ano | grep ":8080"
docker ps --format '{{.Names}}\t{{.Ports}}'

# Git Bash —— 想切回本机直跑，先把容器收掉
docker compose down
```

`/health` 只能证明「这个端口上有服务在答」，**不能**证明「答的是我刚起的那个
进程」。验收脚本因此会在自建子进程之前检查端口占用，被占用时报错退出（退出码 2）
并指明占用者，避免整轮断言打在容器的缺省配置上。

### 5.3 容器内灌库

```bash
# Git Bash：容器内执行，注意 Python 解释器路径与集合名
docker compose exec api python scripts/rag_week6_ingest.py \
    --ingest data/raw --collection jwipc_v3 --chunk-size 800 --overlap 80 --rebuild
```

### 5.4 查看日志

```bash
# Git Bash：跟踪日志
docker compose logs -f api

# Git Bash：导出为交付物（周任务要求）
docker compose logs --no-color api > logs/service_week9.log
```

---

## 6. 探针口径

三个探针分工明确，回答三个不同的问题。

| 接口 | 回答的问题 | 状态码行为 | 是否认证 |
| --- | --- | --- | --- |
| `GET /health` | 进程还活着吗？ | **恒为 200**，依赖全挂也只把 body 的 `status` 置 `degraded` | 免认证 |
| `GET /ready` | 能接业务流量吗？ | 必需依赖未就绪时 **503**，body 仍完整 | 免认证 |
| `GET /metrics-summary` | 这段时间跑得怎么样？ | 恒为 200 | 免认证 |

设计意图：存活探针若绑上依赖状态，编排系统会去重启一个本身没坏的进程。
`/ready` 返回 503 是**设计行为**，不是故障。

### 6.1 必需与可选依赖

必需：`session_store`、`config`、`qdrant`、`llm`、`workflow`。
可选：`mcp`（未启用时 `ready=false, required=false`，不影响 `/ready` 判 200）。

`/ready` 的 `dependencies` 按固定顺序排列：
`session_store, config, qdrant, llm, mcp, workflow`。

### 6.2 `/ready` 顺带刷新的两个数字

- `qdrant` 明细追加 `points_count=N`，是判断「卷是否还在」最直接的证据；
- `mcp` 明细追加 `tools=N`，工具数为 0 说明子进程起来了却没注册成功。

两者属于**观测**而非**判定**：刷新失败只改明细文字，不把依赖翻成未就绪。
明细用 `split(" points_count=")[0]` 保留基准，因此反复调用不会让字符串无限增长。

### 6.3 `/metrics-summary` 字段口径

| 字段 | 口径 |
| --- | --- |
| `requests_total` | `"路由\|状态码"` 到次数的映射，保留最细粒度 |
| `requests_by_route` | 按**归一化**路由聚合；`/v1/tools/<name>/call` 折叠为 `/v1/tools/:name/call` |
| `requests_by_status` | 按状态码聚合 |
| `in_flight` | 采样瞬间仍在处理中的请求数 |
| `latency_ms` | `avg` / `p95` / `samples`；p95 返回**桶上界**（保守，不低估） |
| `llm_calls` | 实际调用次数（含拒答重试），非请求次数 |
| `rag_queries` / `rag_hit_rate` | 命中判定为「召回非空且 Top1 分数达到 `min_score`」，与拒答口径一致 |
| `errors_by_code` | 统一错误码到次数的映射 |
| `idempotency_replays` | 幂等缓存命中次数，即客户端重试率的观测 |

**探针不计入指标**（`/health`、`/ready`、`/metrics-summary` 被排除）。容器的
Healthcheck 是 30 秒一次的，若计入总量会把业务流量稀释成噪声。

计数随进程重启归零，反映的是「当前这个实例现在怎么样」，历史趋势由第 10 周的
Trace 与评测链路承担。

---

## 7. 端口与卷

| 项 | 值 | 说明 |
| --- | --- | --- |
| 宿主端口 | `8080` | API |
| 宿主端口 | `6333` | Qdrant HTTP |
| 卷 `qdrant_data` | `/qdrant/storage` | 知识库数据；`down` 不带 `-v` 即保留 |
| 卷 `agent_service_sessions` | `/app/data/agent_service_sessions` | 会话与幂等缓存 |
| 绑定挂载 | `./data/raw → /app/data/raw:ro` | 语料只读挂载，**不烘进镜像** |
| 绑定挂载 | `./logs → /app/logs` | 日志 |

语料只读挂载而非 COPY 进镜像的原因：`data/raw/` 含内部产品手册（S102H 约 40MB、
VT1000 等），烘进镜像会让镜像无法对外分发。

**镜像不自动同步宿主机代码**：容器跑的是构建时的快照。改完 `src/` 后必须

```bash
# Git Bash
docker compose build api && docker compose up -d
```

否则会出现「代码明明改了、容器行为没变」的假象。

---

## 8. 常见故障与对应命令

### 8.1 `/ready` 返回 503

```bash
# Git Bash：先从 body 定位是哪一层，不要猜
curl -s http://127.0.0.1:8080/ready --noproxy '*' | python -m json.tool
```

| `dependencies` 里未就绪的项 | 根因 | 处置 |
| --- | --- | --- |
| `qdrant` | 集合不存在 / Qdrant 没起 / 容器内 `QDRANT_HOST` 仍指向 localhost | 确认 Qdrant 在跑；容器内必须 `QDRANT_HOST=qdrant` |
| `qdrant` 的 `points_count=0` | 忘了灌库 | 执行 5.3 的灌库命令 |
| `llm` | 模型接口不通 | 本机确认 `ollama list`；容器确认 `host.docker.internal` 可达 |
| `session_store` | 目录不可写（容器内非 root 用户） | 确认 Dockerfile 里 `mkdir -p` + `chown` 已执行；重建镜像 |
| `workflow` | 图构建失败（通常因 `llm` 或 `rag` 缺席） | 先修上游依赖 |

若 `qdrant` 的 `detail` 是 `WinError 10061`（由于目标计算机积极拒绝，无法连接），
说明宿主机那个端口上根本没有监听者，先按 8.7 确认容器端口是否**真的建立**了。
另外注意：**`/ready` 的就绪结论是启动时定下并缓存的**，依赖修好后必须重启服务进程，
否则会一直返回同一条旧错误，详见 8.8。

### 8.2 容器内连不上宿主机模型服务

```bash
# Git Bash：从容器内验证网关可达
docker compose exec api python -c "
import urllib.request
print(urllib.request.urlopen('http://host.docker.internal:11434/v1/models', timeout=5).status)
"
```

返回 200 说明网关通；连接失败则检查 `compose.yaml` 的 `extra_hosts` 是否包含
`host.docker.internal:host-gateway`。

### 8.3 请求返回 `502` / `WinError 10054`

本机环境的 `HTTP_PROXY` / `HTTPS_PROXY` 把发往 `127.0.0.1` 的请求交给了代理。

- curl：加 `--noproxy '*'`；
- Python 脚本：`httpx.Client(..., trust_env=False)`。

仓库内三个服务脚本（`service_week9_acceptance.py` / `service_week9_load.py` /
`service_week9_compose_check.py`）均已显式设置 `trust_env=False`。

### 8.4 请求返回 `429` / `503` / `413`

| 状态码 | `error.code` | 触发条件 |
| --- | --- | --- |
| `401` | `unauthorized` | 未带或带错 Bearer token |
| `413` | `payload_too_large` | 请求体超过 `MAX_BODY_BYTES` |
| `429` | `rate_limited` | 超过每分钟配额，或并发闸门排队超时 |
| `503` | `dependency_unavailable` | 必需依赖未就绪 |
| `504` | `timeout` | 总时限 `REQUEST_TIMEOUT_SECONDS` 耗尽 |

**`429` 与 `504` 的区别是排障关键**：`429` 表示请求根本没进去（排队失败），
`504` 表示进去了但没跑完。前者要扩配额或降并发，后者要看单次耗时。

`401` 不会凭空出现：只要启动服务前 `export AGENT_SERVICE_API_KEY=<非空值>`，认证即开启，
此后除 `/health`、`/ready`、`/metrics-summary` 三个探针外，所有接口都必须带
`Authorization: Bearer <该值>`。密钥为空时认证依赖直接放行，此时带上该头也没有副作用，
所以调用示例里统一带上它是安全的。

### 8.5 一次请求耗时过长（本机真实模型）

`qwen3` 在 CPU 上单节点约 79 秒，工作流 7 节点在 120 秒总时限内必然 `504`。
这是**已知未解决项**，处置方式见 `docs/week09_test_report.md` 第 6 节。

### 8.6 验收脚本退出码 2，提示端口被占用

```
[FAIL] 端口 127.0.0.1:8080 已被占用（com.docker.backend.exe(pid=32572)）。
       请先释放：容器占用的执行 `docker compose down`，...
```

说明 compose 的 `api` 容器还在发布 8080。**不要**直接加 `--no-spawn` 绕过——
容器里的配置是缺省的（无密钥、上限 262144、配额 60），会让认证 / 413 / 429
三条断言给出与实际原因无关的结论。正确做法是二选一：

```bash
# Git Bash：方案 A —— 收掉容器，让脚本照常自建服务
docker compose down
uv run python scripts/service_week9_acceptance.py

# Git Bash：方案 B —— 就想测这个容器，则换端口起本机服务
uv run python scripts/service_week9_acceptance.py --port 8081
```

想确认某个端口上跑的到底是不是自己期望的配置，可以看 `/ready` 的 `config`
明细，或按 3.1 的表逐键核对。

### 8.7 容器显示 `Up`，但宿主机连不上它的端口

症状：`/ready` 的 `qdrant` 明细或直接 `curl` 报 `WinError 10061`（由于目标计算机积极拒绝，
无法连接），而 `docker ps` 显示容器是 `Up`、Docker Desktop 里也是绿点，容易误判成「服务起不来」。

**先分清两处端口配置，判据在这里**：

```bash
# Git Bash —— 期望 vs 实际
docker inspect qdrant_server \
  --format '期望 HostConfig={{json .HostConfig.PortBindings}} 实际 NetworkSettings.Ports={{json .NetworkSettings.Ports}}'
```

- `HostConfig.PortBindings` 是**创建容器时要求的**映射（意图）；
- `NetworkSettings.Ports` 是 Docker **实际建立的**映射（事实）；
- `docker ps` 的 `PORTS` 列读的是后者。所以「只显示 `6333-6334/tcp` 而没有
  `0.0.0.0:6333->6333/tcp` 箭头」就等于**实际没建立**，哪怕 `HostConfig` 里明明写着要绑 6333。

成因是**一次失败的端口绑定被永久保留**：建立映射的那一刻端口被别的容器占着
（报 `Bind for 0.0.0.0:6333 failed: port is already allocated`），Docker 绑定失败，
但**容器照样被启动，只是端口没建立**；此后 `docker start` 对「已在运行」的容器是空操作，
不会重试绑定，于是长期停在「跑着却连不上」的状态。

三步确认：

```bash
# Git Bash
docker inspect qdrant_server --format '{{json .NetworkSettings.Ports}}'    # 空 {} 即未建立
netstat -ano | grep -E ':(6333)\s' | grep LISTENING                        # 宿主机有没有人听
curl -sS --noproxy '*' http://127.0.0.1:6333/collections; echo " exit=$?"  # exit=7 即连接被拒
```

处置是**重建容器强制重新绑定**，并复用原卷以保住数据：

```bash
# Git Bash —— 先查卷名（本项目是 qdrant_data）
docker inspect qdrant_server --format '{{json .Mounts}}'

docker stop qdrant_server
docker rename qdrant_server qdrant_server_bak      # 留后路，确认无误再 docker rm
docker run -d --name qdrant_server -p 6333:6333 -p 6334:6334 \
  -v qdrant_data:/qdrant/storage qdrant/qdrant
```

**备份容器与新建容器共用同一个卷，不要再启动它** —— 两个 Qdrant 进程同时写一个卷会损坏数据。

### 8.8 依赖已经修好，`/ready` 仍然返回 503

`/ready` 的就绪结论是**启动时定下并缓存的**，不会随依赖恢复而自愈：

- `lifespan._build_knowledge_rag()` 只在启动时写入一次 `qdrant` 的结论，失败即 `ready=False`；
- `/ready` 读的是 `deps.degraded_names`；
- `_refresh_qdrant_detail()` 只刷新 `points_count` 这类数字，不翻转就绪结论。

所以端口修好、`curl :6333/collections` 也通了之后，`/ready` 仍会返回同一条旧错误，
看上去像修复无效。**重启服务进程即可**，不必改任何配置：

```bash
# Git Bash —— 前台服务 Ctrl+C 后重新起
uv run python scripts/serve.py --app src.agent_service.app:app --port 8080
```

重启后 `/ready` 应返回 200，且 `qdrant` 明细带出
`collection=jwipc_v3 mode=docker points_count=51`。

---

## 9. 一键自检

```bash
# Git Bash —— 每日门禁（静态检查 + 全量测试）
uv run ruff check .
uv run ruff format --check .
uv run pytest -q

# Git Bash —— 接口验收（自建子进程、真实 HTTP、8 分组）
uv run python scripts/service_week9_acceptance.py

# Git Bash —— 并发与超时（无 5xx + 504 归因）
uv run python scripts/service_week9_load.py --levels 20,50 --requests 40 --min-score 1.0

# Git Bash —— 重启与持久化（需 Docker；会短暂停掉本机 qdrant_server）
uv run python scripts/service_week9_compose_check.py
```

三个脚本的退出码：`0` 全部通过、`1` 有失败项、`2` 前置条件不满足。
详细参数见 `docs/week09_test_commands.md`。
