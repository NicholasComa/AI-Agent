# AI Agent 应用开发

> **第 1–7 周 · 工程基线 + 模型服务 + 单 Agent + Dify 工作流 + RAG 检索/闭环 + MCP 工具**
> 「12 周 AI Agent 应用开发 Roadmap」的落地工程。Week 1–3 建立可复现的工程环境与模型服务、单 Agent；Week 4 用 Dify 可视化工作流复现场景并对照代码版，由 FastAPI 统一包装；Week 5–6 落地 RAG 检索与 RAG 应用闭环（解析 / 切分 / Embedding / Qdrant / Recall@K 评测 / 带引用可拒答的生成链路）；Week 7 把内部能力做成 MCP 标准工具（stdio + Streamable HTTP 双传输，路径/命令白名单 + 确认门三道安全闸）。

---

## 1. 项目内容

本仓库是「12 周 AI Agent 应用开发 Roadmap」**第 1–5 周**的落地工程。目标是建立可复现的 Python AI 应用环境、统一的代码质量与测试基线，并依次交付模型服务、单 Agent、Dify 工作流对照与 RAG 检索能力，为后续 7 周打好地基。

**Week 1 · 工程基线（Day 1–5，打地基）**

- **Day 1 · 工程基线**：用 `uv` + `pyproject.toml` + `uv.lock` 搭建 Python 3.12 统一开发环境，引入 Ruff（lint + format）与 pytest 质量基线。
- **Day 2 · 配置与 JSON**：`src/config.py` 强类型配置加载（API key / base url / model name，来源 `.env` 或 `config.json`），Pydantic 校验。
- **Day 3 · 模型客户端**：`src/llm_client.py`，基于 `httpx` 异步调用 OpenAI 兼容的 chat-completions 接口，封装 `LlmAuthError` / `LlmTimeoutError` / `LlmServerError` 等错误信封。
- **Day 4 · 结构化输出**：`src/schemas.py`（`RequirementAnalysis` 等输出模型）与 `src/prompts.py`（系统提示 + 消息构造），JSON Mode 做结构化需求分析。
- **Day 5 · FastAPI 服务**：`src/app.py`（应用工厂 + `/health` `/chat` `/analyze-requirement` 端点 + 统一异常处理）与 `src/main.py`。

**Week 2 · 模型服务（Day 6–10，升级为稳定服务）**

将 Week 1 的最小服务升级为**稳定、可配置、可测试**的 FastAPI 模型服务：配置改用 `pydantic-settings` 从 `.env` 读取；新增 `ModelClient` Protocol 抽象与工厂；加入 `RequestId` + 访问日志中间件与 JSON 结构化日志（`request_id` 链路追踪）；新增 `GET /models` 与 `POST /chat/stream`（SSE 流式）；`asyncio.Semaphore` 并发限制；所有异常经统一映射为 `ErrorBody`（含 `request_id`）。最终交付 5 个端点、106 条测试全部通过。

**Week 3 · 单 Agent 与 Tool Calling（Day 11–15，引入 LangChain v1）**

基于 LangChain v1 的 `create_agent` 实现 `DevAssistantAgent`：提供 `calculator`（安全算术）、`read_text_file`（沙箱防穿越）、`check_commit_message`（commit 规范校验）三个工具；用 `AgentMiddleware` 实现 `TraceMiddleware`（三钩子记录 Agent Loop 关键节点）与 `SafeToolMiddleware`（工具异常兜底转 `TOOL_ERROR:`）；`DEFAULT_RECURSION_LIMIT=8` 防无限循环。新增 82 条测试（Agent Loop + 工具 + 中间件 + 20 条端到端用例覆盖 4 类场景），全量 188 passed。Agent 目前仅被 pytest 驱动，未接真实模型与交互界面。

**Week 4 · Dify 工作流与代码版对照（Day 16–20）**

用 Dify 可视化工作流理解节点 / 变量 / 分支 / 知识检索，并与代码版 Agent 对照：

- **Day 17 · DevAssistantAgent_Dify**：在 Dify 复现 Week 3 场景为四分支工作流（calculator / check_commit / knowledge_retrieval / chat），导出 `dify_workflows/DevAssistantAgent_Dify.yml`。
- **Day 18 · RequirementAnalysis_Dify**：搭建「需求文本 → 参数提取 → 分类 → 风险分析 → 结构化输出」工作流，导出 `dify_workflows/RequirementAnalysis_Dify.yml`。
- **Day 19 · POST /dify/run**：新增 `src/dify_client.py`（`DifyWorkflowClient` 异步客户端）与 `src/api_models.py` 的 `DifyRunRequest/Response`；`src/app.py` 增加通用透传端点 `POST /dify/run`，靠 `.env` 的 `DIFY_API_KEY` 决定调用哪个工作流。
- **Day 20 · 对比报告**：`docs/day20_dify_vs_code_report.md` 对比 Dify 版与代码版在调试、版本管理、扩展、部署上的差异。
- 新增 6 条 Dify 相关测试（客户端 + 路由），全量 **194 passed**；本机四分支 API 联调全通过。

**Week 5 · RAG 检索基础（Day 21–25）**

落地最小可用的 RAG 检索（不含生成），打通「文档解析 → 切分 → Embedding → Qdrant → TopK 检索」并做 Recall@K 评测：

- `src/rag/ingestion.py`：加载 `.md/.txt`、按 `chunk_size/overlap` 切分、保留 `source` 元数据。
- `src/rag/embeddings.py`：`EmbeddingClient` / `FakeEmbedding` / `get_embedding` 工厂（真实用 `mxbai-embed-large`，离线用假向量）。
- `src/rag/retriever.py`：`ListRetriever`（离线兜底）+ `QdrantRetriever`（主链路，返回 `chunk_id/source/score/text`，不调 LLM）。
- `src/rag/qdrant_store.py`：`QdrantConfig` + connect / ensure_collection / upsert / search。
- `src/rag/evaluate.py`：`load_dataset` / `evaluate` Recall@K / `diagnose_miss`（五类未命中归因）/ `write_report`。
- 评测：`examples/retrieval_set.json`（20 条人工标注）+ `scripts/rag_week5_eval.py`，真实环境 Recall@1=0.778 / @3=0.833 / @5=0.889。
- 新增 RAG 相关测试（`test_rag_*`、`test_qdrant_store`、`test_embedding_client`、`test_dify_*` 之外的 rag 系列），全量 **259 passed**。

**Week 6 · RAG 应用闭环（Day 26–30）**

在 W5 检索底座上补齐「多格式知识库导入 → 检索增强生成（带引用/可拒答）→ RAG HTTP API → 30 条问答回归集 → chunk_size×TopK 参数网格评估」：`JwipcKnowledgeRAG`（PDF/MD/TXT 导入）、`RagGenerator`（双保险拒答 + 引用校验）、`src/rag_api.py`（`POST /rag/query`）；检索增强（Metadata Filter / 混合检索 BM25+RRF / 轻量精排）融合进 `src/rag/`（可选、默认关闭），真实链路混合检索 Recall@1 0.722→0.833。详见 `docs/week06_summary.md` 与 `docs/param_compare.md`。

**Week 7 · MCP：把内部能力做成标准工具（Day 31–34）**

理解 Host / Client / Server 与 JSON-RPC 2.0，开发安全、可描述、可复用的 MCP Tool（SDK 锁 `mcp==1.29.1`，规范 `2025-11-25`，不实现旧 HTTP+SSE）：

- `src/jwipc_dev_mcp_server/`：`server.py`（FastMCP 装配 + `ping`）、`config.py`（沙箱与上限，`JWIPC_MCP_*` 环境变量）、`security.py`（三道闸）、`schemas.py`（出参模型，全 `extra="forbid"` + 统一 ok/error/kind 信封）、`tools.py`（4 工具 + `ToolAnnotations` 只读声明）、`client.py`（双传输客户端封装，stdio 与 Streamable HTTP）。
- **4 个工具**：`list_files` / `read_file`（超 50KB 需确认、二进制拒绝）/ `git_log`（服务端固定拼装 argv + 白名单校验 + 15s 超时）/ `check_commit_message`（复用 Week 3 校验器）。
- **三道安全闸**：`SandboxRoot` 路径白名单（拒 `..` 穿越/绝对路径/盘符/空字节/符号链接逃逸）、`GitCommandPolicy` 命令白名单（只读子命令 + 参数 allowlist）、`ConfirmationGate` 确认门（大文件二次确认）。
- **双传输实测**：stdio（子进程 + 管道）与 Streamable HTTP（`http://127.0.0.1:8765/mcp`）全链路打通；stdio 下 stdout 专用于协议、日志走 stderr。
- **业务串联**：`scripts/mcp_week7_review.py`「审查一次提交改动」——客户端编排 `git_log → check_commit_message → list_files → read_file`，输出结构化审查结论；`--demo-errors` 演示 3 异常场景（`forbidden` / `needs_confirmation` / `argument_rejected`）。
- 新增 28 条 MCP 测试（15 工具 + 10 安全 + 3 双传输），全量 **350 passed, 1 skipped**；文档：`docs/week07_tools.md` / `docs/week07_security_report.md` / `docs/week07_summary.md`。

**最终实现**

- Week 1–2：一个 FastAPI 服务，暴露 5 个端点（健康检查、对话转发、流式对话、模型清单、结构化需求分析）；统一的错误信封（`ErrorBody`）；完整的 `request_id` 链路。
- Week 3：`DevAssistantAgent`（`src/devagent/`）—— 单 Agent + 3 工具 + 2 中间件（追踪 + 兜底）+ 82 条测试；Agent Loop 四个终止条件（直接 final / 调工具→结果→final / 异常兜底 / recursion_limit 截断）。
- Week 4：Dify 双工作流 DSL（`dify_workflows/`）+ `POST /dify/run` 通用透传端点 + `DifyWorkflowClient`；Dify 版与代码版对照报告。
- Week 5：`src/rag/`（ingestion / embeddings / retriever / qdrant_store / evaluate / probes）+ 离线可跑的 RAG 评测框架，Recall@K 达标、五类未命中归因闭环。
- Week 6：RAG 应用闭环——多格式导入、带引用可拒答的生成链路、`POST /rag/query`、30 条问答回归集、参数网格评估；检索增强融合进 `src/rag/`（可选）。
- Week 7：`src/jwipc_dev_mcp_server/`（6 模块 MCP 包）+ 双传输客户端 + 业务流水线脚本 + 28 条 MCP 测试；三道安全闸（路径白名单 / 命令白名单 / 确认门）。
- 完整的测试验证：截至 Week 7，`ruff` 通过、`pytest` **350 passed, 1 skipped**（skip 为 Windows 符号链接权限限制）。

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
│   └── jwipc_dev_mcp_server/  # Week 7 MCP 服务（stdio + Streamable HTTP）
│       ├── __init__.py      # 导出 build_server / main / SERVER_NAME
│       ├── server.py        # FastMCP 装配 + ping + 命令行入口（--transport/--host/--port）
│       ├── config.py        # McpServerConfig 沙箱与上限（JWIPC_MCP_* 环境变量）
│       ├── security.py      # SandboxRoot 路径白名单 / GitCommandPolicy 命令白名单 / ConfirmationGate
│       ├── schemas.py       # 出参模型（ToolResult 信封 + tool_failure 统一错误）
│       ├── tools.py         # list_files / read_file / git_log / check_commit_message
│       └── client.py        # 双传输客户端封装（connect_stdio / connect_http）
├── data/
│   └── mcp_sandbox/         # Week 7 沙箱夹具（.gitignore 忽略，不入库）
├── tests/                   # pytest 测试（32 个文件，350 用例 + 1 skip）
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
│   ├── test_mcp_security.py        # Week 7 SandboxRoot / GitCommandPolicy / ConfirmationGate（10 条）
│   └── test_mcp_client.py          # Week 7 双传输客户端端到端（3 条）
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
│   └── mcp_week7_review.py    # Week 7 业务流水线「审查一次提交改动」（--demo-errors 演示异常）
├── dify_workflows/          # Week 4 导出的 Dify 工作流 DSL
│   ├── DevAssistantAgent_Dify.yml   # Day 17 四分支工作流（calculator/check_commit/kb/chat）
│   └── RequirementAnalysis_Dify.yml # Day 18 需求分析工作流
├── logs/                    # 脚本运行日志（不入库）
│   └── ollama_run.log
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
    └── poho/                # 运行 / 测试截图（不入库）
```

> 本项目的核心交付目录为 `src/`、`tests/`、`docs/`；`main.py`（demo 入口）、`config.example.json`、`scripts/`、`examples/`、`logs/` 为辅助文件。`Dockerfile` / `compose.yaml` 计划从第 9 周加入。

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

---

## 7. 测试与质量

```bash
# 全部测试（截至 Week 7 共 350 passed + 1 skipped）
uv run pytest -q

# 按模块运行（部分示例）
uv run pytest -v tests/test_api.py
uv run pytest -v tests/test_streaming.py
uv run pytest -v tests/test_atool_calling.py
uv run pytest -v tests/test_dify_client.py tests/test_app_dify.py
uv run pytest -v tests/test_rag_evaluate.py tests/test_rag_probes.py

# 本周新增模块单测（MCP 28 条）
uv run pytest -v tests/test_mcp_server_tools.py tests/test_mcp_security.py tests/test_mcp_client.py

# 单个用例
uv run pytest -v tests/test_api.py::test_chat_timeout_returns_504

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

**测试内容（32 个文件 / 350 用例 + 1 skip）**

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

**测试架构要点**

- API 测试用 `httpx.ASGITransport` 在内存里跑整个 ASGI 栈（启动 lifespan、调端点、关闭 lifespan），**不依赖网络**；通过 `create_app(llm_factory=..., config_loader=...)` 注入假 `LlmClient`，覆盖健康/成功/各类错误全分支。
- 并发 / 流式 / 中间件测试同样依赖注入假客户端，保证离线可跑、无副作用。
- `pytest-asyncio` 设为 `asyncio_mode="auto"`，异步用例无需显式标记。
- `pythonpath=["src"]` 已配置，测试内 `from app import create_app` 等扁平的 import 可直接解析。
- Week 3 Agent 测试用 `FakeToolCapableChatModel`（`tests/fake_models.py`）假模型按预设序列返回 `AIMessage`（含 `tool_calls`），不依赖真实 LLM；工具失败场景用测试内 `@tool` 自定义 `boom_*` 函数 + `create_agent(middleware=[SafeToolMiddleware()])` 直构造验证兜底。

---

## 8. 更新记录

> 记录 README 与项目的每周更新节点。

| 周次 | 日期 | 项目更新 | README 更新 |
| --- | --- | --- | --- |
| Week 1–3 | 2026-08 初 | 工程基线 + 模型服务（5 端点）+ 单 Agent（DevAssistantAgent，3 工具 2 中间件）；全量 **188 passed** | 初版 README：项目内容 / 目录结构 / 技术栈 / 环境 / 启动 / API / 测试（覆盖 Week 1–3） |
| Week 4 | 2026-08-14 | Dify 双工作流 DSL（`dify_workflows/`）+ `POST /dify/run` 通用透传端点 + `DifyWorkflowClient`；Dify 版 vs 代码版对比报告；全量 **194 passed** | **未更新**（本周落档时遗漏 README 同步） |
| Week 5 | 2026-08-20 | RAG 检索基础（`src/rag/`：ingestion / embeddings / retriever / qdrant_store / evaluate / probes）+ 离线可跑的 Recall@K 评测框架，五类未命中归因闭环；全量 **259 passed** | **本次更新**：补入 Week 4 + Week 5 内容——顶部概述、§1 项目内容（W4/W5 段落 + 最终实现）、§2 目录结构（新增 `src/rag`、`dify_client`、`tests` 新文件、`scripts` 新脚本、`examples/retrieval_set.json`、`dify_workflows/`、docs 新文档）、§3 技术栈（Qdrant / Embedding / Dify）、§6 API（6 端点，新增 `POST /dify/run`）、§7 测试（259 passed + 新增测试表行）、并新增本 §8 更新记录 |
| Week 6 | 2026-08-20 ~ 08-31 | RAG 应用闭环（多格式导入 / RagGenerator 拒答引用 / `POST /rag/query` / 30 条问答集 / 参数网格）；检索增强融合进 `src/rag/`；全量 **319 passed** | **未更新**（本周落档时遗漏 README 同步，Week 7 更新时一并补齐 §1/§2/§8） |
| Week 7 | 2026-08-31 ~ 09-02 | MCP 全链路（`src/jwipc_dev_mcp_server/` 6 模块 + 双传输客户端 + 冒烟/审查脚本 + 28 条 MCP 测试）；三道安全闸；全量 **350 passed, 1 skipped** | **本次更新**：顶部概述扩至 Week 7；§1 项目内容补 Week 6/Week 7 段落与最终实现；§2 目录结构补 `src/jwipc_dev_mcp_server/`、`data/mcp_sandbox/`、tests/scripts/docs 新文件；§8 补 Week 6/Week 7 两行 |
