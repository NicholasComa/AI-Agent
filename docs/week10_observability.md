# 第十周 可观测性（Langfuse Trace）

本周把「服务跑起来」推进到「跑得怎么样能被看见」：新增 `src/observability/` 追踪层，
以纯元数据方式记录每次请求的 span 树，默认落到本地 JSONL，配置齐全时上报自托管
Langfuse。追踪属于旁路能力——后端全挂也不影响业务接口。

本节覆盖七节：后端与降级、span 字段表、配置键全表、查看步骤、v4 API 要点、**埋点落点表**
（哪些模块在哪些位置开了 span）、本地视图脚本与三种接入方式。埋点落点表在周三补写，因为
它要等评测链路也接上埋点之后才完整。

## 1 后端与降级

### 1.1 三个后端

| 后端 | 类 | 行为 | 何时被选中 |
| --- | --- | --- | --- |
| 本地 JSONL | `JsonlBackend` | 每个 span 结束追加一行 JSON 到 `logs/traces/traces-<日期>.jsonl` | `OBS_BACKEND=local`，或 Langfuse 不可用时自动回退 |
| Langfuse | `LangfuseBackend` | 经 SDK（建在 OpenTelemetry 之上）上报到 Cloud 或自托管实例 | `OBS_BACKEND=langfuse` 且 SDK 可用、凭据齐全、初始化成功 |
| 内存 | `InMemoryBackend` | 只在进程内留一份记录，不落盘不上报 | `OBS_ENABLED=false`，或 JSONL 目录不可写 |

### 1.2 降级链

构造入口是 `observability.build_tracer()`，它调用 `backends.build_backend()`
逐级回退，**任何一步失败都只降级、绝不抛异常**：

```
OBS_ENABLED=false                     → InMemoryBackend(reason="disabled")
OBS_BACKEND=local                     → JsonlBackend
OBS_BACKEND=langfuse 且 SDK 未安装    → JsonlBackend(reason="langfuse sdk not installed")
OBS_BACKEND=langfuse 且凭据缺失       → JsonlBackend(reason="langfuse credentials missing")
OBS_BACKEND=langfuse 且初始化失败     → JsonlBackend(reason="langfuse init failed: ...")
JsonlBackend 构造失败（目录不可写等） → InMemoryBackend(reason="jsonl unavailable: ...")
```

两条设计约束：

1. **构造不抛异常**。调用方没有失败分支要处理。代价是「追踪没生效」不会以异常形式
   冒出来，只能靠探针明细里的 `degraded=` 提示发现。
2. **降级原因一路带下去**。`Tracer.degraded_reason` 暴露给启动日志与 `/ready` 明细，
   因此「为什么没上报」在探针响应里就能读到，不必翻日志找。

### 1.3 与探针的关系

`observability` 是**可选依赖**：`ready=false, required=false`，它降级不会让
`/ready` 返回 503。业务接口不读 `deps.tracer`，因此追踪挂了不影响任何路由。

`/ready` 的 `dependencies` 固定顺序（见 `routes/health.py` 的 `_READY_SCAN_ORDER`）：

```
observability, session_store, config, qdrant, llm, mcp, workflow
```

它排在第一位，与 `lifespan.build_deps` 的组装顺序一致。组装顺序还有一个副作用：
释放钩子按注册**逆序**执行，最先注册的追踪器最后释放，因此其它资源关闭期间产生的
span 仍然有地方可写。

## 2 span 字段表

一条记录就是 `observability.models.SpanRecord` 的一行。JSONL 后端每行写一个对象，
Langfuse 后端把同样的语义映射到远端 observation。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `trace_id` | `str` | 一次请求的标识。确定性采样就是对这个值做哈希，因此同 seed 可复现。 |
| `span_id` | `str` | 本 span 的标识。 |
| `parent_id` | `str \| None` | 父 span；根 span 为 `None`。层级靠它事后还原。 |
| `name` | `str` | 人工命名，取业务语义（如 `retrieve`、`generate`）。 |
| `kind` | `str` | 见下方 `SPAN_KINDS`。 |
| `started_at` / `ended_at` | `str \| None` | ISO 8601，UTC，带 `Z`。 |
| `duration_ms` | `float \| None` | 由两端时间相减得出。 |
| `status` | `str` | `ok` 或 `error`。 |
| `error_type` / `error_message` | `str \| None` | span 内抛异常时填入，异常本身继续向上抛。 |
| `model` | `str \| None` | 生成类 span 的模型名。 |
| `usage` | `dict \| None` | token 计数，见 `pricing.py`。 |
| `cost_usd` | `float \| None` | 由 token 与单价表算出。 |
| `request_id` | `str \| None` | 与访问日志、错误信封里的值同源，用于跨系统对齐。 |
| `attributes` | `dict` | 结构化元数据。字符串值超过 `max_label_chars` 即丢弃，不做截断。 |
| `content` | `dict \| None` | 提示词与回答正文。**默认恒为 `None`**。 |
| `unfinished` | `bool` | 只进不出的 span 在收尾时被标记，配合 `status="error"`。 |

### 2.1 六种 span 类型

`SPAN_KINDS = ("trace", "span", "generation", "retriever", "tool", "chain")`。

映射到 Langfuse 时，远端原生支持 `retriever` / `tool` / `chain` 三类，因此真实语义
直接落到类型上，不必塞进元数据字段；只有根 span 例外，它没有对应的专用类型，
统一用 `span`：

| 本地 `kind` | 远端 observation `type` |
| --- | --- |
| `trace` | `SPAN` |
| `span` | `SPAN` |
| `chain` | `CHAIN` |
| `generation` | `GENERATION` |
| `retriever` | `RETRIEVER` |
| `tool` | `TOOL` |

### 2.2 正文边界的实现方式

「不记正文」不是靠调用方自觉，而是结构上成立的：

- 配置侧 `capture_content` 默认 `false`，此时 `SpanRecord.content` 恒为 `None`；
- Langfuse 后端的 `begin` / `end` **从不传** `input` / `output` 参数，而 SDK 只在
  显式传参时才会上传内容；
- `.env.example` 与 `.env` 都设了 `LANGFUSE_OBSERVE_DECORATOR_IO_CAPTURE_ENABLED=false`，
  作为第二道防线——避免引入 `@observe` 装饰器后 SDK 自行捕获入参出参。

验证方式见 4.3：在 ClickHouse 里做受控检索，一个预期命中的字符串加一个预期不命中的。

## 3 配置键全表

追踪层用 `OBS_` 前缀，Langfuse SDK 自身消费的键用 `LANGFUSE_` 前缀。两组的读取方式
不同，见 3.1。

| 键 | 默认值 | 取值 | 说明 |
| --- | --- | --- | --- |
| `OBS_ENABLED` | `true` | 布尔 | 总开关。关闭时全部追踪 API 退化为空操作，零落盘、零上报。 |
| `OBS_BACKEND` | `local` | `local` / `langfuse` | 非法值回退 `local`。 |
| `OBS_TRACE_DIR` | `logs/traces` | 路径 | JSONL 落盘目录，相对路径按进程工作目录解析。 |
| `OBS_CAPTURE_CONTENT` | `false` | 布尔 | 置 `true` 才会记录提示词与回答正文。 |
| `OBS_MAX_CONTENT_CHARS` | `2000` | 正整数 | 正文单字段截断长度，仅 `capture_content` 为真时生效。 |
| `OBS_MAX_LABEL_CHARS` | `64` | 正整数 | `attributes` 里字符串值的长度上限，超出即丢弃。 |
| `OBS_SAMPLE_RATE` | `1.0` | `0.0` 到 `1.0` | 采样率。按 `trace_id` 哈希确定性采样，同一 trace 要么全记要么全不记。 |
| `LANGFUSE_PUBLIC_KEY` | 空 | `pk-lf-...` | 公钥。 |
| `LANGFUSE_SECRET_KEY` | 空 | `sk-lf-...` | 私钥，真实凭据，只应存在于 `.env`。 |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com` | URL | 端点。自托管填 `http://localhost:3000`。也接受 `LANGFUSE_HOST` 写法。 |
| `LANGFUSE_RELEASE` | 空 | 字符串 | 可选发布标识，写进 trace 的版本字段。 |
| `LANGFUSE_OBSERVE_DECORATOR_IO_CAPTURE_ENABLED` | `false` | 布尔 | 由 SDK 读取，锁住装饰器的入参出参捕获。 |

### 3.1 两组键的读取差异

`ObservabilityConfig` 是冻结 dataclass + 手写 `os.getenv`，不是 `BaseSettings`。
原因是项目里已有的两个配置类都装不下这组键：`config.AppConfig` 禁用了自动读取
环境变量，`AgentServiceSettings` 的前缀被 `AGENT_SERVICE_` 写死，而一个
`BaseSettings` 只能有一个前缀，容不下 `LANGFUSE_*`。

由此产生两条使用约束：

1. **解析失败一律静默回退默认值，绝不阻断启动。** 配置写错只该让行为退化成保守
   默认，不该让服务起不来。拼错的布尔值（如 `OBS_CAPTURE_CONTENT=flase`）会被当成
   「没配」而不是显式的 `False`，并按 `debug` 级别记一条 `config_invalid`。
2. **密钥不会被打印。** `describe()` 只导出 `langfuse_configured` 与前 6 位 + 总长度
   的提示串（形如 `pk-lf-...(...)`），足够辨认用的是哪把 key，又不足以泄漏完整值。

## 4 本地 JSONL 与自托管 Langfuse 的查看步骤

### 4.1 看本地 JSONL

运行环境：Git Bash，工作目录为仓库根。

```bash
cd /d/workspace/py_ai/week01_ai_basics
ls -l logs/traces/
head -n 1 logs/traces/traces-2026-09-20.jsonl | python -m json.tool
wc -l logs/traces/traces-2026-09-20.jsonl
```

`logs/` 已被 `.gitignore` 整体忽略，临时实验产物不会进版本库。若只想在不联网环境
下验证接线，把 `OBS_BACKEND=local` 即可，无需任何外部依赖。

### 4.2 看自托管 Langfuse

运行环境：Git Bash。先在界面看到 trace 之前，六个容器都要起来。

```bash
cd /d/workspace/py_ai/week01_ai_basics/deploy/langfuse
docker compose up -d
docker compose ps
```

浏览器打开 `http://localhost:3000`，用 `.env` 里 `LANGFUSE_INIT_USER_EMAIL` 与
`LANGFUSE_INIT_USER_PASSWORD` 指定的账号登录，进入项目即可看到 trace 列表。
部署细节与生命周期命令见 `deploy/langfuse/README.md`。

端到端冒烟（写三条 trace 并回读校验）：

```bash
cd /d/workspace/py_ai/week01_ai_basics
export PATH="/c/Users/Xsz/.local/bin:$PATH"
uv run python scripts/observability_week10_smoke.py --verify-wait 60
```

只校验落盘、不访问实例时，把后端切到本地并跳过入库核对：

```bash
cd /d/workspace/py_ai/week01_ai_basics
export PATH="/c/Users/Xsz/.local/bin:$PATH"
OBS_BACKEND=local uv run python scripts/observability_week10_smoke.py --verify-wait 0
```

脚本会带一个随机种子后缀，因此反复运行不会把多次结果并进同一条 trace。要复现同一条
trace 时用 `--seed-suffix 42` 固定它。

### 4.3 核对数据是否入库

v4 的自托管默认跑在 `events_only` 模式，数据落在 ClickHouse 的 `events_full` 表。
运行环境：Git Bash，容器名为 `langfuse-clickhouse-1`。

```bash
docker exec langfuse-clickhouse-1 clickhouse-client --query \
  "SELECT name, type, level FROM events_full ORDER BY start_time DESC LIMIT 5"
```

正文边界的验证方法是**受控检索**：一个预期命中的字符串、一个预期不命中的字符串，
两条结果一起看才有说服力。

```bash
# 预期命中：错误 span 的 status_message
docker exec langfuse-clickhouse-1 clickhouse-client --query \
  "SELECT count() FROM events_full WHERE status_message != ''"

# 预期不命中：任何提示词正文
docker exec langfuse-clickhouse-1 clickhouse-client --query \
  "SELECT count() FROM events_full WHERE position(toString(attributes), '这段提示词') > 0"
```

注意 `events_core` 只有一个 `t` 列，对它用 `toString(t.*)` 会被 ClickHouse 以
`UNKNOWN_IDENTIFIER` 拒绝；要检索字段就查 `events_full`。

## 5 v4 API 要点与 v2/v3 废弃写法对照

### 5.1 已确认的行为

本节结论均在本机自托管实例（`langfuse/langfuse:4`，实测版本 `4.38.0`）上验证：

| 项 | 结论 |
| --- | --- |
| 读取观测的端点 | `GET /api/public/v2/observations`，返回 200。 |
| 旧 trace 端点 | `GET /api/public/traces` 返回 **404**，v4 的 `events_only` 模式不再提供。 |
| observation 的 `type` | 大写：`SPAN` / `RETRIEVER` / `GENERATION` / `TOOL` / `CHAIN`。 |
| observation 的 `level` | 大写：`DEFAULT` / `ERROR` 等。 |
| `events_full` 的列 | **不含** `input` / `output`，正文边界在该表上无法用列是否存在来证明。 |
| 健康检查 | web：`/api/public/health`（可加 `?failIfDatabaseUnavailable=true`）、`/api/public/ready`；worker：`/api/health`。 |
| 鉴权 | HTTP Basic，用户名为 public key、密码为 secret key。 |

### 5.2 写法对照

| 场景 | v2 / v3 写法 | v4 写法 |
| --- | --- | --- |
| 拉取 trace 列表 | `GET /api/public/traces` | 改用 `GET /api/public/v2/observations`，按 `traceId` 聚合 |
| 按 id 取单条 trace | `GET /api/public/traces/{id}` | 同上，用查询参数筛选 |
| 父子层级 | 由记录里的父标识事后拼装 | 由 OTel 上下文推导，SDK 的上下文管理器手动进出并压栈 |
| trace 与 observation 的关系 | trace 是独立实体，另有 observations 子资源 | 根 span 即 trace，子 span 通过父标识挂上去 |
| 内容字段 | `input` / `output` 作为常规列存在 | 仅在显式传参时上传，`events_full` 里没有这两列 |

### 5.3 本层的实现选择

`LangfuseBackend` 把 SDK 的上下文管理器**手动进出**并与 span 标识配对压栈：进入时
调 `__enter__`，结束时先 `update` 再 `__exit__`。这样层级由 OTel 上下文自然推导，
与本地后端「结束时追加一行」在语义上等价。

如果改成「只在 span 结束时投递一条记录」，就只能改用显式的 `trace_context` 指父，
而官方明确提示：父 observation 若尚未被服务端收到，子会被挂到 trace 根上。span 的
结束顺序天生是「子先父后」，所以那条路不可靠。

栈深上限 `MAX_STACK_DEPTH = 256`：正常一条 trace 只有十几个 span，触到上限说明存在
未配对的进出（通常是异常路径漏了出口），此时继续压栈会连 OTel 上下文一起泄漏，
因此超限时停止记录并告警。

## 6 埋点落点表

「哪些代码在哪些位置开了 span」这张表是排查的起点：trace 里缺了一类 span，先来这里
对一下，就知道是没插桩还是插了没生效。

| 落点 | 位置 | 打开的 span | 说明 |
| --- | --- | --- | --- |
| 服务：问答请求 | `routes/rag.py:243` | `trace` `rag.answer` | 根 span。属性带 `request_id` 与 `top_k`。 |
| 服务：模型调用 | `lifespan.py:172` | `generation` `generate` | `traced_chat` 包住服务的 `chat_fn`，凡走它的调用**自动**有 span，调用方无需重复包装。 |
| 服务：路由层检索 | `routes/rag.py:88` | `retriever` `retrieve` | 包在 `JwipcKnowledgeRAG` 外层。 |
| 服务：路由层生成 | `routes/rag.py:114` | `generation` `generate` | 同上，包在生成器外层。 |
| 服务：工作流检索 | `lifespan.py:377` | `retriever` `retrieve` | 装配时包 `deps.rag` 再交给图，理由见 6.1。 |
| 服务：工作流节点 | `routes/workflow.py:168` | `chain` `<节点名>` + `generation` | 经 `ObservabilityCallbackHandler` 注册到 `config["callbacks"]`，一层 `chain` 对一个节点。 |
| 服务：工具调用 | `routes/tools.py:74` | `tool` `tool_<name>` | 属性含 `denied` / `is_error` / `arg_keys`，被拒的调用也留痕。 |
| 评测：用例级 | `scripts/eval_week10_run.py:418` | `trace` `eval.<scenario>` | 每条用例一条独立 trace，属性带用例 id 与分组。 |
| 评测：知识问答 | `scripts/eval_week10_run.py:271` | `retriever` `retrieve` | 评测直接用 `deps.rag`，外层的包装要自己叠——`deps.rag` 自身不带埋点。 |
| 评测：需求分析 | `scripts/eval_week10_run.py:240` / `:308` | `retriever` + `chain` × 6 + `generation` | 与服务工作流同一套装配。 |
| 评测：工具调用 | `scripts/eval_week10_run.py:365` | `tool` `tool_<name>` | 不经传输层，直接调 MCP 服务实例。 |
| 冒烟脚本 | `scripts/observability_week10_smoke.py` | 五类 span 各一条 | 专门用来验证后端接线，不依赖业务路由。 |

### 6.1 检索埋点为什么要挂在 `retrieve()` 所在的那一层

RAG 层的四个检索器（`ListRetriever` / `QdrantRetriever` / `HybridRetriever` /
`RerankRetriever`）**统一只提供 `search()`**，没有 `retrieve()`；而 `TracedRetriever`
只实现了 `retrieve()`，其余属性经 `__getattr__` 透传。于是：

- 包在**检索器内层**时，知识库取的是 `search`，正好绕过包装——那一层**从不产出 span**；
- 包在**知识库外层**（`JwipcKnowledgeRAG` 有 `retrieve`）时才会生效。

`lifespan` 起初用的是前者，表现是「RAG 路由看得到 `retriever` span、工作流看不到」——
因为 `routes/rag.py` 恰好在知识库外层又包了一层，把内层的失效掩盖了。现在统一为
「挂在 `retrieve()` 存在的那一层」，并定下一条约定：

> `deps.rag` 自身不带埋点。**任何新增的、直接用它的调用方都必须自己在外层包一层
> `traced_retriever`**，否则该调用链在 trace 里就少了检索这一环。

验证方式：需求分析的一条完整 trace 里应当同时有 `retriever` 与 `chain`。

```bash
# Git Bash，项目根；打印某条 trace 的 span 类型构成
uv run python scripts/trace_week10_view.py --trace-id 4058df044249464793856c29c5600047 --dir logs/eval/traces
```

修正前同一条 trace 是 11 个 span、没有 `retriever`；修正后是 12 个，多出的一个挂在
`chain rag_retrieve` 下面。

### 6.2 工具 span 的属性要先去返回对象里「找对那一层」

`tool` span 的 `denied` / `is_error` 两个属性不是调用方传进来的，而是观测层从**被包调用
的返回值**里判出来的。问题在于同一件事有三种返回形状：

| 调用方 | 返回形状 |
| --- | --- |
| MCP 客户端会话（服务路由） | 对象，失败标志是驼峰 `isError` |
| 进程内 `FastMCP.call_tool`（评测脚本） | `(content, structured)` 二元组，业务字段在第二个元素 |
| 部分实现 | 业务字段在 `structuredContent` |

只按第一种写（直接 `getattr`）时，另外两种会**静默取不到值**：评测跑出来的 `tool` span
一律是 `denied=false, is_error=false`，工具被拦住这件事在 trace 上凭空消失，而报告里
那条用例仍显示 `denied` —— 因为报告走的是评测层另一条判定路径。现在观测层统一先规约到
带业务字段的那一层（`_tool_payload`）再判。

另有一处口径：沙箱表达拒绝用的是 `ok=false` 配一个类别词（`forbidden` / `bad_path` /
`argument_rejected`），不额外给 `denied` 字段，因此词表也要认；且**拒绝优先于失败**——
同一次调用若同时被判成 `denied` 与 `is_error`，「拒绝率」会被失败率吃掉。词表与评测层的
`DENY_KINDS` 同源，但观测层不能反向依赖评测层（`evaluation.rubric` 已经依赖观测层），
故在 `instrument.py` 里单列一份，改动时两边要一起改。

## 7 本地视图脚本与截图

`scripts/trace_week10_view.py` 把 JSONL 渲染成**自包含 HTML**（样式内联，无外部请求），
需要时用本机 Edge 的 `--headless` 截图，不额外安装浏览器自动化工具。

```bash
# Git Bash，项目根；列出最近 20 条（时间升序，最后一行即最新）
uv run python scripts/trace_week10_view.py --list --dir logs/eval/traces
```

```bash
# Git Bash，项目根；不写 --trace-id 即渲染最新一条
uv run python scripts/trace_week10_view.py --dir logs/eval/traces --out logs/eval/view.html
```

```bash
# Git Bash，项目根；指定某一条时用变量传 id，不要把尖括号原样粘进终端
export TRACE_ID=364525400d1c482093907137d3ce4c91
uv run python scripts/trace_week10_view.py --trace-id "$TRACE_ID" --dir logs/eval/traces --out logs/eval/view.html
```

```bash
# Git Bash，项目根；渲染并截图（截图路径落到文档用的图片目录）
uv run python scripts/trace_week10_view.py --trace-id "$TRACE_ID" --dir logs/eval/traces --out logs/eval/view.html --screenshot docs/poho/week10/d48_trace_rag.png
```

尖括号在 bash 里是重定向符号，`--trace-id <trace_id>` 会被解析为「从名为 `trace_id`
的文件读取标准输入」，报的是 `No such file or directory`，与脚本无关。

脚本对输入很宽容：坏行跳过、字段缺失走默认值、断链的 `parent_id` 提级为根、找不到父的
孤儿 span 也照常渲染。唯一的硬要求是每行带 `trace_id`。因此它挂在**任何**兼容结构的
JSONL 上都能用，包括 Langfuse 导出或自己手写的记录。

### 7.1 什么时候必须用脚本，什么时候用网页版

后端是**降级链**（见 1.2）：一个进程只启用一个后端。`OBS_BACKEND=langfuse` 时 JSONL
不写，`OBS_BACKEND=local` 时不上报——所以两条路径看到的不是「同一份数据、两个入口」，
而是**同一批请求的两份记录**；要说明两条路径都可用，就得各自有一条能打开看的记录
（本周实际产出见 7.2）。这条也解释了为什么评测脚本会强制把后端固定为 `local`：它要能在
没有 Langfuse 的机器上跑完，并让报告里的 `trace_id` 直接可打开。

网页版能做而脚本做不到的只有一件事：**Session 视图**（把多条 trace 按会话聚合）。

### 7.2 本周实际产出的截图

| 文件 | 取自 | 能看出什么 |
| --- | --- | --- |
| `poho/week10/d48_trace_rag.png` | 知识问答 | 模型名 `qwen3`、`top1_score` / `hit_count` / `source_count`（检索来源的规模）、每条 span 的耗时 |
| `poho/week10/d48_trace_workflow.png` | 需求分析（走完六个节点） | 12 个 span 的父子链与节点名（`classify` / `functional_points` / `rag_retrieve` / `risk` / `test_points` / `report`）、`chain rag_retrieve` 下挂着 `retriever` |
| `poho/week10/d48_trace_tool_denied.png` | 工具调用（被拒的一条） | `group=denied`、`denied=true` 与 `is_error=false` 分开记录、入参只有 `path` |

需求分析那一组的节点跑的是确定性替身，所以它的 `generation` span 上模型名写作 `fake`，
这是设计如此（见评测报告的已知问题第 3 条），不是采集缺失。

三条都取自**本地 JSONL 路径**：评测脚本会把后端强制固定为 `local`，因此这批请求不在
Langfuse 里。Langfuse 路径的 trace 视图与 Session 视图截图本轮**未产出**，原因是 UI 需要
交互式登录会话，而本机安全策略不允许安装浏览器自动化工具，`--headless` 无法完成带会话的
截图。路径本身是通的，可用下面的命令证明库里确实有数据：

```bash
# Git Bash，项目根；v4 部署改用 v2 观测端点，旧的 /api/public/traces 已 404
PUB=$(grep '^LANGFUSE_PUBLIC_KEY=' .env | cut -d= -f2)
SEC=$(grep '^LANGFUSE_SECRET_KEY=' .env | cut -d= -f2)
curl -sS --noproxy '*' -u "$PUB:$SEC" 'http://localhost:3000/api/public/v2/observations?limit=1'
```

返回 `{"data":[...],"meta":{...}}` 即说明数据在库且可读。

## 8 三种接入方式与前置条件

| 方式 | 前置条件 | 数据落到哪 | 适合什么场景 |
| --- | --- | --- | --- |
| 本地 JSONL | 无外部依赖。`OBS_BACKEND` 缺省即 `local`，可用 `OBS_TRACE_DIR` 改目录。 | `logs/traces/traces-<日期>.jsonl` | CI、离线、单机复核。脚本渲染即可看 |
| Langfuse 自托管 | Docker 可用；`deploy/langfuse` 六个容器起来；`OBS_BACKEND=langfuse`；`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` 齐全且初始化成功。 | 本机 ClickHouse 的 `events_full`（v4 为 `events_only` 模式） | 想看 Session 视图、想用 UI 对比多次请求 |
| Langfuse Cloud | 公网可达；云端项目凭据；`LANGFUSE_BASE_URL` 指向云端地址。 | 云端 | 多人共享、跨机器查看。注意正文默认不上传 |

三种方式**互斥**：同一时刻只有被选中的那条路径有数据。因此「本地截图 + Langfuse 截图」
必须来自两次独立的请求，不能指望同一次请求两边都有。

自托管的容器起停、密钥体系与排障见 `deploy/langfuse/README.md`；核对数据是否真的进了
ClickHouse 见 4.3。

## 9 相关文件

| 位置 | 内容 |
| --- | --- |
| `src/observability/config.py` | `ObservabilityConfig`、`describe()` |
| `src/observability/models.py` | `SpanRecord`、`SPAN_KINDS`、token 与成本计算 |
| `src/observability/context.py` | `contextvars` 承载的当前 trace / span 上下文 |
| `src/observability/tracer.py` | `Tracer` 门面与 `build_tracer()` |
| `src/observability/pricing.py` | 单价表与成本估算 |
| `src/observability/instrument.py` | `traced_chat` / `traced_retriever` / `traced_tool` 三个包装器，落点表的实现都在这里 |
| `src/observability/callbacks.py` | `ObservabilityCallbackHandler`：把 LangChain 回调事件翻成 `chain` / `generation` span |
| `src/observability/backends/` | `base.py`（后端接口与 `InMemoryBackend`）、`jsonl.py`、`langfuse.py` 与 `__init__.py` 里的后端工厂 |
| `deploy/langfuse/` | 自托管 Langfuse 的 compose 编排、`.env` 模板与部署说明 |
| `scripts/observability_week10_smoke.py` | 端到端冒烟：写 trace 并回读校验 |
| `scripts/trace_week10_view.py` | 把 JSONL 渲染成自包含 HTML，并可用本机 Edge 无头截图 |
| `scripts/eval_week10_build.py` | 生成并自检 52 条评测数据集 |
| `scripts/eval_week10_run.py` | 跑数据集、算指标、出报告 |
| `scripts/eval_week10_compare.py` | 多组结果并排对比，逐条列出变差用例 |
| `src/evaluation/` | 评测层：数据集、指标、结果口径、Rubric、报告 |
| `data/golden/week10_golden.jsonl` | 冻结的 52 条评测数据集 |
| `tests/observability/` | 配置、追踪器、JSONL 后端、Langfuse 后端四组单测 |
| `tests/evaluation/` | 数据集、指标、报告与对比脚本四组单测 |
| `docs/week10_evaluation.md` | 数据集构成、指标口径、执行方式与实跑结果 |
| `docs/week10_eval_report.md` | 三组参数对比的数字与变差用例清单 |
