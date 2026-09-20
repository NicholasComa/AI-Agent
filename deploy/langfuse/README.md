# 自托管 Langfuse

本目录用 docker compose 在本机跑一套完整的 Langfuse，供仓库根 `services` 的
追踪层上报使用。相比用 Langfuse Cloud，它不需要注册账号、不需要配数据区域，
trace 数据全程不出本机。

自托管实例与 Cloud 是同一套代码、同一套 API，SDK 侧只差一个 `LANGFUSE_BASE_URL`
——本项目里这个值写在仓库根 `.env`，指向 `http://localhost:3000`。

## 目录内容

| 文件 | 作用 | 是否入库 |
|---|---|---|
| `compose.yaml` | 六个服务的编排定义 | 是 |
| `.env.example` | 配置模板，值留空 | 是 |
| `.env` | 实际密钥，由脚本随机生成 | 否 |

## 六个服务

| 服务 | 镜像 | 端口 | 宿主机可访问 |
|---|---|---|---|
| `langfuse-web` | `langfuse/langfuse:4` | 3000 | 是，UI 与 API 入口 |
| `langfuse-worker` | `langfuse/langfuse-worker:4` | 3030 | 仅 `127.0.0.1` |
| `clickhouse` | `clickhouse/clickhouse-server:25.12` | 8123 / 9000 | 仅 `127.0.0.1` |
| `minio` | `cgr.dev/chainguard/minio` | 9090 / 9091 | 9090 对外，供多模态直传 |
| `redis` | `redis:7` | 6379 | 仅 `127.0.0.1` |
| `postgres` | `postgres:17` | 5432 | 仅 `127.0.0.1` |

五个数据卷 `langfuse_*` 由 compose 自动加项目名前缀，与仓库根 `compose.yaml` 的
`qdrant_data`、`agent_service_sessions` 互不干扰。

资源占用以 ClickHouse 为主，六个容器空载合计约 2 到 4 GB 内存。本机 31.6 GB
物理内存，余量充足。

## 前置：启动 Docker Desktop

Docker Desktop 安装路径为本机用户目录，不是默认的 `Program Files`：

```text
C:\Users\Xsz\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe
```

从开始菜单启动 Docker Desktop，等托盘图标变为运行中，然后确认守护进程就绪。

运行环境：任意 shell

```bash
docker version --format '{{.Server.Version}}'
```

输出形如 `29.6.2` 即就绪。若报 `failed to connect to the docker API`，说明
Docker Desktop 还没起完，等一会儿再试。

## 首次启动

运行环境：Git Bash

```bash
cd /d/workspace/py_ai/week01_ai_basics/deploy/langfuse
docker compose up -d
```

运行环境：cmd

```cmd
cd /d D:\workspace\py_ai\week01_ai_basics\deploy\langfuse
docker compose up -d
```

首次执行会拉取六个镜像，累计约 1.5 GB。使用 `docker.1panel.live` 镜像源，
无需额外配置。看拉取进度：

运行环境：Git Bash 或 cmd，在 `deploy/langfuse` 目录下

```bash
docker compose logs -f
```

`Ctrl+C` 只退出日志跟随，不会停容器。看到 `langfuse-web` 打印 `Ready` 即可，
首次启动通常需要 2 到 3 分钟，其中 ClickHouse 建表最慢。

## 判断是否就绪

运行环境：Git Bash（`--noproxy '*'` 必须带，否则本机 `http_proxy` 会把
`127.0.0.1` 交给代理，得到空的 `HTTP 000`）

```bash
curl -s --noproxy '*' http://localhost:3000/api/public/health; echo " exit=$?"
```

预期输出 `{"status":"OK","version":"..."}`。

要连带校验数据库连通性，加查询参数：

```bash
curl -s --noproxy '*' "http://localhost:3000/api/public/health?failIfDatabaseUnavailable=true"; echo " exit=$?"
```

检查后台 worker（消费事件、写 ClickHouse 的那个容器）：

```bash
curl -s --noproxy '*' http://localhost:3030/api/health; echo " exit=$?"
```

三个端点全部返回 200 才算栈是完整可用的。`docker compose ps` 里六个服务的
状态都应带 `(healthy)`。

## 首次登录

浏览器打开 `http://localhost:3000`。

账号由 `deploy/langfuse/.env` 里的 `LANGFUSE_INIT_*` 在实例首次启动时自动创建，
不需要注册。登录邮箱固定为 `admin@local.dev`，密码读取方式：

运行环境：Git Bash，在 `deploy/langfuse` 目录下

```bash
grep -E '^LANGFUSE_INIT_USER_(EMAIL|PASSWORD)=' .env
```

运行环境：cmd

```cmd
findstr /B "LANGFUSE_INIT_USER_EMAIL LANGFUSE_INIT_USER_PASSWORD" .env
```

登录后左侧应已存在组织 `Local Org` 与项目 `Agent Service`。

## 验证密钥可用

项目的 API 密钥同样是启动时自动写入的，可在 UI 的 `Settings` → `API Keys`
复查。用命令行验证一次，比在界面上点更直接：

运行环境：Git Bash，在 `deploy/langfuse` 目录下

```bash
PK=$(grep -E '^LANGFUSE_INIT_PROJECT_PUBLIC_KEY=' .env | cut -d= -f2-)
SK=$(grep -E '^LANGFUSE_INIT_PROJECT_SECRET_KEY=' .env | cut -d= -f2-)
curl -s --noproxy '*' -u "$PK:$SK" http://localhost:3000/api/public/projects; echo " exit=$?"
```

返回项目信息的 JSON 即密钥有效。预期内容里应能看到组织 `Local Org` 与项目
`Agent Service`。

上面两条用 `grep` 逐行取值，而不是 `set -a; . ./.env`。原因是 `.env` 里
`LANGFUSE_INIT_ORG_NAME=Local Org` 这类值带空格，bash 的 `source` 会把空格后
的部分当成命令执行并报 `Org: command not found`。这类值对 docker compose 的
解析器没有问题，只在 shell 直接读取时不安全。

## 核对数据是否入库

`deploy/langfuse` 起的实例是 v4，运行在 `events_only` 模式下。**v4 里没有
`/api/public/traces` 这个端点了**，调用它会返回 404 并在响应体里说明该模式
不支持，容易被误读成「数据没进去」。读取观测数据要用 v2 端点：

运行环境：Git Bash，在 `deploy/langfuse` 目录下

```bash
PK=$(grep -E '^LANGFUSE_INIT_PROJECT_PUBLIC_KEY=' .env | cut -d= -f2-)
SK=$(grep -E '^LANGFUSE_INIT_PROJECT_SECRET_KEY=' .env | cut -d= -f2-)
curl -s --noproxy '*' -u "$PK:$SK" "http://localhost:3000/api/public/v2/observations?limit=5"; echo " exit=$?"
```

要确认原始事件真的落进了数据库（而不是只进了队列），直接查 ClickHouse 更权威：

运行环境：Git Bash，在 `deploy/langfuse` 目录下

```bash
CLP=$(grep -E '^CLICKHOUSE_PASSWORD=' .env | cut -d= -f2-)
docker compose exec -T clickhouse clickhouse-client -u clickhouse --password "$CLP" \
  -q "SELECT name, type, level FROM default.events_full ORDER BY name"
```

`events_full` 存的是入库前的原始事件，`events_core` 是同一批数据的窄表副本，
两者行数应当一致。

需要实证「没有上传提示词与回答正文」时，在这个表上做一次带对照的搜索：同时搜
一个**不该出现**的串（例如被清洗掉的敏感属性值）和一个**应当出现**的串（例如
异常消息，它会进 `status_message`）。只有前者为 0、后者非 0，才说明搜索有效且
没有泄露——只搜前者的话，搜索本身写错也会得到 0。

运行环境：Git Bash，在 `deploy/langfuse` 目录下

```bash
docker compose exec -T clickhouse clickhouse-client -u clickhouse --password "$CLP" \
  -q "SELECT count() FROM default.events_full WHERE position(status_message, '不许出现的串') > 0"
docker compose exec -T clickhouse clickhouse-client -u clickhouse --password "$CLP" \
  -q "SELECT count() FROM default.events_full WHERE status_message != ''"
```

注意 `events_full` 的表结构里本来就没有 input / output 列，正文要泄露也只能
出现在事件体里，因此判断依据是「搜不到」而不是「列为空」。

## 日常启停

运行环境：Git Bash 或 cmd，在 `deploy/langfuse` 目录下

```bash
docker compose stop      # 停容器，保留数据卷，下次 start 秒起
docker compose start     # 重新启动已停的容器
docker compose down      # 停并删除容器，数据卷保留
docker compose up -d     # 起栈
```

四种操作的取舍：日常关机用 `stop`；改了 compose 用 `down` 再 `up -d`；
`down` 不会删数据，只有显式加 `-v` 才会。

## 升级

运行环境：Git Bash 或 cmd，在 `deploy/langfuse` 目录下

```bash
docker compose pull
docker compose up -d
```

数据库迁移由 `langfuse-web` 容器在启动时自动执行，不需要额外的迁移步骤。
跨大版本升级前建议先备份数据卷。

## 完全重置

清空所有 trace、账号与密钥，回到全新状态。注意 `LANGFUSE_INIT_*` 只在实例
没有任何组织时执行，所以只有在删掉数据卷之后重新初始化才会生效。

运行环境：Git Bash 或 cmd，在 `deploy/langfuse` 目录下

```bash
docker compose down -v
docker compose up -d
```

`-v` 会删除五个数据卷，不可恢复。确认确实要丢弃历史 trace 再执行。

## 排障

镜像拉取失败，报 `manifest unknown` 或连接超时。

`compose.yaml` 里所有 Docker Hub 镜像都带 `${DOCKER_MIRROR}` 前缀，值在
`.env` 的 `DOCKER_MIRROR`。当前用 `docker.1panel.live/`。换源后重试：

运行环境：Git Bash 或 cmd，在 `deploy/langfuse` 目录下

```bash
docker compose pull
```

`cgr.dev/chainguard/minio` 不走镜像源，该仓库可直连，无需代理。

`langfuse-web` 一直不健康。

先看日志定位在哪个环节：

运行环境：Git Bash 或 cmd，在 `deploy/langfuse` 目录下

```bash
docker compose logs --tail=80 langfuse-web
```

常见原因有两类：某个依赖容器的健康检查没过（`docker compose ps` 会显示），
或者初始化参数格式错误。后者看日志里有没有 `LANGFUSE_INIT` 相关的报错——值
一旦带引号就会被当成字面量，初始化静默跳过。

`compose.yaml` 不给密钥设默认值，缺项时 `docker compose` 会直接报
`deploy/langfuse/.env 缺少 XXX` 并拒绝启动，不会用弱口令悄悄起来。

端口被占用。

运行环境：Git Bash

```bash
netstat -ano | grep LISTENING | grep -E ':(3000|3030|5432|6379|8123|9000|9090|9091)\b'
```

运行环境：cmd

```cmd
netstat -ano | findstr LISTENING | findstr /R ":3000 :3030 :5432 :6379 :8123 :9000 :9090 :9091"
```

最后一列是 PID，用 `tasklist /FI "PID eq <PID>"` 查是哪个进程。

## 与项目配置的关系

仓库根 `.env` 里的可观测性配置指向本实例：

```dotenv
OBS_BACKEND=langfuse
LANGFUSE_BASE_URL=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

密钥与 `deploy/langfuse/.env` 里的 `LANGFUSE_INIT_PROJECT_*` 是同一对，改一处
要同步另一处。

注意仓库根 `compose.yaml` 的 `api` 服务会把 `LANGFUSE_BASE_URL` 覆盖成
`http://host.docker.internal:3000`：容器内的 `localhost` 指的是容器自身，
既到不了宿主机的 3000 端口，也到不了本实例。这与该文件里 Ollama 和 Qdrant
的处理方式一致。

另需注意 `OBS_BACKEND` 的两个取值是互斥的：选 `langfuse` 时不再写本地
JSONL。若本实例没在运行，追踪记录只会在 SDK 的失败重试里消失，本地不留副本。
跑评测前先确认三个健康端点都返回 200。

## 端到端验证

仓库根的 `scripts/observability_week10_smoke.py` 会按 `.env` 配置跑一条完整
trace，再轮询本实例的公开 API 核对名称、类型、错误级别与 trace 分组。

运行环境：Git Bash，在仓库根目录

```bash
cd /d/workspace/py_ai/week01_ai_basics
export PATH="/c/Users/Xsz/.local/bin:$PATH"
uv run python scripts/observability_week10_smoke.py --verify-wait 60
```

全部项通过时退出码为 0。若实例没起来，脚本会在「实例健康检查」一步失败并
立即退出，不会留下半截数据。
