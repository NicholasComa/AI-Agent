# AI Agent 应用开发

> **第 1–10 周 · 工程基线 + 模型服务 + 单 Agent + Dify 工作流 + RAG 检索/闭环 + MCP 工具 + LangGraph 状态化工作流 + Agent 服务工程化 + 追踪与评测**
> 「12 周 AI Agent 应用开发 Roadmap」的落地工程。Week 1–3 建立可复现的工程环境与模型服务、单 Agent；Week 4 用 Dify 可视化工作流复现场景并对照代码版，由 FastAPI 统一包装；Week 5–6 落地 RAG 检索与 RAG 应用闭环（解析 / 切分 / Embedding / Qdrant / Recall@K 评测 / 带引用可拒答的生成链路）；Week 7 把内部能力做成 MCP 标准工具（stdio + Streamable HTTP 双传输，路径/命令白名单 + 确认门三道安全闸）；Week 8 用 LangGraph 把需求分析拆成 6 节点状态图（分类 / 功能点 / 检索 / 风险 / 测试点 / 报告 + 歧义人工确认），具备重试、降级与 Checkpoint 续跑；Week 9 把上述能力收进一个可对外服务的 Agent 服务（认证 / 限流 / 请求体上限、并发闸门与超时、幂等去重、三个探针与进程内指标、Docker Compose 一键起停 + 卷持久化）；Week 10 为整个工程加可观测性（本地 JSONL + 自托管 Langfuse 双后端、三处自动埋点）与离线可跑的评测层（冻结数据集、纯函数指标、三组参数对比报告）。

---

## 1. 项目内容

本仓库是「12 周 AI Agent 应用开发 Roadmap」**第 1–10 周**的落地工程。目标是建立可复现的 Python AI 应用环境、统一的代码质量与测试基线，并依次交付模型服务、单 Agent、Dify 工作流对照、RAG 检索/闭环、MCP 工具、LangGraph 状态化工作流、Agent 服务工程化，最后为整个工程补上可观测性（追踪）与离线评测层。

**Week 1 · 工程基线（Day 1–5）**
- 用 `uv` + `pyproject.toml` + `uv.lock` 搭建 Python 3.12 统一环境与 Ruff / pytest 质量基线。
- `src/config.py`（pydantic-settings 强类型配置）、`src/llm_client.py`（httpx 异步调用 + 错误信封）、`src/schemas.py` / `src/prompts.py`（JSON Mode 结构化输出）。

**Week 2 · 模型服务（Day 6–10）**
- 升级为稳定服务：`ModelClient` 抽象 + 工厂、`RequestId` + 访问日志中间件、JSON 结构化日志（request_id 链路）。
- 新增 `GET /models`、`POST /chat/stream`（SSE）、`asyncio.Semaphore` 并发限制；统一 `ErrorBody`（含 request_id）。交付 5 个端点、106 条测试。

**Week 3 · 单 Agent（Day 11–15）**
- LangChain v1 `create_agent` 实现 `DevAssistantAgent`（`src/devagent/`）：3 工具（安全算术 / 沙箱读文件 / commit 校验）+ 2 中间件（追踪 + 异常兜底）+ `recursion_limit` 截断。
- 82 条测试（Agent Loop + 工具 + 中间件 + 20 条端到端），全量 **188 passed**。

**Week 4 · Dify 工作流对照（Day 16–20）**
- 两个 Dify 工作流 DSL（`dify_workflows/`）+ `POST /dify/run` 通用透传端点 + `DifyWorkflowClient`；Dify 版 vs 代码版对照报告。
- 6 条 Dify 测试，全量 **194 passed**。

**Week 5 · RAG 检索基础（Day 21–25）**
- `src/rag/`：解析切分、Embedding（mxbai-embed-large，离线 FakeEmbedding 兜底）、检索（ListRetriever + QdrantRetriever）、Qdrant 存储、Recall@K 评测（五类未命中归因）。
- 20 条人工标注集，真实 Recall@1=0.778 / @3=0.833 / @5=0.889；全量 **259 passed**。

**Week 6 · RAG 应用闭环（Day 26–30）**
- `JwipcKnowledgeRAG`（多格式导入）、`RagGenerator`（引用校验 + 双保险拒答）、`POST /rag/query`；30 条问答回归集、chunk_size×TopK 参数网格。
- 检索增强（Metadata Filter / BM25+RRF / 轻量精排，默认关闭）融合进 `src/rag/`；混合检索 Recall@1 0.722→0.833；全量 **319 passed**。

**Week 7 · MCP 工具（Day 31–35）**
- `src/jwipc_dev_mcp_server/`（6 模块）+ 4 工具（list_files / read_file / git_log / check_commit_message）+ 双传输客户端（stdio / Streamable HTTP）。
- 三道安全闸（路径白名单 / 命令白名单 / 确认门）；`mcp_week7_review.py` 业务流水线；28 条测试，全量 **350 passed, 1 skipped**。

**Week 8 · LangGraph 状态化工作流（09-07 ~ 09-11）**
- `src/graph/`：6 业务节点（分类 / 功能点 / 检索 / 风险 / 测试点 / 报告）+ 澄清中断节点；重试降级不中断整图、Checkpoint 续跑、人工确认闭环。
- 4 个演示脚本；24 条图测试，全量 **374 passed, 1 skipped**。

**Week 9 · Agent 服务工程化（09-14 ~ 09-18）**
- `src/agent_service/`（15 模块）：把 RAG / 工作流 / MCP 工具收进可对外服务；5 业务端点 + 3 探针、认证 / 限流 / 请求体上限、并发闸门与超时分级错误码、幂等去重、进程内指标、Docker Compose 一键起停 + 卷持久化。
- 5 个脚本 + 3 份文档 + 88 条服务测试；全量 **462 passed, 1 skipped**。

**Week 10 · 追踪与评测（09-19 ~ 09-23）**
- 可观测性层（`src/observability/`）：本地 JSONL + 自托管 Langfuse 双后端、三处自动降级、仅元数据上报；`traced_chat` / `traced_retriever` / `traced_tool` 三包装器、`trace_week10_view.py` 自包含 HTML 视图。
- 评测层（`src/evaluation/`）：冻结数据集、纯函数指标（阈值 / 跳过语义 / 分位数）、三组参数对比脚本（`eval_week10_run.py` / `compare.py`）与数字块可替换报告。
- 追踪 114 条 + 评测 145 条，全量约 **721 passed, 1 skipped**。

---

## 2. 目录结构

```
week01_ai_basics/
├── README.md                # 本文件
├── pyproject.toml           # 项目元数据 + 依赖 + Ruff/pytest 配置
├── uv.lock                  # 依赖锁定（必须入库）
├── config.example.json      # 配置模板（真实 config.json 不入库）
├── .env.example             # 环境变量模板（真实 .env 不入库）
├── .gitignore               # Git 忽略规则
├── .python-version          # 锁定 Python 3.12（入库）
├── main.py                  # uv init 生成的最小 demo 入口（保留作 hello 示例）
├── src/                     # 业务代码（扁平包）
│   ├── hello.py             # Day 1 hello（demo）
│   ├── config.py            # Day 2/6 配置（pydantic-settings，从 .env 读取）
│   ├── llm_client.py        # Day 3/6 Ollama 适配器（chat / chat_stream / 重试）
│   ├── schemas.py           # Day 4 LLM 输出契约（RequirementAnalysis 等）
│   ├── prompts.py           # Day 4 系统提示 + 消息构造
│   ├── api_models.py        # HTTP 线协议（ChatChunk/ModelInfo/ModelsResponse/ErrorBody/DifyRunRequest/Response）
│   ├── model_client.py      # Day 7 ModelClient 抽象（Protocol）
│   ├── model_factory.py     # Day 7 工厂：配置 → 客户端实例
│   ├── middleware.py        # Day 8 RequestId + AccessLog 中间件（纯 ASGI）
│   ├── logging_config.py    # Day 8 JSON 日志 + request_id 链路
│   ├── app.py               # Day 5/8/9 FastAPI 应用工厂 + 6 端点（含 POST /dify/run）+ 异常映射
│   ├── main.py              # Day 5 FastAPI 服务入口（fastapi dev/run 目标）
│   ├── dify_client.py       # Week 4 DifyWorkflowClient 异步客户端（blocking 调用封装）
│   ├── devagent/            # Week 3 DevAssistantAgent（单 Agent + Tool Calling）
│   │   ├── __init__.py
│   │   ├── dev_assistant_agent.py   # 工厂 build_devassistant_agent + SYSTEM_PROMPT
│   │   ├── middleware.py            # TraceRecorder / TraceMiddleware / SafeToolMiddleware
│   │   └── tools/
│   │       ├── __init__.py
│   │       ├── calculator.py
│   │       ├── read_text_file.py
│   │       └── check_commit_message.py
│   ├── rag/                 # Week 5–6 RAG 检索与应用闭环
│       ├── __init__.py
│       ├── ingestion.py     # 文档加载、切分、metadata、chunk_id
│       ├── embeddings.py     # EmbeddingClient / FakeEmbedding / get_embedding 工厂
│       ├── retriever.py     # ListRetriever（离线兜底）+ QdrantRetriever（主链路）
│       ├── qdrant_store.py   # QdrantConfig、connect、ensure_collection、upsert、search
│       ├── evaluate.py       # load_dataset / evaluate Recall@K / diagnose_miss / write_report
│       ├── probes.py         # ChunkingProbe 临时集合对照实验（切分归因）
│       ├── pdf_reader.py     # Week 6 pypdf 解析 + min_chars 碎片过滤
│       ├── knowledge_rag.py  # Week 6 JwipcKnowledgeRAG 多格式导入编排
│       ├── generator.py      # Week 6 RagGenerator 生成链路（引用校验 + 双保险拒答）
│       ├── filters.py        # Week 6 Metadata Filter（可选增强）
│       ├── bm25.py           # Week 6 BigramBM25 稀疏检索
│       ├── rerank.py         # Week 6 轻量精排 / Cross-Encoder 占位
│       └── hybrid.py         # Week 6 混合检索 RRF + RerankRetriever 适配
│   ├── jwipc_dev_mcp_server/  # Week 7 MCP 服务（stdio + Streamable HTTP）
│   │   ├── __init__.py      # 导出 build_server / main / SERVER_NAME
│   │   ├── server.py        # FastMCP 装配 + ping + 命令行入口（--transport/--host/--port）
│   │   ├── config.py        # McpServerConfig 沙箱与上限（JWIPC_MCP_* 环境变量）
│   │   ├── security.py      # SandboxRoot 路径白名单 / GitCommandPolicy 命令白名单 / ConfirmationGate
│   │   ├── schemas.py       # 出参模型（ToolResult 信封 + tool_failure 统一错误）
│   │   ├── tools.py         # list_files / read_file / git_log / check_commit_message
│   │   └── client.py        # 双传输客户端封装（connect_stdio / connect_http）
│   ├── graph/               # Week 8 LangGraph 状态化工作流
│       ├── __init__.py      # 导出 build_requirement_workflow / build_quickstart_graph / WorkflowConfig
│       ├── state.py         # WorkflowState（TypedDict，total=False，15 字段）
│       ├── config.py        # WorkflowConfig（JWIPC_GRAPH_* 环境变量）
│       ├── fakes.py         # 可注入 Fake ChatFn（按 system 消息 task 键路由）
│       ├── nodes.py         # 6 业务节点 + clarify 中断节点 + 重试降级 + 条件路由
│       ├── workflow.py      # build_requirement_workflow 组装（固定边 + 条件边 + checkpointer）
│       └── quickstart.py    # 最小示例图（条件边 + interrupt + Checkpoint 续跑）
│   ├── observability/        # Week 10 可观测性层（双后端 + 三包装器 + 配置/模型/视图脚本）
│   └── evaluation/           # Week 10 评测层（数据集 / 指标 / 报告 / 对比）
├── data/
│   └── mcp_sandbox/         # Week 7 沙箱夹具（.gitignore 忽略，不入库）
├── src/agent_service/      # Week 9 Agent 服务包（导入根 src/，共 15 模块）
│   ├── __init__.py          # 导出 create_agent_service_app / AgentServiceDeps / AgentServiceSettings 等
│   ├── app.py               # 应用工厂 + 4 个中间件 + 统一异常处理器（含错误码计数）
│   ├── deps.py              # 依赖容器 AgentServiceDeps + ServiceDeps 类型别名 + require(field)
│   ├── lifespan.py          # 启动按序组装：session → config → qdrant/RAG → llm → mcp → workflow（失败只降级）
│   ├── settings.py          # AGENT_SERVICE_* 配置（认证 / 并发 / 限流 / 时限 / 会话目录 / MCP）
│   ├── errors.py            # 错误码、状态码映射、ServiceError（可携带响应头）
│   ├── schemas.py           # 全部请求 / 响应模型（统一 extra=forbid）
│   ├── session.py           # 会话记录与幂等缓存落盘（叶子模块）
│   ├── guards.py            # 并发闸门 / 排队超时 429 / 总时限 504 / 幂等 / 断连探测
│   ├── security.py          # 认证与限流（路由依赖；探针天然豁免）
│   ├── metrics.py           # 进程内指标计数器 + 采集中间件（零新依赖）
│   ├── middleware.py        # RequestId / AccessLog / 请求体上限（纯 ASGI，兼容 SSE）
│   └── routes/              # health（3 探针）/ rag / workflow / tools
├── tests/                   # pytest 测试（约 721 用例 + 1 skip）
│   ├── conftest.py                 # 全局 fixture（预留）
│   ├── fake_models.py              # Week 3 测试假模型（FakeToolCapableChatModel）
│   ├── test_config.py              # AppConfig 配置加载 / 字段校验 / env 隔离
│   ├── test_model_client.py        # ModelClient Protocol 抽象与适配器契约
│   ├── test_llm_client.py          # LlmClient 异步 chat / chat_stream / 错误 / 重试
│   ├── test_streaming.py           # /chat/stream SSE 逐 chunk 推送
│   ├── test_concurrency.py         # asyncio.Semaphore 并发限制（MAX_CONCURRENCY）
│   ├── test_middleware.py          # RequestId / AccessLog 中间件（rid 注入与日志）
│   ├── test_api.py                 # 6 端点集成测试（ASGITransport，覆盖成功/422/401/429/502/504/404）
│   ├── test_structured_output.py   # /analyze-requirement 结构化输出契约与校验
│   ├── test_agent_loop.py          # Week 3 Agent Loop 四个终止条件
│   ├── test_devagent_tools.py      # Week 3 三个工具（含边界 / 越权 / 结构化错误）
│   ├── test_devagent_agent.py      # Week 3 中间件 / 追踪 / 工厂
│   ├── test_atool_calling.py       # Week 3 20 条端到端用例（4 类场景）
│   ├── test_dify_client.py         # Week 4 DifyWorkflowClient 初始化 / run / 异常
│   ├── test_app_dify.py            # Week 4 POST /dify/run 路由成功 / 参数校验 / 异常
│   ├── test_embedding_client.py    # Week 5 EmbeddingClient / FakeEmbedding 契约
│   ├── test_qdrant_store.py        # Week 5 QdrantConfig / connect / upsert / search
│   ├── test_rag_ingestion.py       # Week 5 文档加载 / 切分 / metadata
│   ├── test_rag_retriever.py       # Week 5 ListRetriever / QdrantRetriever 检索
│   ├── test_rag_evaluate.py        # Week 5 Recall@K / diagnose_miss 五类归因
│   ├── test_rag_probes.py          # Week 5 ChunkingProbe 对照实验与边界
│   ├── test_knowledge_rag.py       # Week 6 多格式导入 / PDF 解析
│   ├── test_rag_generator.py       # Week 6 生成 + 拒答 + 引用校验
│   ├── test_rag_qa_set.py          # Week 6 30 条问答集回归
│   ├── test_rag_api.py             # Week 6 POST /rag/query 线协议
│   ├── test_rag_hybrid_search.py   # Week 6 混合检索 RRF / BM25
│   ├── test_rag_metadata_filter.py # Week 6 Metadata Filter
│   ├── test_rag_rerank.py          # Week 6 轻量精排
│   ├── test_mcp_server_tools.py    # Week 7 MCP 4 工具常规 + 边界（15 条）
│   ├── test_mcp_client.py          # Week 7 双传输客户端端到端（3 条）
│   ├── test_mcp_security.py        # Week 7 SandboxRoot / GitCommandPolicy / ConfirmationGate（10 条）
│   ├── graph/                      # Week 8 状态化工作流测试（24 条）
│   │   ├── test_workflow_smoke.py        # 冒烟：正常 6 节点 / 歧义中断 + resume / 降级
│   │   ├── test_requirement_workflow.py  # 图结构 5 + 节点职责 7 + 人工确认 4
│   │   └── test_graph_resilience.py      # 韧性 5：重试 / 降级 / 配置 / 依赖 / 报告
│   └── agent_service/              # Week 9 Agent 服务测试（88 条）
│       ├── conftest.py                   # harness 夹具（ASGITransport 内存跑整个 ASGI 栈）
│       ├── service_fakes.py              # 服务替身：ServiceChat / FakeMcpSession / build_rag / build_deps
│       ├── test_service_health.py        # 探针语义 / 配置校验 / 统一错误信封
│       ├── test_service_rag.py           # RAG 端点一次性与 SSE / 拒答 / 引用
│       ├── test_service_workflow.py      # 工作流端点正常 / 挂起续跑 / 幂等
│       ├── test_service_tools.py         # 工具列表与调用 / 结构化信封 / 协议异常
│       ├── test_service_guards.py        # 会话落盘 / 幂等命中 / 闸门 429 / 总时限 504
│       ├── test_service_security.py      # 401 / 429 + Retry-After / 413 / 探针豁免
│       └── test_service_metrics.py       # 指标契约与埋点 / 探针明细（25 条）
├── examples/                # 真实 / 样本数据
│   ├── requirement_samples.json    # 需求分析样本
│   ├── structured_run_real.json    # 真实模型运行输出样本
│   └── retrieval_set.json          # Week 5 20 条人工标注检索集（expected_sources）
├── scripts/                 # 一次性脚本（真实跑）
│   ├── run_real_ollama.py   # 真实调用 ollama 验证脚本（自带 src/ 路径）
│   ├── serve.py             # 绕过 Windows AppLocker 的本地 ASGI launcher
│   ├── call_dify_workflow.py  # Week 4 本机直连 Dify API 调用 / 校验
│   ├── rag_week5_demo.py      # Week 5 RAG 检索 demo（离线 FakeEmbedding）
│   ├── rag_week5_real_demo.py # Week 5 真实 Embedding + Docker Qdrant 闭环演示
│   ├── rag_week5_eval.py      # Week 5 Recall@K 评测（--show-results / --verbose / --offline）
│   ├── rag_week6_ingest.py    # Week 6 知识库导入 CLI（--rebuild 重建集合）
│   ├── rag_week6_qa.py        # Week 6 问答 CLI（--strategy / --metadata / --debug / --eval）
│   ├── rag_week6_param_compare.py  # Week 6 chunk_size × TopK 参数网格评估
│   ├── mcp_week7_server.py    # Week 7 MCP 服务启动入口（--transport stdio|streamable-http）
│   ├── mcp_week7_smoke.py     # Week 7 MCP 冒烟（list_tools + ping + 4 工具）
│   ├── mcp_week7_review.py    # Week 7 业务流水线「审查一次提交改动」（--demo-errors 演示异常）
│   ├── service_week9_smoke.py        # Week 9 冒烟：ASGITransport + 替身，不联网
│   ├── service_week9_acceptance.py   # Week 9 验收：自建子进程 + 真实 HTTP，8 分组
│   ├── service_week9_load.py         # Week 9 并发与超时（11 项断言）
│   └── service_week9_compose_check.py # Week 9 容器重启与持久化（12 项断言）
├── dify_workflows/          # Week 4 导出的 Dify 工作流 DSL
│   ├── DevAssistantAgent_Dify.yml   # Day 17 四分支工作流（calculator/check_commit/kb/chat）
│   └── RequirementAnalysis_Dify.yml # Day 18 需求分析工作流
├── logs/                    # 脚本运行日志（不入库）
│   ├── ollama_run.log
│   └── service_week9.log    # Week 9 服务运行日志（docker compose logs 导出）
└── docs/                    # 学习笔记、阶段交付物
    ├── day01_environment.md
    ├── day02_python_json.md
    ├── day03_http_async.md
    ├── day04_structured_output.md
    ├── day06_concepts.md
    ├── day07_concepts.md
    ├── day08_concepts.md
    ├── day09_concepts.md
    ├── day11_agent_loop.md
    ├── day12_tools.md
    ├── day13_agent_middleware.md
    ├── day14_task_summary.md
    ├── week01_summary.md    # 第 1 周五天总结
    ├── week02_summary.md    # 第 2 周阶段总结
    ├── week03_summary.md    # 第 3 周阶段总结
    ├── day16_dify_concepts.md
    ├── day17_dify_repro_design.md  # Day 17 设计 + §10 元 Schema 修正步骤
    ├── day17_task_summary.md
    ├── day18_task_summary.md
    ├── day19_task_summary.md
    ├── day20_dify_vs_code_report.md  # Dify 版 vs 代码版对比
    ├── week04_summary.md    # 第 4 周阶段总结
    ├── week05_retrieval_eval.md  # Week 5 Recall@K 评测报告
    ├── week05_summary.md    # 第 5 周阶段总结
    ├── week06_summary.md    # 第 6 周阶段总结
    ├── param_compare.md     # Week 6 chunk_size × TopK 参数对比报告
    ├── param_compare_offline.md  # Week 6 离线对照报告
    ├── week07_tools.md      # Week 7 MCP 工具说明（入参/出参/行为/异常码）
    ├── week07_security_report.md  # Week 7 安全测试报告（三道闸设计与实测）
    ├── week07_summary.md    # 第 7 周阶段总结
    ├── week08_architecture.md  # Week 8 Mermaid 架构图 + 节点职责 + Checkpoint 示例
    ├── week08_state_fields.md  # Week 8 状态字段说明（写入方 / 读取方 / 失败取值）
    ├── week08_summary.md    # 第 8 周阶段总结
    ├── week09_deployment.md # Week 9 部署手册（前置条件/配置全表/两套流程/探针口径/排障）
    ├── week09_test_report.md# Week 9 工程测试报告（并发/超时/重启/持久化 + 排障记录）
    ├── week09_summary.md    # 第 9 周阶段总结
    ├── week09_test_commands.md # Week 9 服务测试命令参考
    └── poho/                # 运行 / 测试截图（不入库）
```

> 本项目的核心交付目录为 `src/`、`tests/`、`docs/`；`main.py`（demo 入口）、`config.example.json`、`scripts/`、`examples/`、`logs/` 为辅助文件。`Dockerfile` / `compose.yaml`（含 `.dockerignore`）已从第 9 周加入。

---

## 3. 技术栈

| 层次        | 选型                        | 用途                                  |
| ----------- | --------------------------- | ------------------------------------- |
| 运行环境    | Python 3.12                 | 主线语言；与 uv 锁定                  |
| 依赖管理    | uv + pyproject.toml         | 虚拟环境、依赖安装、锁文件            |
| 代码质量    | Ruff（lint + format）       | 静态检查 + 格式化 + import 排序        |
| 单元测试    | pytest + pytest-asyncio     | 单元 / 集成测试（asyncio_mode=auto）  |
| HTTP 客户端 | httpx                       | 异步模型接口调用（OpenAI 兼容）       |
| 数据模型    | Pydantic v2                 | 配置校验、结构化输出、HTTP 线协议      |
| 配置加载    | pydantic-settings           | 从 `.env` 读取 `AppConfig`（Week 2 起）|
| 流式响应    | sse-starlette               | `POST /chat/stream` 的 SSE 推送        |
| API 服务    | FastAPI[standard] + Uvicorn | 6 端点服务（含 `fastapi` CLI + uvicorn，Week 4 增 `POST /dify/run`）|
| Agent 框架    | LangChain v1               | `create_agent` + `@tool` + `AgentMiddleware`（Week 3 起）|
| 向量数据库  | Qdrant                      | Week 5 RAG 向量存储（Collection + Point + 向量检索，Cosine）|
| Embedding   | mxbai-embed-large（Ollama） | Week 5 文本向量化（dim=1024）；离线用 FakeEmbedding |
| 工作流平台  | Dify                        | Week 4 可视化工作流（节点/变量/分支/知识检索），API 由 `POST /dify/run` 调用 |
| 环境变量    | python-dotenv               | 读取 `.env` 中的密钥与配置            |
| 服务工程化  | FastAPI 路由依赖 + 纯 ASGI 中间件 | Week 9：认证 / 限流 / 请求体上限 / 请求 ID / 访问日志 / 指标采集（不用 `BaseHTTPMiddleware`，避免缓存响应体与 SSE 冲突） |
| 并发与超时  | asyncio.Semaphore + 排队/总时限双闸门 | Week 9：排队超时 → 429 `rate_limited`；总时限 → 504 `timeout` |
| 容器化      | Docker 多阶段构建 + Docker Compose | Week 9：非 root 运行、具名卷持久化（`qdrant_data` / `agent_service_sessions`）、Healthcheck 打 `/health` |
| 可观测性    | 自研进程内指标（零新依赖）  | Week 9：固定边界延迟直方图（内存与请求量无关）、路由键归一化、`/metrics-summary` 导出 |

---

## 4. 环境要求

| 工具            | 版本        | 校验命令                                                                                  |
| --------------- | ----------- | ----------------------------------------------------------------------------------------- |
| Python          | 3.12.13     | `uv run python --version`                                                                 |
| uv              | ≥ 0.5       | `uv --version`                                                                            |
| Git             | 任意新版    | `git --version`                                                                           |
| Ruff            | ≥ 0.15.22   | `uv run ruff --version`                                                                   |
| pytest          | ≥ 9.1.1     | `uv run pytest --version`                                                                 |
| pytest-asyncio  | ≥ 1.4.0     | `uv run python -c "import pytest_asyncio; print(pytest_asyncio.__version__)"`             |
| FastAPI[standard] | ≥ 0.139.2 | `uv run python -c "import fastapi; print(fastapi.__version__)"`                          |
| Uvicorn         | ≥ 0.51.0    | `uv run python -c "import uvicorn; print(uvicorn.__version__)"`                            |
| Pydantic        | ≥ 2.13.4    | `uv run python -c "import pydantic; print(pydantic.VERSION)"`                             |
| pydantic-settings | ≥ 2.14.2 | `uv run python -c "import pydantic_settings; print(pydantic_settings.__version__)"`      |
| sse-starlette   | ≥ 3.4.6     | `uv run python -c "import sse_starlette; print(sse_starlette.__version__)"`               |
| httpx           | ≥ 0.28.1    | `uv run python -c "import httpx; print(httpx.__version__)"`                               |
| python-dotenv   | ≥ 1.2.2     | `uv run python -c "import dotenv; print(dotenv.__version__)"`                             |
| LangChain       | ≥ 1.3.14    | `uv run python -c "import langchain; print(langchain.__version__)"`                        |
| LangGraph       | ≥ 1.2.10    | `uv run python -c "import langgraph; print(langgraph.__file__)"`                           |

> **关于 `fastapi[standard]`（FastAPI 增强版）**：本项目锁定的是 `fastapi[standard]`，**不是**裸 `fastapi`。`[standard]` 额外捆绑了 `uvicorn` 与 `fastapi` 命令行工具（`fastapi dev` / `fastapi run`）。若只装了裸 `fastapi`，运行 `uv run fastapi dev src/main.py` 会报「需要安装 `fastapi[standard]`」的错误；请始终用 `uv add "fastapi[standard]"`，依赖已在 `pyproject.toml` 中声明。

> **不要在系统级 pip 安装依赖**。所有依赖都通过 `uv add` 加入 `pyproject.toml`，并由 `uv.lock` 锁定。

**本地模型服务（可选但推荐远程模型服务）**：`/chat` 等端点默认对接本地 Ollama。需自行安装并拉取模型，例如 `ollama pull qwen3`，并在 `.env` 中设置 `API_BASE_URL=http://localhost:11434/v1`、`MODEL_NAME=qwen3`。

---

## 5. 快速启动

```bash
# 1. 克隆仓库
git clone https://github.com/NicholasComa/AI-Agent.git
cd AI-agent

# 2. 同步依赖（首次会创建 .venv 并安装锁定的全部包）
uv sync

# 3. 复制环境变量模板(部分内容需自己配置)
cp .env.example .env         # Git Bash / PowerShell
# cmd 中：  copy .env.example .env

# 4. 启动 FastAPI 服务
uv run fastapi dev src/main.py      # 开发模式（自动重载）
uv run fastapi run src/main.py      # 生产模式
```

> 命令运行环境说明：
> - `mkdir` / `cp` / `rm` 走 **Git Bash**。
> - `copy` / `del` / `dir` 走 **cmd**。
> - `uv add` / `uv run` 在以上两种环境下命令一致。

---

## 6. API 服务

启动后默认监听 `http://127.0.0.1:8000`，共 6 个端点：

| 方法 | 路径 | 用途 | 输入 Schema | 输出 Schema |
| --- | --- | --- | --- | --- |
| GET  | `/health` | 服务状态 + 配置摘要（provider / max_concurrency 等） | - | `HealthResponse` |
| POST | `/chat` | 转发 chat-completions（非流式） | `ChatRequest` | `ChatResponse` |
| POST | `/chat/stream` | 流式对话，SSE 逐 chunk 推送 | `ChatRequest` | `text/event-stream`（`ChatChunk` 序列，末片 `done:true`） |
| GET  | `/models` | 列出可用模型 + 当前默认模型 | - | `ModelsResponse` |
| POST | `/analyze-requirement` | 结构化需求分析（JSON Mode + Pydantic 校验） | `AnalyzeRequirementRequest` | `AnalyzeRequirementResponse` (extends `RequirementAnalysis`) |
| POST | `/dify/run` | 通用透传：调用 `.env` 中 `DIFY_API_KEY` 指向的 Dify 工作流（Week 4 新增） | `DifyRunRequest` | `DifyRunResponse`（`outputs` 透传） |

所有 I/O 模型定义在 `src/api_models.py`；所有错误统一为 `ErrorResponse`（内含 `ErrorBody`）：

```json
{
  "error": {
    "code": "llm_timeout",
    "message": "模型调用超时",
    "detail": "LlmTimeoutError",
    "status_code": 504,
    "request_id": "f925d1c50d1947b49c3e1339fca568dc"
  }
}
```

错误码 → HTTP 状态：

| code | HTTP | 触发场景 |
| --- | --- | --- |
| `validation_error`     | 422 | 请求体不符合 Pydantic 约束（空 messages / 越界 temperature / 非法 role / 空 text / 多传未定义字段等） |
| `not_found`            | 404 | 未知路由 |
| `method_not_allowed`   | 405 | 路由存在但方法不匹配 |
| `llm_auth`             | 401 | 401/403（API key 错） |
| `llm_rate_limit`       | 429 | 429 限流 |
| `llm_timeout`          | 504 | 模型超时（含重试耗尽） |
| `llm_server`           | 502 | 上游 5xx |
| `llm_bad_response`     | 502 | 响应不是合法 JSON / 不符合 `RequirementAnalysis` |
| `llm_error`            | 502 | 其它 `LlmError` |
| `internal_error`       | 500 | 未捕获异常（兜底） |

> `ChatRequest` 含 `model_config = ConfigDict(extra="forbid")`：**多传任何未定义字段都会直接 422 拒绝**。想流式请打 `/chat/stream`，不要往 `/chat` 的 body 里塞 `stream` 字段。

启动命令：

```bash
# 开发模式（自动重载）
uv run fastapi dev src/main.py

# 生产模式
uv run fastapi run src/main.py

# 直接用 Python 跑（走 src/main.py 的 __main__ 分支）
uv run python src/main.py
```

> 端点都**不写** API key 到日志 / 异常消息 / 响应体；`/health` 只返回 `key_configured: true/false`。所有响应体与响应头（`X-Request-ID`）都携带同源 `request_id`，可据此关联日志。

启动成功后可打开交互式 API 文档：`http://127.0.0.1:8000/docs`（Swagger UI）或 `http://127.0.0.1:8000/redoc`。API docs中的具体操作步骤如下：

```powershell
GET /health
# 直接点击“GET /health”栏后展开界面，点击“Try it out”后，点击“Execut”。

GET /models
# 操作同上 /health。

POST /chat
# 直接点击“POST /chat”栏后展开界面，点击“Try it out”后，会显示一个默认参数的“Request body”如下:
{
  "messages": [
    {
      "role": "system",
      "content": "string"
    }
  ],
  "model": "string",
  "temperature": 2,
  "max_tokens": 1
}
填写示例：
{
  "messages": [
    {
      "role": "user",
      "content": "用一句话介绍你自己"
    }
  ],
  "model": "qwen3"
}
填写完后点击“Execut”即可执行。

POST /chat/stream
# 主要操作同上 /chat

POST /analyze-requirement
# 直接点击“POST /analyze-requirement”栏后展开界面，点击“Try it out”后，会显示一个默认参数的“Request body”如下:
{
  "text": "string"
}
填写示例：
{
  "text":"我要做一个员工请假系统，支持手机端提交申请和领导审批"
}
填写完后点击“Execut”即可执行。

```

Git bash 中 API 服务对话操作步骤如下：
```bash
# /health
curl -s http://127.0.0.1:8000/health | python -m json.tool

# /chat（建议用 UTF-8 文件 + -d @req.json 传入中文，详见下方说明）
cat > req.json <<'EOF'
{"messages":[{"role":"user","content":"用一句话介绍你自己"}]}
EOF
curl -s -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d @req.json

# /chat/stream（必须加 -N 才能实时看到逐字输出）
curl -N -X POST http://127.0.0.1:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d @req.json

# /models
curl -s http://127.0.0.1:8000/models

# /analyze-requirement
cat > req2.json <<'EOF'
{"text":"我要做一个员工请假系统，支持手机端提交申请和领导审批"}
EOF
curl -s -X POST http://127.0.0.1:8000/analyze-requirement \
  -H "Content-Type: application/json" \
  -d @req2.json
```

> **中文测试提示（Windows 终端编码问题）**：Git Bash / cmd 默认控制台输入代码页为 936(GBK)，直接内联中文（如 `-d '{"content":"你好"}'`）会产生 GBK 字节，FastAPI 只认 UTF-8，于是报 `400 There was an error parsing the body`。**稳定解法**：
> 1. 用 VS Code 保存一个 **UTF-8** 的 `req.json`，再以 `-d @req.json` 传入（推荐）；
> 2. 或直接在浏览器 `http://127.0.0.1:8000/docs` 的 Swagger UI 里 Try it out 打中文（浏览器自动发 UTF-8）。
> 3. 直接使用英文进行对话。

### 6.1 Week 9 · Agent 服务（8 个端点，端口 8080）

`src/agent_service/` 是独立于 `src/main.py` 的第二个应用，把 RAG / 工作流 / MCP 工具统一对外。
启动：`uv run python scripts/serve.py --app src.agent_service.app:app --port 8080`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET  | `/health` | 存活探针：进程是否还在、版本、启动时间、后端模式。**恒 200**，依赖全挂只置 `degraded` |
| GET  | `/ready` | 就绪探针：逐依赖明细；必需层未就绪时 **503**（body 仍完整） |
| GET  | `/metrics-summary` | 指标快照：请求量 / 状态码分布 / 延迟 p95 / 模型调用 / 检索命中率 / 错误码 / 幂等命中 |
| POST | `/v1/rag/answer` | RAG 问答；`?stream=true` 走 SSE |
| POST | `/v1/workflow/requirement-analysis` | 需求分析工作流（可能挂起等待澄清） |
| POST | `/v1/workflow/{thread_id}/resume` | 补充澄清信息后从断点续跑 |
| GET  | `/v1/tools` | 列出 MCP 工具 |
| POST | `/v1/tools/{name}/call` | 调用 MCP 工具 |

三个探针**不参与认证**，供容器 Healthcheck 与编排系统调用（`/health`、`/ready`、`/metrics-summary` 也不计入请求指标，避免秒级探活稀释业务流量）。

业务端点支持两个请求头：

| 请求头 | 作用 |
| --- | --- |
| `Authorization: Bearer <key>` | 认证；仅当 `AGENT_SERVICE_API_KEY` 非空时强制 |
| `Idempotency-Key: <key>` | 幂等去重；命中时响应带 `Idempotency-Replayed: true` |

错误码 → HTTP 状态：

| code | HTTP | 触发场景 |
| --- | --- | --- |
| `invalid_argument` | 400 / 404 / 405 / 422 | 参数不合规、未知路由、方法不匹配 |
| `unauthorized`     | 401 / 403 | 未带或带错 Bearer token |
| `payload_too_large`| 413 | 请求体超过 `AGENT_SERVICE_MAX_BODY_BYTES` |
| `rate_limited`     | 429 | 超过每分钟配额，或在并发闸门排队超时 |
| `cancelled`        | 499 | 流式连接被客户端断开 |
| `internal`         | 500 | 未捕获异常（兜底） |
| `dependency_unavailable` | 503 | 必需依赖未就绪 |
| `timeout`          | 504 | 单请求总时限 `AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS` 耗尽 |

> **`429` 与 `504` 的区别是排障关键**：`429` 表示请求根本没进去（排队失败），`504` 表示进去了但没跑完。
> 前者要扩配额或降并发，后者要看单次耗时。
>
> `/ready` 返回 503 是**设计行为**而非故障：必需依赖（`session_store` / `config` / `qdrant` / `llm` / `workflow`）中任一层未就绪即 503，`mcp` 为可选层不影响判定。

部署、配置键全表与排障见 `docs/week09_deployment.md`。

---

## 7. 测试与质量

```bash
# 全部测试（截至 Week 10 共约 721 passed + 1 skipped）
uv run pytest -q

# 按模块运行（部分示例）
uv run pytest -v tests/test_api.py
uv run pytest -v tests/test_streaming.py
uv run pytest -v tests/test_atool_calling.py
uv run pytest -v tests/test_dify_client.py tests/test_app_dify.py
uv run pytest -v tests/test_rag_evaluate.py tests/test_rag_probes.py

# Week 7 新增模块单测（MCP 28 条）
uv run pytest -v tests/test_mcp_server_tools.py tests/test_mcp_security.py tests/test_mcp_client.py

# Week 8 新增模块单测（状态化工作流 24 条）
uv run pytest -v tests/graph

# Week 9 新增模块单测（Agent 服务 88 条）
uv run pytest -v tests/agent_service

# 单个用例
uv run pytest -v tests/test_api.py::test_chat_timeout_returns_504

# Week 8 工作流脚本（默认注入 Fake，不依赖模型与 Qdrant）
uv run python scripts/graph_week8_quickstart.py        # 最小状态图：条件边 + interrupt + Checkpoint
uv run python scripts/graph_week8_workflow.py          # 两条主路径：正常 / 歧义澄清
uv run python scripts/graph_week8_resilience.py        # 韧性：重试 / 降级 / Checkpoint 续跑
uv run python scripts/graph_week8_demo.py              # 业务串联：1 正常 + 3 异常
uv run python scripts/graph_week8_demo.py --scenario normal --real   # 接真实 Ollama + Qdrant

# 静态检查 + 格式化
uv run ruff check .
uv run ruff format --check .

# 真实环境探活（要求 .env 已配置模型 API 或 Ollama API 且 ollama 在跑）
curl http://127.0.0.1:8000/health
```

### 7.1 新增模块运行指令

**Week 5 · RAG 检索基础**

```bash
# 离线检索 demo（FakeEmbedding，无需 Qdrant）
uv run python scripts/rag_week5_demo.py
# 真实 Embedding + Docker Qdrant 闭环演示
uv run python scripts/rag_week5_real_demo.py
# Recall@K 评测（--offline 离线 / --show-results 打印明细 / --verbose）
uv run python scripts/rag_week5_eval.py --offline
uv run python scripts/rag_week5_eval.py --show-results
```

**Week 6 · RAG 应用闭环**

```bash
# 知识库导入（--rebuild 重建集合）
uv run python scripts/rag_week6_ingest.py --rebuild
# 问答 CLI（--strategy / --metadata / --debug / --eval）
uv run python scripts/rag_week6_qa.py --strategy hybrid --eval
# chunk_size × TopK 参数网格评估
uv run python scripts/rag_week6_param_compare.py
```

**Week 7 · MCP 服务**

```bash
# 启动 Server（stdio 默认；streamable-http 需另开终端常驻，端口必须带 :8765）
uv run python scripts/mcp_week7_server.py --transport stdio
uv run python scripts/mcp_week7_server.py --transport streamable-http --port 8765

# 冒烟（list_tools + ping + 4 工具，两种传输）
uv run python scripts/mcp_week7_smoke.py --transport stdio
uv run python scripts/mcp_week7_smoke.py --transport streamable-http

# 业务流水线「审查一次提交改动」（stdio 默认；--repo 指定沙箱内仓库）
uv run python scripts/mcp_week7_review.py
uv run python scripts/mcp_week7_review.py --transport streamable-http
# 演示 3 异常场景（越界 / 大文件未确认 / git 参数注入）
uv run python scripts/mcp_week7_review.py --demo-errors
```

**Week 9 · Agent 服务（本机 + 容器两套流程）**

```bash
# --- 本机直跑 ---
# 启动服务（前台运行）
uv run python scripts/serve.py --app src.agent_service.app:app --host 127.0.0.1 --port 8080

# 三个探针（--noproxy 必需：本机 HTTP_PROXY 会劫持发往 127.0.0.1 的请求）
curl -s http://127.0.0.1:8080/health          --noproxy '*'
curl -s http://127.0.0.1:8080/ready           --noproxy '*' | python -m json.tool
curl -s http://127.0.0.1:8080/metrics-summary --noproxy '*' | python -m json.tool

# 接口验收（自建子进程 + 真实 HTTP，7 分组）
uv run python scripts/service_week9_acceptance.py
uv run python scripts/service_week9_acceptance.py --only rag,workflow   # 只跑指定分组

# 并发 + 超时（11 项断言；--min-score 1.0 强制拒答以绕开 CPU 推理）
uv run python scripts/service_week9_load.py --levels 20,50 --requests 40 --min-score 1.0
uv run python scripts/service_week9_load.py --only timeout              # 只跑超时组

# --- 容器 ---
docker stop qdrant_server          # 让出 6333（本机常驻容器占用）
docker compose up -d --build       # 代码变更后必须 rebuild，容器不会自动同步源码
uv run python scripts/service_week9_compose_check.py --skip-up   # 重启 + 持久化（12 项断言）
uv run python scripts/service_week9_compose_check.py --check-only # 只报告，不动容器
docker compose logs --no-color api > logs/service_week9.log      # 导出服务日志
docker compose down && docker start qdrant_server                # 收尾还原
```

三个脚本的退出码统一为 `0` 全部通过 / `1` 有失败项 / `2` 前置条件不满足。
参数与分组明细见 `docs/week09_test_commands.md`，部署与排障见 `docs/week09_deployment.md`。

**测试内容（36 个文件 / 462 用例 + 1 skip）**

| 测试文件 | 覆盖主题 | 关键验证点 |
| --- | --- | --- |
| `test_config.py` | `AppConfig` 配置加载 | `.env` 读取、字段类型校验、`extra` 字段拒绝、env 隔离（不污染其它用例） |
| `test_model_client.py` | `ModelClient` 抽象 | Protocol 契约、适配器可被 `isinstance` 校验、与具体后端解耦 |
| `test_llm_client.py` | `LlmClient` 适配器 | 异步 `chat()` / `chat_stream()`、各类 `LlmError` 抛出、重试逻辑 |
| `test_streaming.py` | 流式接口 | `/chat/stream` 逐 `ChatChunk` 推送、末片 `done:true`、中断处理 |
| `test_concurrency.py` | 并发限制 | `asyncio.Semaphore` 限制同时打后端的请求数（`MAX_CONCURRENCY`） |
| `test_middleware.py` | 中间件 | `RequestIdASGIMiddleware` 注入/传播 rid、`AccessLogMiddleware` 结构化日志且不读敏感字段 |
| `test_api.py` | 6 端点集成 | 用 `httpx.ASGITransport` 在内存跑整个 ASGI 栈，覆盖 `/health` `/chat` `/chat/stream` `/models` `/analyze-requirement` `/dify/run` 的成功与 422/401/429/502/504/404 全分支 |
| `test_structured_output.py` | 结构化输出 | `/analyze-requirement` 返回符合 `RequirementAnalysis` 契约、JSON Mode 闭环 |
| `test_agent_loop.py` | Agent Loop | 四个终止条件（直接 final / 调工具→结果→final / 异常兜底 / recursion_limit 截断） |
| `test_devagent_tools.py` | 三个工具 | `calculator` AST 白名单、`read_text_file` 沙箱防穿越、`check_commit_message` 规范校验 |
| `test_devagent_agent.py` | 中间件 / 工厂 | `TraceRecorder` 事件记录、`TraceMiddleware` 三钩子、`SafeToolMiddleware` 兜底、`build_devassistant_agent` 工厂 |
| `test_atool_calling.py` | 20 条端到端用例 | 4 类场景：正确选工具 6 / 无需工具 4 / 错误参数 5 / 工具失败 5 |
| `test_dify_client.py` | Week 4 DifyWorkflowClient | 初始化校验（缺 key 抛错）、`run()` 成功、参数/异常分支 |
| `test_app_dify.py` | Week 4 POST /dify/run | 路由成功 / 参数校验 / 异常分支 |
| `test_embedding_client.py` | Week 5 Embedding | `EmbeddingClient` / `FakeEmbedding` 维度一致、工厂返回正确实例 |
| `test_qdrant_store.py` | Week 5 Qdrant | `QdrantConfig` 解析、connect、ensure_collection、upsert、search 边界 |
| `test_rag_ingestion.py` | Week 5 解析切分 | 文档加载、`chunk_size/overlap` 切分、metadata 与 `source` 保留 |
| `test_rag_retriever.py` | Week 5 检索 | `ListRetriever` 离线相似度、`QdrantRetriever` 主链路返回 `chunk_id/source/score/text` |
| `test_rag_evaluate.py` | Week 5 评测 | Recall@K 统计、五类未命中归因（过滤/解析/检索异常/TopK/Embedding 或切分）、`detail_out` |
| `test_rag_probes.py` | Week 5 切分归因 | `ChunkingProbe` 临时集合对照、方案齐全、语义高分、空 paths/非法方案/维度不一致报错 |
| `test_knowledge_rag.py` | Week 6 多格式导入 | MD/TXT/PDF 分派解析、`min_chars` 碎片过滤、Chunk 元数据 |
| `test_rag_generator.py` | Week 6 生成链路 | 引用校验/改挂/降级、双保险拒答、阈值行为（dim=1024 单独验证） |
| `test_rag_qa_set.py` | Week 6 问答集回归 | 30 条（20 found + 10 unfound）逐条断言与拒答契约 |
| `test_rag_api.py` | Week 6 RAG API | `POST /rag/query` 线协议（ASGITransport）+ 拒答契约 |
| `test_rag_hybrid_search.py` / `test_rag_metadata_filter.py` / `test_rag_rerank.py` | Week 6 检索增强 | BM25/RRF 融合、payload 过滤、精排适配与懒构建 |
| `test_mcp_server_tools.py` | Week 7 MCP 工具 | 4 工具常规 + 边界（越界/不存在/目录/超限/二进制/确认后放行/截断/非仓库） |
| `test_mcp_security.py` | Week 7 安全闸 | SandboxRoot 各类越界、GitCommandPolicy 注入拦截、ConfirmationGate 状态（symlink 用例 Windows skip） |
| `test_mcp_client.py` | Week 7 双传输 | stdio 真实子进程全链路、Streamable HTTP ASGI 端到端、connect_http 工厂注入 |
| `graph/test_workflow_smoke.py` | Week 8 工作流冒烟 | 正常 6 节点写满字段、歧义 interrupt + `Command(resume=...)` 续跑、LLM 永久失败降级 |
| `graph/test_requirement_workflow.py` | Week 8 图结构与节点 | 节点集合与固定边主链、条件边路由、thread 隔离、Mermaid 导出；6 节点各自字段与降级；人工确认 4 条 |
| `graph/test_graph_resilience.py` | Week 8 韧性测试 | 瞬时故障重试成功不留 errors、永久失败写 errors、重试次数可配置、依赖降级不中断、失败后报告仍产出 |
| `agent_service/test_service_health.py` | Week 9 探针与配置 | `/health` 依赖挂掉仍 200 只置 degraded（存活探针不绑依赖）、`/ready` 必需层未就绪 503 并指名、可选层（MCP）未就绪仍 200、依赖层集合完整、统一错误信封、`request_id` 双处一致、容器未注册报 `dependency_unavailable`、配置非法值启动前拦下、闸门容量取自配置 |
| `agent_service/test_service_rag.py` | Week 9 RAG 端点 | 一次性与 SSE 流式（帧序 / 取消 / 超时）、拒答契约、引用校验、依赖缺失 503 |
| `agent_service/test_service_workflow.py` | Week 9 工作流端点 | 正常路径 6 节点写满字段、歧义挂起与 `resume` 续跑、thread 隔离、幂等回放 |
| `agent_service/test_service_tools.py` | Week 9 工具端点 | 工具列表、调用转发结构化信封、`is_error` 呈现、协议异常翻 503 |
| `agent_service/test_service_guards.py` | Week 9 会话与闸门 | 会话落盘与读取、幂等键命中/未命中/键相同内容不同、并发闸门排队超时 429、总时限 504 |
| `agent_service/test_service_security.py` | Week 9 安全加固 | 未认证 401（且不消耗配额）、配额耗尽 429 + `Retry-After`、请求体超限 413、探针在认证开启时豁免（判据为白名单：`/ready` 的 503 属合法应答，500/502/404 判失败） |
| `agent_service/test_service_metrics.py` | Week 9 指标与探针明细 | 路由键归一化（`/v1/tools/<name>/call` 折叠）、直方图 p95 取桶上界且溢出桶不丢样本、并发计数不为负、摘要字段齐全、探针不计入请求量、命中率把低分拒答算未命中且不调模型、`errors_by_code` 归类、幂等命中计数、`/ready` 带 `points_count` 与 `tools=N` 且反复调用不增长明细、容器未注册时中间件不影响请求 |
| `observability/*`（114 条） | Week 10 追踪层 | 配置解析与非法值静默回退、后端降级链逐级判据、JSONL 逐行落盘与坏行容忍、Langfuse 后端的类型映射与栈深上限、`traced_chat` / `traced_retriever` / `traced_tool` 三包装器（含工具返回形状规约与拒绝词表）、回调处理器把节点事件翻成 `chain` / `generation` span |
| `evaluation/*`（148 条） | Week 10 评测层 | 数据集配额与冻结校验、指标纯函数（阈值、跳过语义、分位数取桶上界）、报告渲染与轨迹提示命令、对比脚本的翻转判定（含检查项级退化）、分场景延迟与生成块替换 |

**测试架构要点**

- API 测试用 `httpx.ASGITransport` 在内存里跑整个 ASGI 栈（启动 lifespan、调端点、关闭 lifespan），**不依赖网络**；通过 `create_app(llm_factory=..., config_loader=...)` 注入假 `LlmClient`，覆盖健康/成功/各类错误全分支。
- 并发 / 流式 / 中间件测试同样依赖注入假客户端，保证离线可跑、无副作用。
- `pytest-asyncio` 设为 `asyncio_mode="auto"`，异步用例无需显式标记。
- `pythonpath=["src"]` 已配置，测试内 `from app import create_app` 等扁平的 import 可直接解析。
- Week 3 Agent 测试用 `FakeToolCapableChatModel`（`tests/fake_models.py`）假模型按预设序列返回 `AIMessage`（含 `tool_calls`），不依赖真实 LLM；工具失败场景用测试内 `@tool` 自定义 `boom_*` 函数 + `create_agent(middleware=[SafeToolMiddleware()])` 直构造验证兜底。

### 7.2 第十周：追踪与评测

#### 怎么看 trace

追踪层默认把 span 落到本地 JSONL，渲染脚本把它变成自包含 HTML（样式内联、无外部请求），
需要时用本机 Edge 的无头模式截图，不额外安装浏览器自动化工具。

```bash
# Git Bash，项目根
cd /d/workspace/py_ai/week01_ai_basics
export PATH="/c/Users/Xsz/.local/bin:$PATH"
uv run python scripts/trace_week10_view.py --list --dir logs/traces
uv run python scripts/trace_week10_view.py --dir logs/traces --out logs/traces/view.html
```

服务的 trace 落在 `logs/traces/`，评测跑出来的在 `logs/eval/traces/`，`--dir` 要与
报告里 `trace_id` 的出处一致，否则会报找不到该 trace。

```bash
# Git Bash，项目根；指定某一条并截图（示例 id 取自评测的 trace 目录）
export TRACE_ID=364525400d1c482093907137d3ce4c91
uv run python scripts/trace_week10_view.py --trace-id "$TRACE_ID" --dir logs/eval/traces --out logs/eval/view.html --screenshot docs/poho/week10/d48_trace_rag.png
```

不写 `--trace-id` 时渲染最新一条。尖括号在 bash 里是重定向符号，命令里不要写
`--trace-id <trace_id>`，那会被解析成从名为 `trace_id` 的文件读取标准输入。

本地 JSONL 与 Langfuse 是**互斥**的两条路径：一个进程只启用一个后端，`OBS_BACKEND`
选中谁就只有谁有数据。想看自托管实例的 UI（含 Session 视图）时：

```bash
# Git Bash，在 deploy/langfuse 目录下；六个容器起来后浏览器开 http://localhost:3000
docker compose up -d
```

#### 怎么跑评测

评测数据集的构建、指标口径与执行方式见 `docs/week10_evaluation.md`；下面的命令是最短路径。

```bash
# Git Bash，项目根；数据集自检，非零退出即不达标
uv run python scripts/eval_week10_build.py --check
```

```bash
# Git Bash，项目根；小样本跑通，每场景 5 条，本机约 16 分钟
uv run python scripts/eval_week10_run.py --limit 5 --tag dryrun
```

```bash
# Git Bash，项目根；三组参数对比逐组单跑，全量一轮约 47 分钟（几乎全在知识问答的推理上），建议放后台
uv run python scripts/eval_week10_run.py --tag baseline
uv run python scripts/eval_week10_run.py --tag retrieval --strategy hybrid
uv run python scripts/eval_week10_run.py --tag prompt --prompt grounded --min-score 0.7
```

```bash
# Git Bash，项目根；把三组并排成对比表并逐条列出变差用例
uv run python scripts/eval_week10_compare.py --baseline logs/eval/week10_eval_baseline.json --variant logs/eval/week10_eval_retrieval.json logs/eval/week10_eval_prompt.json --out docs/week10_eval_report.md
```

对比脚本的 `--out` 指向带生成标记的文档时只替换标记之间的数字块，手写叙述原样保留；
指向新文件时整份生成。报告与 trace 都在 `logs/` 下，该目录整体被 `.gitignore` 排除。

#### 文档索引

| 文档 | 内容 |
| --- | --- |
| `docs/week10_observability.md` | 追踪层的后端与降级链、span 字段表、配置键全表、埋点落点表、视图脚本用法、三种接入方式与前置条件 |
| `docs/week10_test_commands.md` | 追踪与埋点的逐条命令、预期输出与排障 |
| `docs/week10_evaluation.md` | 数据集构成与冻结口径、指标定义、评测执行方式、实跑结果与已知问题 |
| `docs/week10_eval_report.md` | 三组参数对比的数字、变差用例清单、Rubric 一致率与局限声明、成本口径 |
| `docs/week10_summary.md` | 本周总结（目标、结构、模块、提交、测试、失败案例、未解决项、下周计划） |
| `deploy/langfuse/README.md` | 自托管 Langfuse 的部署、起停、密钥与排障 |

---

## 8. 更新记录

> 记录 README 与项目的每周更新节点。

| 周次 | 日期 | 项目更新 | README 更新 |
| --- | --- | --- | --- |
| Week 1–3 | 2026-08 初 | 工程基线 + 模型服务（5 端点）+ 单 Agent（DevAssistantAgent，3 工具 2 中间件）；全量 **188 passed** | 初版 README：项目内容 / 目录结构 / 技术栈 / 环境 / 启动 / API / 测试（覆盖 Week 1–3） |
| Week 4 | 2026-08-14 | Dify 双工作流 DSL（`dify_workflows/`）+ `POST /dify/run` 通用透传端点 + `DifyWorkflowClient`；Dify 版 vs 代码版对比报告；全量 **194 passed** | **未更新**（本周落档时遗漏 README 同步） |
| Week 5 | 2026-08-20 | RAG 检索基础（`src/rag/`：ingestion / embeddings / retriever / qdrant_store / evaluate / probes）+ 离线可跑的 Recall@K 评测框架，五类未命中归因闭环；全量 **259 passed** | **本次更新**：补入 Week 4 + Week 5 内容——顶部概述、§1 项目内容（W4/W5 段落 + 最终实现）、§2 目录结构（新增 `src/rag`、`dify_client`、`tests` 新文件、`scripts` 新脚本、`examples/retrieval_set.json`、`dify_workflows/`、docs 新文档）、§3 技术栈（Qdrant / Embedding / Dify）、§6 API（6 端点，新增 `POST /dify/run`）、§7 测试（259 passed + 新增测试表行）、并新增本 §8 更新记录 |
| Week 6 | 2026-08-20 ~ 08-31 | RAG 应用闭环（多格式导入 / RagGenerator 拒答引用 / `POST /rag/query` / 30 条问答集 / 参数网格）；检索增强融合进 `src/rag/`；全量 **319 passed** | **未更新**（本周落档时遗漏 README 同步，Week 7 更新时一并补齐 §1/§2/§8） |
| Week 7 | 2026-08-31 ~ 09-04 | MCP 全链路（`src/jwipc_dev_mcp_server/` 6 模块 + 双传输客户端 + 冒烟/审查脚本 + 28 条 MCP 测试）；三道安全闸；全量 **350 passed, 1 skipped** | **本次更新**：顶部概述扩至 Week 7；§1 项目内容补 Week 6/Week 7 段落与最终实现；§2 目录结构补 `src/jwipc_dev_mcp_server/`、`data/mcp_sandbox/`、tests/scripts/docs 新文件；§8 补 Week 6/Week 7 两行 |
| Week 8 | 2026-09-07 ~ 09-11 | LangGraph 状态化工作流（`src/graph/` 6 模块 + 6 业务节点 / 1 澄清节点 + 4 个演示脚本 + 24 条图相关测试）；重试降级、Checkpoint 续跑、人工确认闭环；全量 **374 passed, 1 skipped** | **本次更新**：顶部概述扩至 Week 8；§1 项目内容补 Week 8 段落与最终实现（374 passed）；§2 目录结构补 `src/graph/`、`tests/graph/`；§3 技术栈补 LangGraph；§7 补 Week 8 测试与脚本命令（用例数 374、35 个文件）；§8 补 Week 8 一行 |
| Week 9 | 2026-09-14 ~ 09-18 | Agent 服务工程化（`src/agent_service/` 15 模块 + 8 个路由/探针 + 5 个脚本 + 88 条服务测试 + Dockerfile/compose + 三份文档）；安全加固（认证 / 限流 / 请求体上限 + 探针豁免）、并发闸门与超时分级错误码、幂等去重、三个探针与进程内指标、容器卷持久化；全量 **462 passed, 1 skipped** | **本次更新**：顶部概述扩至 Week 9；§1 项目内容补 Week 9 段落与最终实现（462 passed）；§7 补 Week 9 测试与脚本命令（本机 + 容器两套流程、用例数 462、36 个文件）+ 8 行服务测试表；§8 补 Week 9 一行 |
| Week 10 | 2026-09-19 ~ 09-23 | 追踪与评测层（`src/observability/` 双后端 + 三包装器 + HTML 视图、`src/evaluation/` 冻结数据集 + 纯函数指标 + 三组参数对比；追踪 114 + 评测 145 条，全量约 **721 passed, 1 skipped**） | **本次更新**：顶部概述扩至 Week 10；§1 项目内容删除「最终实现」汇总段、把 Week 1–10 全部改写为精简要点、补 Week 9 / Week 10 独立段落；§2 目录结构补 `src/observability/`、`src/evaluation/` 并刷新 tests 计数；§7 测试计数更新为约 721 passed；§8 补 Week 10 一行 |
