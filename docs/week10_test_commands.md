# 第十周 追踪层与 Langfuse 测试命令

本文件覆盖追踪抽象层与 Langfuse 接入：`src/observability/` 本体、全链路埋点
（RAG / 工作流 / 工具三类路由）、trace HTML 视图、两种后端（本地 JSONL /
自托管 Langfuse）的验证、以及界面上能看到什么。

命令按**追踪数据往哪去**分组：

1. 第一节 追踪层单元测试（不需要任何外部设施）
2. 第二节 本地 JSONL 后端（不联网、不起容器），含 trace 视图与响应头核对
3. 第三节 自托管 Langfuse（六个容器 + 界面与 API 核对）
4. 第四节 界面与记录名对照（看数据时最容易对不上的地方）
5. 第五节 对照表与已知问题

所有命令在 **Git Bash** 执行，工作目录 `D:\workspace\py_ai\week01_ai_basics`
（除注明 cmd 或 Docker Desktop 图形界面外）。

```bash
# Git Bash —— 先加 PATH 并进目录
export PATH="/c/Users/Xsz/.local/bin:$PATH"
cd /d/workspace/py_ai/week01_ai_basics
```

五个必须记住的点：

1. **跑之前先看在哪一层目录**。本文件有两种命令：仓库根（`uv run ...`）、
   `deploy/langfuse`（`docker compose ...`）。每段代码块首行都注明了所在目录。
   在仓库根跑 `docker compose exec clickhouse ...` 会报
   `service "clickhouse" is not running`，意思是**根 compose 里没有这个服务**，
   不是容器没起。
2. **长命令写成单行，不要用 `\` 续行**。反斜杠后带上尾随空格（复制粘贴常带）会让
   后续参数被当成独立命令，典型症状是 `bash: -q: command not found`。本文件所有
   命令按单行给出，可直接整行复制。
3. **curl 一律加 `--noproxy '*'`**。本机 `HTTP_PROXY` 指向本地代理，不加会把发往
   `127.0.0.1` 的请求交给代理，得到 `HTTP 000`。仓库内脚本已在 `httpx` 上设
   `trust_env=False`，脚本调用无需处理。
4. **从 `.env` 取值用 `grep | cut`，不要 `set -a; . ./.env`**。`deploy/langfuse/.env`
   里 `LANGFUSE_INIT_ORG_NAME=Local Org` 这类值带空格，bash 的 `source` 会把空格后的
   部分当成命令执行并报 `Org: command not found`。docker compose 解析该文件不受影响。
5. **v4 没有 `/api/public/traces` 端点**（返回 404，响应体说明 `events_only` 模式不支持）。
   读观测数据用 `/api/public/v2/observations`。这一点最容易误判成「数据没入库」。

另外两条只影响本文件第二节的 curl 段，单独在那一节开头写明：起服务要带
`--app agent_service.app:app`，中文请求体要 `printf` 落盘后 `--data-binary`。

---

## 一、追踪层单元测试（不需要外部设施）

### 追踪层本体

```bash
# Git Bash
uv run pytest tests/observability -q
```

预期：`106 passed`。覆盖配置解析、`contextvars` 隔离、四类后端降级路径、JSONL 落盘与
键名净化，以及埋点包装器与 LangGraph 节点回调。

### 接线是否改坏既有路由

追踪器挂进了依赖容器，因此要确认服务侧测试仍全绿：

```bash
# Git Bash
uv run pytest tests/agent_service -q
```

预期：`96 passed`。其中一条逐位断言 `/ready` 的依赖顺序，顺序被误改会在这里直接失败。

### 全量

```bash
# Git Bash
uv run pytest -q
```

预期：`576 passed, 1 skipped`。skip 是符号链接用例，环境限制（当前账户不允许创建
符号链接），与追踪无关。本周起点基线是 `462 passed + 1 skipped`。

### SDK 可用性

```bash
# Git Bash
uv run python -c "import langfuse; print(langfuse.__version__)"
```

预期：`4.15.4`（SDK 版本，由 `uv.lock` 固定）。自托管实例的版本号另有其值
（`langfuse/langfuse:4` 是浮动 tag，会随镜像更新变化），两者不需要一致。

### 门禁

```bash
# Git Bash
uv run ruff check .
uv run ruff format --check .
```

预期：`All checks passed!` 与 `151 files already formatted`。

### 落盘未污染仓库（隐性缺陷自查）

`tests/agent_service/service_fakes.build_deps()` 会注入指向 `tmp_path` 的本地追踪器。
漏了这处注入，症状是「测试全绿但仓库里莫名多出文件」。

```bash
# Git Bash
stat -c %s logs/traces/traces-*.jsonl     # 记下字节数
uv run pytest -q
stat -c %s logs/traces/traces-*.jsonl     # 应完全没变
```

预期：两次字节数一致。实测 `11302` 跑前跑后不变。

---

## 二、本地 JSONL 后端

### 核对配置

```bash
# Git Bash
grep -E '^(OBS_|LANGFUSE_)' .env
```

按需把 `OBS_BACKEND` 改成 `local` 再跑本节。两个取值互斥：选 `langfuse` 时不写本地
JSONL；选 `local` 时不联网。

### 端到端冒烟

```bash
# Git Bash
OBS_BACKEND=local uv run python scripts/observability_week10_smoke.py --verify-wait 0
```

`--verify-wait 0` 表示跳过远端入库核对（本地后端本就不上网）。预期末行为
`冒烟全部通过`，退出码 0。实测输出：

```text
== 后端 ==
  [OK]   后端选择  jsonl
== 追踪 ==
  [OK]   本次种子后缀  1789897503
== 落点核对 ==
  [OK]   本地落盘  24 条记录，覆盖 5 个 span
  [OK]   敏感键清洗  prompt 未进入任何记录
  [OK]   未收尾 span  被收尾并标记：['smoke.unfinished', 'smoke.unfinished', 'smoke.unfinished']

冒烟全部通过
```

脚本跑三条路径：正常链（`smoke.request` 下挂 retriever / generation / tool）、异常链
（在工具 span 内抛异常，标 `error`）、未收尾链（只 `start_trace` 不 `end_trace`，
落盘前统一补收尾并标 `error` + `attributes.unfinished=true`）。

### 直接看落盘文件

```bash
# Git Bash
ls -l logs/traces/
wc -l < "logs/traces/traces-$(date +%F).jsonl"
head -n 1 "logs/traces/traces-$(date +%F).jsonl" | python -m json.tool
```

文件名按天切片，形如 `traces-2026-09-20.jsonl`。首行结构：

```json
{
    "trace_id": "7a396dbac3c3a28fd13921f1ab5a687f",
    "span_id": "995ddaa5cb1e423c",
    "parent_id": "0681b4eab56b4faa",
    "name": "retrieve",
    "kind": "retriever",
    "started_at": "2026-09-20T06:29:07.547595Z",
    "ended_at": "2026-09-20T06:29:07.547595Z",
    "duration_ms": 0.0,
    "status": "ok",
    "error_type": null,
    "error_message": null,
    "model": null,
    "usage": null,
    "cost_usd": null,
    "request_id": null,
    "attributes": {"top_k": 3, "strategy": "vector", "top1_score": 0.87},
    "content": null,
    "unfinished": false
}
```

`content` 为 `null` 是预期行为：`OBS_CAPTURE_CONTENT=false` 时只落结构字段，检索来源
只记哈希。语料含内部手册，该值保持 `false`。

### 复现同一条 trace

```bash
# Git Bash
OBS_BACKEND=local uv run python scripts/observability_week10_smoke.py --verify-wait 0 --seed-suffix 20260920
```

`--seed-suffix` 固定种子后缀。缺省取当前时间戳，因此连续两次运行会产生不同的
`trace_id`；固定它可让多次运行落进同一条 trace 便于对比，也便于复现问题。

### 看单条 trace 的完整结构（HTML 视图）

```bash
# Git Bash
uv run python scripts/trace_week10_view.py --list
```

列出现有 trace，**按开始时间升序，最新的在最后一行**。要挑最近的一条可以直接
`--list | tail -n 3`，或者用 `--trace-id` 精确指定：

```bash
# Git Bash —— 缺省自动取「开始时间最晚」的那条，无需先 --list
uv run python scripts/trace_week10_view.py --out logs/traces/view.html
```

```bash
# Git Bash —— 指定 trace 标识；--screenshot 直接出图，路径写仓库相对路径即可
uv run python scripts/trace_week10_view.py --trace-id 42fabcceff5d4367a4b06893cab9e29b --out logs/traces/view.html --screenshot docs/poho/week10/d47_trace_rag.png
```

输出的 HTML 左栏是 span 树（按 `parent_id` 缩进，父节点缺失的记为根），右栏是统一
刻度的时间轴，下方逐条列出该 span 的全部字段。只读取本地 JSONL，不联网。

摘要里三行值得核对：

```text
span 数    : 3
类型       : generation, retriever, trace
根 span 数 : 1
```

`根 span 数` 大于 1 说明有 span 的父节点没落到同一 trace 里（通常是没接上 trace
上下文）；类型里缺 `chain` 说明工作流节点埋点没生效。

### 核对全链路四类 span 同时出现

一条 RAG 请求应同时有 `retriever` 与 `generation`：

```bash
# Git Bash
uv run python scripts/trace_week10_view.py --list | tail -n 5
```

一条工作流请求应有 6 条 `chain`（对应 6 个执行节点）+ 1 条 `trace`：

```bash
# Git Bash —— 把上一步看到的 workflow trace 标识填进来
uv run python scripts/trace_week10_view.py --trace-id <workflow_trace_id> --out logs/traces/wf.html
```

`chain` 条数必须等于响应体 `trace` 字段的节点数。若恰好是两倍，说明 LangGraph 的
pregel 包装层没被折叠掉——每个节点外层还有一层同名包装，回调会看到两层
`on_chain_start`。

### 验证响应头带 trace 标识

先起服务。**注意 `--app` 必填**：`scripts/serve.py` 缺省加载 `src.main:app`（第五周的
网关），那个应用没有 `/v1/rag/answer`，会返回 404 让人误判成「埋点没生效」。

```bash
# Git Bash，在仓库根目录 —— 起服务，保持这个窗口不关
uv run python scripts/serve.py --port 8000 --app agent_service.app:app
```

另开一个 Git Bash 窗口发请求。**中文请求体必须先用 `printf` 落盘再 `--data-binary`**，
内联 `-d '{"question":"中文"}'` 在本机会得到空响应（Git Bash 的编码转换问题）；
`-D` / `-o` 的路径写 **Windows 绝对路径**，写相对路径会报
`curl: Failed to open ...`。

```bash
# Git Bash —— 另开窗口；TP 指向一个已存在的临时目录
TP="D:/workspace/py_ai/week01_ai_basics/data/week10_curl_tmp"
mkdir -p "$TP"
printf '%s' '{"question":"Qdrant 是什么？","top_k":2}' > "$TP/q.json"
curl -sS --noproxy '*' -m 30 -D "$TP/h.txt" -o "$TP/b.json" -X POST http://127.0.0.1:8000/v1/rag/answer -H 'Content-Type: application/json' --data-binary @"$TP/q.json"
grep -i 'x-trace-id' "$TP/h.txt"
```

预期输出一行 `x-trace-id: <32 位十六进制>`。实测（Qdrant 未启动时为 503，属预期）：

```text
HTTP/1.1 503 Service Unavailable
x-trace-id: 3c7412c4a7004b53b229a0c20e574fa4
x-request-id: 20888d8eff8d4947b5df2b86a2507f5b
```

并核对错误信封的 `detail` 末尾带同一个标识：

```bash
# Git Bash
python -m json.tool "$TP/b.json"
```

```json
{"error": {"code": "dependency_unavailable",
           "detail": "... 502 (Bad Gateway) trace=3c7412c4a7004b53b229a0c20e574fa4"}}
```

四个要点：

1. **成功与失败都要带头**。503 的响应同样应带 `x-trace-id`，且错误信封的
   `error.detail` 末尾会追加 ` trace=<同一个标识>`。
2. **探针路径不带**。`/health`、`/ready`、`/metrics-summary` 不建 trace，因此没有
   该头，这是预期行为而非缺陷。
3. **路由前的 422 不带**。参数校验失败发生在路由体执行之前，此刻还没有 trace，
   所以 `X-Trace-Id` 为空、`detail` 里也没有 `trace=`。这是设计如此。
4. **根 span 不能是 error**。取一条成功请求的 `trace_id` 渲染成 HTML，确认其
   `status` 为 `ok`。历史上曾把 `X-Trace-Id` 写在声明式返回模型实例上，Pydantic
   会抛 `ValueError`，该异常穿出 `tracer.trace()` 会把本该成功的根 span 标成错误
   ——症状是「接口正常但所有 trace 都是失败的」。正确写法是给路由加
   `response: Response` 参数并在 trace 体外写头。

---

## 三、自托管 Langfuse

### 前置：启动 Docker Desktop

Docker Desktop 安装在本机用户目录，不是默认的 `Program Files`：

```text
C:\Users\Xsz\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe
```

运行环境：Docker Desktop 图形界面（从开始菜单启动，等托盘图标变为运行中）

确认守护进程就绪：

```bash
# Git Bash 或 cmd，任意目录
docker version --format '{{.Server.Version}}'
```

预期输出 `29.6.2`。报 `failed to connect to the docker API` 说明还没起完。

### 端口占用检查

```bash
# Git Bash
netstat -ano | grep LISTENING | grep -E ':(3000|3030|5432|6379|8123|9000|9090|9091)\b'
```

```cmd
:: cmd
netstat -ano | findstr LISTENING | findstr /R ":3000 :3030 :5432 :6379 :8123 :9000 :9090 :9091"
```

有输出说明端口被占，最后一列是 PID，用 `tasklist /FI "PID eq <PID>"` 查是哪个进程。
本机跑栈时这八个端口应为空。

### 起栈

```bash
# Git Bash
cd /d/workspace/py_ai/week01_ai_basics/deploy/langfuse
docker compose up -d
```

```cmd
:: cmd
cd /d D:\workspace\py_ai\week01_ai_basics\deploy\langfuse
docker compose up -d
```

首次会拉六个镜像（累计约 1.5 GB，实测 7 分 8 秒）。`compose.yaml` 里所有 Docker Hub
镜像都带 `${DOCKER_MIRROR}` 前缀，值在 `deploy/langfuse/.env`，当前为
`docker.1panel.live/`；`cgr.dev/chainguard/minio` 不走镜像源。

### 就绪判定

```bash
# Git Bash，在 deploy/langfuse 目录下
docker compose ps
```

六个服务都应为 `Up`。注意**只有四个基础容器带 `(healthy)`**（`clickhouse`、`minio`、
`redis`、`postgres`），`langfuse-web` 与 `langfuse-worker` 没有定义健康检查，显示为
`Up N hours` 属正常。判据是下面三个端点全部 200：

```bash
# Git Bash —— --noproxy '*' 必须带
curl -s --noproxy '*' http://localhost:3000/api/public/health; echo " exit=$?"
curl -s --noproxy '*' "http://localhost:3000/api/public/health?failIfDatabaseUnavailable=true"; echo " exit=$?"
curl -s --noproxy '*' http://localhost:3030/api/health; echo " exit=$?"
```

预期三条都是 `exit=0` 且 HTTP 状态码 `200`：

| 命令 | 预期输出 | 状态码 |
| --- | --- | --- |
| 第 1 条 | `{"status":"OK","version":"..."}` | 200 |
| 第 2 条 | 与第 1 条**完全相同** | 200 |
| 第 3 条 | `{"status":"ok"}`（小写 `ok`，与上面不同） | 200 |

三点说明：

1. **判据是状态码，不是响应体。** 第 2 条的 `failIfDatabaseUnavailable=true` 只在数据库
   不可达时改变行为——那时返回 **503**；数据库正常时响应体与第 1 条一字不差。所以"两条
   输出看起来一样"是正常现象，不代表参数没生效。未知查询参数会被静默忽略（实测
   `?nonsense=1` 同样返回 200），因此单看响应体区分不出"参数生效"与"参数被忽略"。
2. 第 3 条的 worker 端点，**200 本身就表示数据库连接成功**，与第 2 条构成两项独立的
   数据库连通性证据。
3. 输出里的**版本号不是固定值**：`compose.yaml` 用的是浮动 tag（`langfuse/langfuse:4`），
   镜像更新后版本号会变，只要 `status` 为 `OK` 即视为通过。

### 验证密钥

```bash
# Git Bash，在 deploy/langfuse 目录下
PK=$(grep -E '^LANGFUSE_INIT_PROJECT_PUBLIC_KEY=' .env | cut -d= -f2-)
SK=$(grep -E '^LANGFUSE_INIT_PROJECT_SECRET_KEY=' .env | cut -d= -f2-)
curl -s --noproxy '*' -u "$PK:$SK" http://localhost:3000/api/public/projects; echo " exit=$?"
```

预期返回项目信息，含组织 `local` 与项目 `agent-service`。账号由 `LANGFUSE_INIT_*`
在实例首次启动时自动创建，不需要注册。

### 端到端冒烟

```bash
# Git Bash，在仓库根目录
cd /d/workspace/py_ai/week01_ai_basics
export PATH="/c/Users/Xsz/.local/bin:$PATH"
uv run python scripts/observability_week10_smoke.py --verify-wait 60
```

`--verify-wait 60` 是等待记录入库的最长秒数。实例没起来时脚本会在「实例健康检查」
一步失败并立即退出，不会留下半截数据。实测输出：

```text
  backend                      langfuse
  langfuse_base_url            http://localhost:3000
  langfuse_configured          True
  langfuse_public_key_hint     pk-lf-...(38)
== 后端 ==
  [OK]   后端选择  langfuse
  [OK]   实例健康检查  4.38.0 @ http://localhost:3000
== 落点核对 ==
  [OK]   远端入库  8 条记录全部可取回
  [OK]   类型映射  SPAN / RETRIEVER / GENERATION / TOOL / CHAIN 均正确
  [OK]   错误分级  ['broken.tool', 'smoke.error', 'smoke.unfinished'] 均为 ERROR
  [OK]   trace 分组  三条 trace 分别为 4 / 2 / 2 条记录

冒烟全部通过
```

密钥只打印前缀与长度（`pk-lf-...(38)`），这是脚本的既定行为。「trace 分组」核对的是
内部按 seed 分组后的记录数，与界面上显示的条目名不是同一套命名，见第四节。

### 核对数据真的入库

API 层核对过了还想看数据库，直接查 ClickHouse 更权威。

**先确认工作目录**：必须在 `deploy/langfuse` 下执行。在仓库根目录跑会报
`service "clickhouse" is not running`——根 `compose.yaml` 里没有这个服务，报错指的是
「这个项目里没有该服务」，不是「容器没起」。

```bash
# Git Bash，在 deploy/langfuse 目录下
CLP=$(grep -E '^CLICKHOUSE_PASSWORD=' .env | cut -d= -f2-)
docker compose exec -T clickhouse clickhouse-client -u clickhouse --password "$CLP" -q "SELECT name, type, level FROM default.events_full ORDER BY name"
```

不想打成一行，也可以先 `export` 掉密码（`export` 而非 `CLP=...`，这样同一终端后续命令都能用）：

```bash
# Git Bash，在 deploy/langfuse 目录下 —— 三条都是单行
export CLP=$(grep -E '^CLICKHOUSE_PASSWORD=' .env | cut -d= -f2-)
docker compose exec -T clickhouse clickhouse-client -u clickhouse --password "$CLP" -q "SELECT count() AS rows, uniqExact(name) AS names FROM default.events_full"
```

`events_full` 存入库前的原始事件，`events_core` 是同一批数据的窄表副本，两者行数应一致。
**每条已上报记录各占一行**，因此 `rows` 能直接反映「冒烟跑过几次」：一次冒烟产生 8 条
记录，同日跑 6 次即 48 行；`names` 是覆盖的 span 名个数，实测 8 个
（`broken.tool` / `dropped.step` / `generate` / `retrieve` / `smoke.error` /
`smoke.request` / `smoke.unfinished` / `tool_check_commit`）。实例只要没被重置，这张表是
累积的，行数偏大属正常。

### 实证没有上传正文

在 `events_full` 上做一次**带对照**的搜索，同时搜一个不该出现的串和一个应当出现的串。
只搜前者的话，搜索写错也会得到 0。

```bash
# Git Bash，在 deploy/langfuse 目录下 —— 两条独立命令，各写一行
docker compose exec -T clickhouse clickhouse-client -u clickhouse --password "$CLP" -q "SELECT count() FROM default.events_full WHERE position(status_message, '提示词里的独有串') > 0"
docker compose exec -T clickhouse clickhouse-client -u clickhouse --password "$CLP" -q "SELECT count() FROM default.events_full WHERE status_message != ''"
```

预期：第一条 `0`，第二条**非 0**。第二条是判据成立的**前提**——它为 0 说明搜索本身无效，
第一条的 0 就不构成任何证据。实测第二条随冒烟次数增长（每跑一次多 3 条带
`status_message` 的记录：`smoke.error`、`broken.tool`、`smoke.unfinished`），出现过
3、18 等值，不必追求某个固定数字。

这里用 `position(...)` 而不是 `LIKE`：`LIKE` 在查询串含 `%` 或 `_` 时会变成通配符，
误把别的行也算命中，而带 `%` 的提示词正文恰恰是最需要排除的情形。`position` 是纯子串
查找，语义上没有这个歧义。

另 `events_full` 的表结构里本来就没有 input / output 列，正文要泄露只能出现在事件体里，
因此判断依据是「搜不到」而非「列为空」；`events_core` 只有一个 `t` 列，对它用
`toString(t.*)` 会被 ClickHouse 以 `UNKNOWN_IDENTIFIER` 拒绝，要查就查 `events_full`。

### 日常启停

```bash
# Git Bash 或 cmd，在 deploy/langfuse 目录下
docker compose stop      # 停容器，保留数据卷，下次 start 秒起
docker compose start     # 重新启动已停的容器
docker compose down      # 停并删除容器，数据卷保留
docker compose up -d     # 起栈
```

`down` 不删数据，只有显式加 `-v` 才会。整库重置、升级、排障见
`deploy/langfuse/README.md`。

---

## 四、界面与记录名对照

看界面时最容易对不上的一处：**冒烟脚本输出的三个名字是 seed，不是界面上能看到的条目名。**

界面列出的是 **observation**，每条有 `name`。一次冒烟产生 8 条 observation，分布在
三条 trace 上：

| 脚本输出里的 seed | 界面上的根 observation 名 | 该 trace 的记录数 | 级别 |
| --- | --- | --- | --- |
| `smoke-ok-<种子>` | `smoke.request` | 4 | 根 `DEFAULT`，子项 `DEFAULT` |
| `smoke-error-<种子>` | `smoke.error` | 2 | 根 `ERROR`，子项 `ERROR` |
| `smoke-unfinished-<种子>` | `smoke.unfinished` | 2 | 根 `ERROR` |

`smoke-ok` 只用于由种子推导 `trace_id`，**不会作为任何 observation 名出现**，在
Tracing 列表里搜它必然是空结果。要确认「正常那条 trace 进去了」，看 `smoke.request`。

这 8 条 observation 的完整对照：

| observation 名 | 类型 | 级别 | 属于哪条 trace |
| --- | --- | --- | --- |
| `smoke.request` | SPAN | DEFAULT | ok |
| `retrieve` | RETRIEVER | DEFAULT | ok |
| `generate` | GENERATION | DEFAULT | ok |
| `tool_check_commit` | TOOL | DEFAULT | ok |
| `smoke.error` | SPAN | ERROR | error |
| `broken.tool` | TOOL | ERROR | error |
| `smoke.unfinished` | SPAN | ERROR | unfinished |
| `dropped.step` | CHAIN | DEFAULT | unfinished |

在 Tracing 列表里筛选「名称为 `smoke.request`」即可定位正常链；日期选 **Past 1 day**，
否则刚跑的数据可能不在默认时间窗内。

### 界面核对

浏览器打开 `http://localhost:3000`。登录邮箱固定为 `admin@local.dev`，密码取值：

```bash
# Git Bash，在 deploy/langfuse 目录下
grep -E '^LANGFUSE_INIT_USER_(EMAIL|PASSWORD)=' .env
```

```cmd
:: cmd，在 deploy/langfuse 目录下
findstr /B "LANGFUSE_INIT_USER_EMAIL LANGFUSE_INIT_USER_PASSWORD" .env
```

登录后左侧应有组织 `Local Org` 与项目 `Agent Service`。默认认证状态可能是 `Read`，
需要写入操作时在右上角切到编辑权限。

---

## 五、对照表与已知问题

### 计划措辞与实际实现

规划文档写作时早于实施，以下几处与实际交付不同，以本表为准：

| 规划里的写法 | 实际实现 | 原因 |
| --- | --- | --- |
| `uv sync --group obs`、`[dependency-groups] obs = ["langfuse>=3"]` | `langfuse>=4,<5` 写在主 `dependencies`，直接 `uv run` 自动同步 | 实测 v4 SDK 与主链路无版本冲突；单独分组反而让 `tests/observability` 依赖安装顺序 |
| `LANGFUSE_HOST` | `LANGFUSE_BASE_URL` | 与 SDK 自身读取的键名一致，避免一处两写法 |
| Cloud 端点 `https://cloud.langfuse.com` | 自托管 `http://localhost:3000` | 改为自托管后不需要注册与选数据区域 |
| 周一「不加测试」，周二起补到 20 条 | 首日即交付 62 条（`tests/observability/`） | 降级链有四种路径，不给测试覆盖会静默失效 |
| `docs/week10_observability.md` 五节骨架 | 六节（多「相关文件」一节） | 便于按功能找文件入口 |

### 速查

| 用途 | 命令 | 依赖 |
| --- | --- | --- |
| 追踪层单测 | `uv run pytest tests/observability -q` | 无 |
| 全量单测 | `uv run pytest -q` | 无 |
| 本地后端冒烟 | `OBS_BACKEND=local uv run python scripts/observability_week10_smoke.py --verify-wait 0` | 无 |
| 列出现有 trace | `uv run python scripts/trace_week10_view.py --list` | 有落盘记录 |
| 渲染 trace 视图 | `uv run python scripts/trace_week10_view.py --out logs/traces/view.html` | 有落盘记录 |
| 渲染并截图 | `uv run python scripts/trace_week10_view.py --screenshot docs/poho/week10/d47_trace_rag.png` | Edge |
| 自托管端到端冒烟 | `uv run python scripts/observability_week10_smoke.py --verify-wait 60` | 六个容器 |
| 起自托管栈 | `cd deploy/langfuse && docker compose up -d` | Docker Desktop |
| 停自托管栈 | `cd deploy/langfuse && docker compose stop` | Docker Desktop |
| 看 ClickHouse 落库 | `cd deploy/langfuse && docker compose exec -T clickhouse clickhouse-client -u clickhouse --password "$CLP" -q "SELECT name, type, level FROM default.events_full ORDER BY name"` | 六个容器 |

### 五个已知问题

**1. `OBS_BACKEND` 两个取值互斥。** 选 `langfuse` 时不再写本地 JSONL。若实例没在
运行，追踪记录只会在 SDK 的失败重试里消失，本地不留副本。跑评测前先确认三个健康
端点都返回 200；需要本地留档就先切 `local` 跑一遍。

**2. v4 没有 `/api/public/traces`。** 自托管实例运行在 `events_only` 模式下，该端点
返回 404 并在响应体里说明不支持。用 `/api/public/v2/observations` 读取，返回的
`type` 为大写（`SPAN` / `RETRIEVER` / `GENERATION` / `TOOL` / `CHAIN`）。

**3. `langfuse-web` 与 `langfuse-worker` 不带 `(healthy)`。** 这两个容器没有定义
健康检查，`docker compose ps` 里显示为 `Up N hours`。不要据此判定栈有问题，用第三节
的三个健康端点判断。

**4. `smoke-ok` 不是 observation 名。** 它是生成 `trace_id` 的 seed，界面上不存在
该条目。界面里对应的是根 observation `smoke.request`。详见第四节。

**5. 路由前的 422 没有 trace 标识。** 参数校验由 FastAPI 在进入路由体之前完成，
此时还没建 trace，因此响应头不带 `X-Trace-Id`、信封里也没有 `trace=`。这是设计
如此，不要当成缺陷。需要给这类请求也留下标识时，只能由客户端自带 `X-Trace-Id`
请求头并在网关侧记录。

### 与其它文档的分工

| 文档 | 面向 | 内容 |
| --- | --- | --- |
| 本文件 | 执行 | 可直接复制的命令、预期输出、界面与记录名对照 |
| `docs/week10_observability.md` | 工程 | 后端与降级链、span 字段口径、配置键全表、v4 API 对照 |
| `deploy/langfuse/README.md` | 部署 | 六个容器的起停、升级、整库重置、排障 |
| 桌面 `12week/week10_observability_concepts.md` | 学习 | 术语与项目落点、指标能否规则计算的判断表 |
