# Week 9 工程测试报告

> 覆盖四类工程测试（并发 / 超时 / 重启 / 持久化）与静态门禁，每条结论都附执行命令与原始输出要点。
> 测试日期：2026-09-17。所有命令在 **Git Bash**（Windows，MINGW64）执行，工作目录 `/d/workspace/py_ai/week01_ai_basics`。

---

## 1. 结论速览

| 测试类别 | 脚本 | 断言数 | 结果 | 关键数字 |
| --- | --- | --- | --- | --- |
| 静态门禁 | `ruff` + `pytest` | — | 通过 | `ruff check` 0 问题；`pytest` **462 passed, 1 skipped** |
| 接口验收（8 分组） | `service_week9_acceptance.py` | — | 通过 | 真实 HTTP，覆盖 probe/rag/idempotency/stream/workflow/tools/envelope/security |
| 入站防护 | 同上（security 组） | 5 | 通过 | 401 / 413 / 429 全部命中，`Retry-After=20` |
| 并发 | `service_week9_load.py` | 8 | 通过 | 20 并发 10.5~18.3 req/s / p95 1104~2456ms；50 并发 25.7~44.5 req/s；**两档均无 5xx** |
| 超时 | 同上（timeout 组） | 5 | 通过 | 504 `timeout`，`detail=request_timeout=2.0s`，`errors_by_code.timeout=1` |
| 重启 | `service_week9_compose_check.py` | 6 | 通过 | `restart api` 后 `/ready` 200；幂等缓存跨重启可回放 |
| 持久化 | 同上（persistence 组） | 6 | 通过 | `down` 不删卷 → `up` 后 `points_count=51` 一致，仍能召回 |

合计：容器检查 **12 项断言全部通过**；压测 **13 项断言全部通过**。

---

## 2. 静态门禁

```bash
# Git Bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

输出要点：

```
All checks passed!
129 files already formatted
462 passed, 1 skipped in 15.81s
```

唯一 skip 为 `tests/test_mcp_security.py:57`（当前环境不允许创建符号链接），属既有跳过项，与本周改动无关。

测试基线变化：本周起点 374 passed + 1 skipped，终点 **462 passed + 1 skipped**（超出计划目标 405）。
新增 88 条中，本周四新增 `tests/agent_service/test_service_metrics.py` 贡献 **25 条**。

---

## 3. 并发测试

### 3.1 执行命令

```bash
# Git Bash：20 与 50 两档，每档 40 个请求
# --min-score 1.0 强制低分拒答，只压检索与并发闸门，绕开 CPU 推理
uv run python scripts/service_week9_load.py --levels 20,50 --requests 40 --min-score 1.0
```

关于两个参数的取舍：

- `--min-score 1.0`：本机真实模型在 CPU 上单次生成数十秒，40 个请求串起来会跑几十分钟且结果被模型推理噪声主导。设为 1.0 让生成链路走拒答分支（不调模型），测得的才是**闸门与调度**的能力。
- `--requests 40`：为最高并发（20）的 2 倍，保证「排队—放行—再排队」至少发生两轮，否则测不出排队行为。

### 3.2 原始输出要点

```
[INFO] 档位=[20, 50] 每级请求数=40 min_score=1.0 闸门=8
== load ==
[PASS] 20 并发 · 全部收到响应       40 个
[PASS] 20 并发 · 无 5xx        逐个请求均非 5xx
[PASS] 20 并发 · 限流只出现在超配额场景  429x0/40 属闸门=8 的排队保护，仍拿到 40 个 200
[PASS] 20 并发 · 吞吐与延迟        吞吐=10.5 req/s p50=1365ms p95=2456ms max=2597ms | 200x40
[PASS] 50 并发 · 全部收到响应       40 个
[PASS] 50 并发 · 无 5xx        逐个请求均非 5xx
[PASS] 50 并发 · 限流只出现在超配额场景  429x16/40 属闸门=8 的排队保护，仍拿到 24 个 200
[PASS] 50 并发 · 吞吐与延迟        吞吐=25.7 req/s p50=1258ms p95=1445ms max=1449ms | 200x24 429x16
```

### 3.3 数字解读

| 档位 | 吞吐 | p50 | p95 | max | 状态码分布 |
| --- | --- | --- | --- | --- | --- |
| 20 并发 | 10.5 ~ 18.3 req/s | 1009 ~ 1365ms | 1104 ~ 2456ms | 1156 ~ 2597ms | `200x40` |
| 50 并发 | 25.7 ~ 44.5 req/s | 766 ~ 1258ms | 801 ~ 1445ms | 802 ~ 1449ms | `200x24 429x16` ~ `200x15 429x25` |

表中给出的是同一命令多次运行的**区间**，不是单点值。同一台机器上重跑同一命令，
吞吐与延迟会有明显浮动（本机为开发机，Docker Desktop、VS Code 等常驻进程会争抢
CPU，而 `qwen3` 的检索与 embedding 也在同机跑）。因此：

- **可对外引用的结论是稳定性而非绝对值**：两档均**无 5xx**、20 档**全部 200**、
  429 **只出现在超配额档**——这三条每次运行都成立。
- **吞吐/延迟绝对值只在同一次运行内横向可比**（例如同一轮里 20 档 vs 50 档），
  不宜跨轮次或跨机器比较。

三个值得注意的现象：

1. **50 并发吞吐反而更高**（每次运行都是如此）。原因是 50 并发档有相当比例的请求
   在闸门处被立即拒绝（429），它们不占用处理时间却计入吞吐，因此吞吐被抬高。
   **这个数字不能当作服务处理能力**，真正的处理能力由 20 并发档（全部 200）代表。
2. **50 并发档的 p95 反而更低**，同样是因为被限流的请求很快就返回了。只有 20
   并发档的延迟数字是可对外引用的容量指标。
3. **429 出现在 50 档是设计行为**：闸门容量默认 8，50 并发时必然有请求排队超时
   （`queue_timeout_seconds=5.0`）被拒。20 档有部分并发被排队，但 5 秒内能被放行，
   因此没有 429。

被限流的比例在两次运行间从 62.5% 降到 40%，同样属机器负载波动——这进一步说明
该档的比例本身不是稳定指标，判定「限流只在超配额档出现」比盯住具体比例更有意义。

### 3.4 断言口径说明

「限流只出现在超配额场景」这条断言按闸门容量分支判定，而不是简单地允许或禁止 429：

- 并发 **≤** 闸门容量却出现 429 → **FAIL**（限流口径与服务配置不一致，是缺陷）；
- 并发 **>** 闸门容量 → 429 属排队保护，但若**全部**被限流也判 FAIL（该档未观测到任何有效吞吐，数字没有解释力）。

该断言的告警能力已实测验证：把 `--max-concurrency` 故意声明为 100（与服务端实际的 8 不符）后：

```
[FAIL] 50 并发 · 限流只出现在超配额场景  并发 50 ≤ 闸门 100 却出现 15 个 429
工程测试未通过：1 项失败，0 项跳过
```

---

## 4. 超时测试

### 4.1 设计

在进程内注入一个「睡眠 5 秒」的假模型（`SlowChat`），同时把服务总时限压到 2 秒
（`request_timeout_seconds=2.0`），使 `504` 必定触发。用进程内替身而非真实模型，
是为了让超时**确定发生**，而不是碰运气等模型慢下来。

### 4.2 原始输出要点

```
== 慢模型 -> 2s 超时 ==
[PASS] 504 timeout          detail=request_timeout=2.0s
[PASS] 超时后服务存活              /ready=200
[PASS] 超时计入 errors_by_code  timeout=1
[PASS] 504 计入状态码分布          requests_by_status={'504': 1}
[PASS] 超时前已进入模型调用           chat 调用次数=1
```

### 4.3 五条断言的用意

| 断言 | 验证的问题 |
| --- | --- |
| 504 且 `code=timeout` | 超时映射到正确错误码，客户端能区分「超时」与「依赖不可用」 |
| `detail=request_timeout=2.0s` | 错误体带出配置值，运维不必翻配置就能确认用的是哪一档时限 |
| 超时后 `/ready` 仍 200 | 超时被正确回收，没有把服务带崩或泄漏闸门槽位 |
| `errors_by_code.timeout=1` | 超时进了错误码分布，可从 `/metrics-summary` 观测 |
| `requests_by_status` 含 504 | 超时进了状态码分布，与错误码是两条独立的观测路径 |
| `chat` 调用次数 = 1 | 超时发生在**模型调用之后**，说明时限覆盖了上游调用，而不是只包住了本地逻辑 |

---

## 5. 重启测试

### 5.1 执行命令

```bash
# Git Bash：容器已在跑，跳过 up 直接测
uv run python scripts/service_week9_compose_check.py --skip-up
```

不加 `--skip-up` 时脚本会自行 `docker compose up -d` 并等待就绪。

### 5.2 原始输出要点

```
== restart ==
[PASS] 重启前写入幂等缓存                   status=200 replayed=true
[PASS] 重启前 /ready                  points_count=51 session_store=dir=/app/data/agent_service_sessions sessions=0
[PASS] docker compose restart api  Container week01_ai_basics-api-1 Restarting   Container week01_ai_basics-api-1 Started
[PASS] 重启后 /ready 恢复 200           dependencies 完整
[PASS] 会话目录未变                      dir=/app/data/agent_service_sessions sessions=0
[PASS] 重启后幂等缓存可回放                  status=200 replayed=true
```

### 5.3 断言的用意

关键在最后一条：**幂等键在重启前后复用同一个值**。若函数内部重新生成键，
重启后测到的就是两次互不相关的请求，缓存是否落盘根本测不出来。用同一个键命中，
才证明条目确实从具名卷上恢复，而非残留在进程内存里。

「会话目录未变」比对的是 `session_store` 的明细文字（含目录与条目数），
确认重启没有把落盘路径换掉（例如退化成容器内临时目录）。

### 5.4 实测恢复时间

`docker compose restart api` 后 `/ready` 在 3 秒内恢复 200（手工验证）：

```bash
# Git Bash
docker compose restart api
for i in $(seq 1 40); do
  sleep 3
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/ready --noproxy '*' --max-time 5)
  echo "第${i}次 /ready=$code"
  [ "$code" = "200" ] && break
done
# 第1次 /ready=200
```

脚本里的 `READY_TIMEOUT_SECONDS=180` 是为首次冷启动（Qdrant 健康检查 + API 连模型）
留的余量，重启场景远用不到。

---

## 6. 持久化测试

### 6.1 原始输出要点

```
== persistence ==
[PASS] docker compose down         未删卷（无 -v）
[PASS] down 后无残留服务                 compose ps 为空
[PASS] docker compose up -d        ok
[PASS] up 后 /ready 恢复 200          依赖明细完整
[PASS] points_count 与重启前一致         points_count=51
[PASS] down/up 后仍能召回               citations=1 confidence=0.9
```

### 6.2 判据为什么是 `points_count`

`/ready` 的 `qdrant` 明细里带有 `points_count=51`。这是「卷是否还在」最直接的
可核验数字：

- `down` 带 `-v` 会删卷 → 集合消失 → `points_count` 归零或 `/ready` 直接 503；
- `down` 不带 `-v` → 卷保留 → `up` 后 `points_count` 与重启前**逐位相同**。

脚本把 `down` 一律实现为不带 `-v`，并把该语义写进参数名（`compose_stop(remove=True)`）
而不是靠调用方记得。

最后一条「仍能召回」补上了端到端验证：数字一致只说明计数没丢，真正跑一次
RAG 问答拿到 `citations=1` 才说明向量与 payload 都可读可用。

---

## 7. 排障记录：四个真实缺陷

本节记录测试过程中定位到的三个问题，它们都由脚本先报错、再定位、最后修复。

### 7.1 环境代理劫持本机请求（502 / ConnectionReset 10054）

**症状**：50 并发档出现 `502` 与 `WinError 10054` 连接重置，初看像服务在高并发下崩溃。

**定位**：先排除服务侧——`netstat` 确认 8080 有监听、无残留 python 进程、`/health` 正常。
再查环境变量，发现本机存在：

```
HTTP_PROXY=http://127.0.0.1:57348
HTTPS_PROXY=http://127.0.0.1:57348
```

`httpx` 默认 `trust_env=True`，会把发往 `127.0.0.1:8080` 的请求交给这个本地代理，
代理无法正确转发导致 502 或断连。**请求根本没到服务**。

**修复**：三个脚本的 `httpx` 客户端全部显式设置 `trust_env=False`
（`service_week9_load.py`、`service_week9_acceptance.py`、`service_week9_compose_check.py`）。
`curl` 侧的对应做法是加 `--noproxy '*'`。

### 7.2 幂等回放测试的键未在重启前后复用

**症状**：重启组的「重启后幂等缓存可回放」偶发失败，而手工验证缓存明明有效。

**定位**：`idempotency_roundtrip()` 早期版本在函数内部固定生成键字符串，且忽略
首次写入的返回码——首次写入若失败（例如 500），缓存里根本没有条目，回放必然不命中，
但结论里只看到一个孤立的 `replayed=None`，看不出根因。

**修复**：函数签名改为接收 `key` 参数，由调用方在两组用例间共享同一个键；
同时检查首次写入的状态码，非 200 时把状态码与 `error.code` 一并写进结论。

### 7.3 测试脚本停掉了被测服务自己的依赖（最隐蔽）

**症状**：`--skip-up` 模式下，重启组稳定失败——「写入幂等缓存 status=500 code=internal」，
之后「重启后 /ready 超时未恢复（未就绪=['qdrant']）」，但持久化组却全部通过。

**定位**：脚本原有的端口冲突处理是「停掉任何发布 6333 的容器」。而 `--skip-up` 场景下
栈已经在跑，`docker ps` 里同时有：

- 本机常驻的 `qdrant_server`（本地工作流用，确实需要停）；
- compose 自己的 `week01_ai_basics-qdrant-1`（**被测服务的后端**，不该停）。

脚本把后者一起停了，等于自己拔掉了被测服务的电源。持久化组通过是因为它随后执行了
`docker compose up -d`，顺手把 qdrant 又拉了起来——这个「意外自愈」掩盖了问题，
直到加上 `_LAST_READY_HINT` 把依赖明细打进失败结论才看出真相。

**修复**：新增 `_external_container_names()`，按 compose 项目名与容器名前缀
（`<project>-`）双重判断，只停**不属于本 compose 项目**的容器。

顺带补上诊断增强：`wait_until_ready` 超时时记录最后一次响应的依赖明细，
失败结论从「超时未恢复」细化为「超时未恢复（status=503 未就绪=['qdrant']）」。
这个改动是发现 7.3 的关键。

### 7.4 验收脚本连到了容器而非自建服务（本轮新发现）

**症状**：`--only security --api-key s3cret --rate-limit 3 --max-body-bytes 4096`
稳定报三项失败，且失败信息互相矛盾：

```
[FAIL] 未授权拒绝          疑似防护失效：status=422 code=invalid_argument
[FAIL] 超限请求体 -> 413   status=422 code=invalid_argument
[FAIL] 配额耗尽 -> 429     客户端读超时（30s）
```

这三条恰好覆盖认证、体积上限、配额三个方向，看起来像「安全加固整体没生效」。

**定位过程**：在隔离脚本里复刻同样的环境注入与请求头合并逻辑，却能得到正确的
`401 unauthorized` / `422 invalid_argument`——说明服务端与脚本逻辑都没错，问题
在**实跑时连到了谁**。查端口占用后真相明了：

```bash
netstat -ano | grep ":8080"
#   TCP 0.0.0.0:8080  LISTENING  32572   ← com.docker.backend.exe
#   TCP [::1]:8080    LISTENING  30928   ← wslrelay.exe
docker ps --format '{{.Names}}\t{{.Ports}}'
#   week01_ai_basics-api-1  Up (healthy)  0.0.0.0:8080->8080/tcp
```

上一轮 compose 验证留下的 `api` 容器仍发布着 8080。脚本新起的子进程因端口冲突
启动失败，而就绪轮询恰好能从**那个容器**拿到 `/health` 的 200，于是整轮断言都打
在容器的缺省配置上：

| 断言期望的配置 | 容器实际生效 |
| --- | --- |
| `AGENT_SERVICE_API_KEY=s3cret` | 未设置 → **认证关闭** |
| `AGENT_SERVICE_MAX_BODY_BYTES=4096` | 未设置 → 缺省 262144 |
| `AGENT_SERVICE_RATE_LIMIT_PER_MINUTE=3` | 未设置 → 缺省 60 |

三条失败因此全部有了合理解释：认证没开 → 匿名请求得到 422 而非 401；体上限是
262144 → 4160 字节的载荷根本没超限，反而先被 Pydantic 的 `max_length=4000`
拦成 422；配额是 60 → 循环里那几次请求耗不光，而合法 RAG 请求在真实 qwen3（CPU）
上要跑约 50 秒，被 30 秒客户端超时打断。

**修复**（两处，均为「让错误可诊断」而非「绕过错误」）：

1. `ServerProcess.ensure_port_free()`：自建子进程**之前**先查端口，
   被占用即抛出 `PortInUseError` 并以退出码 2 终止，报错信息带上占用者进程名与
   释放方法。这挡住了「子进程起不来却照样往下跑」这条路径。
2. `warn_mismatched_target()`：`--no-spawn` 模式下无法控制外部服务配置，
   于是发一个缺必填字段的小请求探认证口径，不一致时直接给出
   「很可能连到了仍以缺省配置运行的旧服务或容器」的结论。

**顺带修掉一个真问题**：429 用例原先发**合法** RAG 请求体，第一次请求就会撞上
30 秒客户端超时——而 `enforce_rate_limit` 是路由依赖、在请求体解析之前执行，
因此改用非法小请求体后前 N 次几毫秒返回 422、第 N+1 次被限流，几秒内跑完。

**修复后实测**：

```
[PASS] 探针免认证 /health  status=200
[PASS] 探针免认证 /ready   status=200
[PASS] 未授权拒绝          401 unauthorized
[PASS] 超限请求体 -> 413   limit=4096
[PASS] 配额耗尽 -> 429     第 4 次被限流，Retry-After=20

验收全部通过：5 项        # 退出码 0
```

**留下的教训**：`/health` 只答进程存活，**不能**用来判断「现在监听这个端口的是
我刚起的进程」。任何自建子进程的脚本都该先确认端口空闲，否则会静默测错对象。

**同一问题在压测脚本中同样存在**：`service_week9_load.py` 也自建 `ServerProcess`
并只等 `/health`，因此已被一并修复（它复用 acceptance 脚本导出的
`PortInUseError`，检查逻辑不重复实现）。实测把端口指向一个已占用的 8081：

```
[FAIL] 端口 127.0.0.1:8081 已被占用（python.exe(pid=34280)）。请先释放：...
退出码 = 2
```

对压测脚本来说这一点尤其重要：如果连到的是配置不同的旧服务，闸门容量与配额
都不是本次打算测的那套，**并发档位的全部结论都会失真**，而且没有任何报错。

---

## 8. 未解决的问题

### 8.1 真实模型下的工作流超时（已知，未解决）

`qwen3` 在 CPU 上单节点推理约 79 秒，工作流 7 个节点串联，在
`AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS=120` 下必然 `504`。

**当前处置**：承认并记录。本周的并发与超时测试都用假模型或低分拒答绕开真实推理，
因此结论仍然有效——测的是服务层的闸门、时限与错误映射，不是模型速度。

**后续方向**：把超时按路由分级（RAG 120s / 工作流约 600s），拆成两个配置键。
口径已定，尚未实现。

### 8.2 真实模型对电商需求的分类跑偏（待排查）

`"开发一个电商网站，包含商品浏览、购物车与支付功能。"` 被判为 `other`、置信度 0.30，
而更模糊的 `"帮我做个东西"` 反而跑完了 7 个节点。假模型对前者给出
`category=web` / `conf≈0.9`，因此疑点在真实模型的提示词或 JSON 解析，而非链路本身。
本周未列入必做项，留待后续。

### 8.3 50 并发档的吞吐数字不可对外引用

如 3.3 所述，50 并发档有 62.5% 的请求被限流，其吞吐与延迟数字都被「快速拒绝」扭曲。
若需要真正的容量数字，应在 `AGENT_SERVICE_MAX_CONCURRENCY` 调高后重测，或改用
低于闸门容量的多个档位（如 2/4/8）描绘容量曲线。

---

## 9. 复现全部结论的命令清单

```bash
# Git Bash，工作目录 /d/workspace/py_ai/week01_ai_basics

# 1) 静态门禁
uv run ruff check .
uv run ruff format --check .
uv run pytest -q

# 2) 接口验收（8 分组，自建子进程，真实 HTTP）
#    会先检查端口空闲；若 compose 容器占着 8080，先 docker compose down
uv run python scripts/service_week9_acceptance.py

# 2.1) 入站防护专项（401 / 413 / 429，5 项断言）
uv run python scripts/service_week9_acceptance.py \
    --only security --api-key s3cret --rate-limit 3 --max-body-bytes 4096

# 3) 并发 + 超时（13 项断言）
uv run python scripts/service_week9_load.py --levels 20,50 --requests 40 --min-score 1.0

# 4) 重启 + 持久化（12 项断言）
#    注意：会短暂停掉本机 qdrant_server，结束后按提示 docker start qdrant_server 还原
docker stop qdrant_server
docker compose up -d --build
uv run python scripts/service_week9_compose_check.py --skip-up

# 5) 导出服务日志
docker compose logs --no-color api > logs/service_week9.log
```
