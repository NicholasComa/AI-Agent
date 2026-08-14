# Week 4 · Day 20：Dify 版 vs 代码版 对比报告

> 适用项目：南宁软件组 AI Agent 12 周 Roadmap · Week 4（Dify Workflow + FastAPI 包装）
> 仓库：`D:\workspace\py_ai\week01_ai_basics\`
> 落档日期：2026-08-14
> 关联文档：`docs/week03_summary.md`（代码版文档）、`docs/day17_dify_repro_design.md`、`docs/day18_task_summary.md`、`docs/day19_task_summary.md`

---

## 1. 背景与目标

Week 3 我们用 LangChain v1 `create_agent` 写出了代码版 `DevAssistantAgent`（`src/devagent/`），提供 `calculator` / `read_text_file` / `check_commit_message` 三个工具，并带追踪中间件与递归上限控制。Week 4 我们在 Dify 中复现了它的一个简化场景（`DevAssistantAgent_Dify` 工作流），又用 FastAPI 把 Dify 已发布工作流包装成通用透传接口 `POST /dify/run`（Day 19）。

本报告把**两个版本**放在同一张桌子上横向比较，覆盖=四个维度——调试、版本管理、扩展、部署——并补充第五个维度「能力边界」（Day 19 计划已列）。目的是厘清"低代码原型"与"代码优先"各自的优缺点。

> 对照（截至 2026-08-14）：
> - **代码版**：`src/devagent/`，仅用 `FakeToolCapableChatModel`（假模型）跑过 pytest，未接真实模型、无 HTTP 入口（见 `week03_summary.md` §8）。
> - **Dify 版**：`dify_workflows/DevAssistantAgent_Dify.yml` + `src/app.py` 的 `POST /dify/run`，已用本机真实本地模型（ollama + qwen3 / deepseek-v4-pro）通过四分支联调。

---

## 2. 对比对象概览

### 2.1 代码版（Week 3 DevAssistantAgent）

| 项 | 内容 |
| --- | --- |
| 形态 | LangChain v1 `create_agent` 编译出的 `CompiledStateGraph`（真正的 Agent Loop） |
| 工具 | `calculator`（AST 白名单安全算术）、`read_text_file`（沙箱只读、防路径穿越）、`check_commit_message`（提交规范校验） |
| 控制 | `TraceMiddleware` + `TraceRecorder`（记录 before/after_model、tool_call）、`SafeToolMiddleware`（异常→`TOOL_ERROR:` 不冒泡）、`recursion_limit=8` |
| 模型决策 | **模型自己决定调哪个工具**、是否继续循环（四个终止条件 A–D） |
| 入口 | 仅测试框架间接调用；无终端 REPL、无 Web 服务（与 `src/app.py` 旧 `LlmClient` 割裂） |
| 测试 | `FakeToolCapableChatModel` + 82 条用例（工具 38 + agent 21 + tool_calling 20 + loop 3） |

### 2.2 Dify 版（Week 4 DevAssistantAgent_Dify + FastAPI 包装）

| 项 | 内容 |
| --- | --- |
| 形态 | Dify Workflow（固定 DAG）：`用户输入 → LLM(参数提取) → IF/ELSE(tool_used) → 四个分支汇总节点 → 变量聚合器 → 结束` |
| 分支 | `calculator` / `check_commit` / `knowledge_retrieval`（kb，知识检索）/ `chat`（闲聊） |
| 控制 | 由画布连线 + 条件分支决定路由；**无循环** |
| 模型决策 | **不靠模型选工具**——第一段 LLM 只解析出 `tool_used` 字段，IF/ELSE 据此走固定分支（确定性路由） |
| 入口 | 已发布工作流 → `POST /dify/run`（`DifyWorkflowClient` 封装，`outputs: dict[str, Any]` 透传） |
| 测试 | 本机真实模型四分支联调（calculator / check_commit / kb / chat 全通过）+ `tests/test_dify_client.py` 3 passed + `tests/test_app_dify.py` 3 passed |

**最关键的架构差异**：代码版是**模型驱动的 Agent Loop**（动态选工具、可迭代）；Dify 版是**确定性的 Workflow**（先用 LLM 抽取意图字段，再用 IF/ELSE 路由到固定分支）。两者都解决"帮研发干活"的问题，但智能的安放位置不同。区别就是：一个是你写好"说明书"让它照着做；一个是你告诉它"目标"，它自己想办法。

---

## 3. 维度对比

### 3.1 调试与可观测性

| 对比点 | 代码版 | Dify 版 |
| --- | --- | --- |
| 调试界面 | 终端 / IDE；需跑 Python 才能看到中间过程 | Dify Studio 画布 + 运行日志，**节点级**输入/输出/耗时/Token 可视化 |
| 追踪粒度 | `TraceRecorder` 把 before_model / after_model / tool_call 记成结构化事件列表，可断言、可回放 | 每个节点独立 trace，能直接看出"哪条分支被触发、各节点耗时多少" |
| 错误定位 | 异常栈 + `TOOL_ERROR:` 信封；但需在代码层打点 | 某节点变红即知失败点，变量选择器点选减少"变量名拼错"类问题 |
| 可观测性外溢 | 在进程内，要自己接 Langfuse 可观测性和追踪平台（Week 10 才做） | Dify 自带运行历史；接 Langfuse 也是后续事项 |
| 局限 | 假模型跑测试，真实模型行为未在生产链路观测过 | 调试强依赖 Dify 服务在线；结构化 trace 需额外接 Langfuse 才能长期留存 |

**结论**：**快速定位"哪一步出问题"Dify 更快**（画布 + 节点日志）；**要写自动化断言 / 回归比对"模型究竟调了什么工具"代码版更好**（结构化 trace + pytest）。两者互补：Dify 适合人在回路里排查，代码版适合进持续集成CI做回归。

### 3.2 版本管理

| 对比点 | 代码版 | Dify 版 |
| --- | --- | --- |
| 主载体 | Git 中的 `.py` 文件 + `uv.lock` | Dify 数据库 / 画布（UI 内状态）；DSL 导出 yaml 只是快照 |
| diff 可读性 | 高——纯文本，行级 diff，Code Review 友好 | 低——`*.yml` 含节点坐标、自动生成 ID，diff 噪声大 |
| 评审 | 可在 git 里 PR 拉取请求后逐行评审、跑 ruff + pytest | 主要在 Dify UI 内改，需养成"改完即导出 + 提交 DSL"的纪律 |
| 回滚 | `git revert` 精准 | 依赖 Dify 版本快照 / 重新导入历史 DSL |
| 本周做法 | 代码改动全部 commit 进仓库（见 §5） | 已把 `DevAssistantAgent_Dify.yml`、`RequirementAnalysis_Dify.yml` 导出归档进 `dify_workflows/` |

**结论**：**代码版通过 git 进行版本管理，管理较强**（文本、可评审、可回滚）；Dify 版必须把"导出 DSL + 提交仓库"作为强制步骤，否则工作流变更只存在于 UI 里、不可追溯。

### 3.3 扩展性

| 对比点 | 代码版 | Dify 版 |
| --- | --- | --- |
| 加一个工具 | 写一个 `@tool` 函数 + 注册进 `create_agent`；Pydantic 做类型约束；pytest 直接覆盖 | 画布上加节点 + 改 IF/ELSE + 配输出映射；无代码，但复杂逻辑多节点会臃肿 |
| 统一能力 | 中间件（Trace / SafeTool）对**所有工具**自动生效 | 每个分支要单独配节点，跨分支复用靠"变量聚合器"等手动物流 |
| 逻辑复杂度 | 适合多步迭代、条件分支嵌套（Agent Loop） | 适合**线性 / 固定分支**；避免过度塞进单个超长 Prompt |
| 生效方式 | 改代码 → 跑测试 → 部署 | 改画布 → **重新发布**工作流，API 才生效（本周联调多次因此重发） |

**结论**：**加"标准工具"Dify 更快**（拖节点即可）；**做"复杂编排 + 统一调整关注点（追踪/兜底）"代码版更可控**。Dify 的强项在"快速验证想法"，代码版的强项在"逻辑可组合、可测试、可复用"。

### 3.4 部署与运行环境

| 对比点 | 代码版 | Dify 版 |
| --- | --- | --- |
| 运行形态 | `uv run python` 跑 Agent；**本仓库暂无 HTTP 服务包它**（`src/app.py` 仍是旧 `LlmClient`，与 Agent 割裂） | Dify 以服务运行（Docker Compose）；工作流发布后由 `POST /dify/run` 这个 FastAPI 服务透传调用 |
| 对外入口 | 无（仅测试） | 有——`uvicorn src.main:app` 起 FastAPI，`/dify/run` 即统一接口 |
| 依赖隔离 | `uv` + `pyproject.toml` + `uv.lock` | Dify 自身依赖由 Docker 镜像管；本仓库只依赖 `httpx` + `fastapi` 客户端 |
| 切换工作流 | 不适用 | 改 `.env` 的 `DIFY_API_KEY` 即可切换配置好的不同工作流 |
| 资源 | 轻量，纯 Python 进程 | 重——需 Dify 全套服务（含向量库等）在线 |

**结论**：Dify 版已通过 `POST /dify/run` 暴露成 HTTP 接口，可直接调用；代码版 Agent 还只在测试里跑、没接进 `src/app.py`。但仓库已有 FastAPI 框架（`src/app.py` + `scripts/serve.py`，自 Week 1 就有），所以代码版"上线"不用重搭服务——只要在 `src/app.py` 里加一条路由（如 `/agent/run`）调用 `build_devassistant_agent()` 即可。

### 3.5 能力边界（补充维度）

| 能力 | 代码版 | Dify 版 | 说明 |
| --- | --- | --- | --- |
| 精确计算 | ✅ AST 白名单，≤512 字符，拒 `**` / 非数字，返回结构化错误信封 | 靠 LLM + 代码节点算；结果取决于节点配置 | 代码版有**显式安全边界**，可解释 |
| 文件读取 | ✅ `read_text_file` 沙箱限定 `train_dir`，防路径穿越 | 无此分支（被 知识检索节点 + chat 取代） | Dify 版**未复现**文件读取，改为使用知识检索 |
| 知识检索（RAG） | 无（需 Week 5–6） | ✅ `knowledge_retrieval` 节点，已接小说知识库 | Dify 版**领先**展示 RAG |
| 提交校验 | ✅ `check_commit_message` | ✅ 代码节点 + 结构化输出 | 两版都有 |
| 闲聊 | 模型判断"无需工具"→ 直接答 | ✅ 独立 chat 分支 | 两版都有 |
| 循环 / 迭代 | ✅ `recursion_limit=8` + 终止条件 A–D，防无限循环 | DAG 无循环（不会死循环，也不能多步迭代） | 代码版能"多轮调用工具"，Dify 版一次到底 |
| 错误兜底 | ✅ `SafeToolMiddleware` 转 `TOOL_ERROR:` 不崩 | 节点失败看 Dify 错误处理配置 | 代码版兜底更程序化、可测 |

**结论**：代码版在**安全计算、文件沙箱、可控循环**上更严谨；Dify 版在**知识检索、快速出原型、可视化**上更省力。两者能力集可互补。

---

## 4. 关键差异速查表

| 维度 | 代码版优 | Dify 版优 | 相同 |
| --- | --- | --- | --- |
| 调试定位 | 结构化 trace + 自动化断言 | 节点级可视化日志 | — |
| 版本管理 | 文本 diff / PR 评审 / 回滚 | — | — |
| 扩展（标准工具） | — | 拖节点即加入，免代码 | — |
| 扩展（复杂编排） | 中间件统一 + 可组合 | — | — |
| 部署（有入口） | — | `POST /dify/run` 已可调用 | — |
| 安全计算 | AST 白名单 + 错误信封 | — | — |
| 知识检索 | — | 内置 RAG 节点 | — |
| 可控循环 | recursion_limit + 终止条件 | — | — |

---

## 5. 适用场景建议

- **用 Dify 版当"探针"**：需求还没定型、要快速给业务方看效果、要接知识库做检索——先在画布拼出来，当天就能演示。
- **用代码版当"底座"**：逻辑要进 CI 回归、要统一追踪/兜底、要做多步迭代或严格安全边界——回到 `src/devagent/` 写 Agent。
- **接真实模型 + 上线**：代码版不用新搭服务，直接在现有 `src/app.py` 加一条 `/agent/run` 路由调用 `DevAssistantAgent` 即可，跟 Dify 版的 `/dify/run` 走同一套框架。

---

## 6. 本周结论

Dify 版与代码版在**调试、版本管理、扩展、部署、能力边界**五个维度上的取舍已厘清。两者不是替代关系——Dify 版是"可视化快速验证"，代码版是"代码优先可演进底座"；同一份 `POST /dify/run` 接口即可让两者在工程中并存，后续按需把代码版也包装成服务即可。

