# Week 06 Summary — week01_ai_basics

> 周期：2026-08-20 ~ 2026-08-27 ｜ 路线：南宁软件组 AI Agent 应用开发 12 周 Roadmap · 第 6 周（RAG 应用闭环）  
> 仓库：`D:\workspace\py_ai\week01_ai_basics`  
> 运行环境：Python 3.12 + uv + Ruff + pytest + FastAPI + Pydantic v2；真实链路 qwen3 + mxbai-embed-large @ Ollama + Docker Qdrant

---

## 执行摘要

- **主要进展**：本周完成 **W06 RAG 应用闭环**——在 W05 检索底座之上补齐「多格式知识库导入 → 检索增强生成（带引用/可拒答）→ RAG HTTP API → 30 条问答回归集 → chunk_size×TopK 参数网格评估」全链路，并验证真实链路（有答案带引用 confidence≈0.95，无答案全部拒答）。
- **关键变更**：新增 `JwipcKnowledgeRAG`（PDF/MD/TXT 多格式导入）、`RagGenerator`（双保险拒答 + 引用校验）、`src/rag_api.py`（`POST /rag/query`）；新增依赖 `pypdf`；测试全量 **292 passed、ruff 全绿**。
- **待关注事项**：检索层无拒答阈值，依赖生成层 `min_score`；检索增强（Metadata Filter / 混合检索 BM25+RRF / 轻量精排）已融合进 `src/rag/`（filters/bm25/rerank/hybrid 模块 + QdrantRetriever 增强 + JwipcKnowledgeRAG 可选编排），真实链路混合 Recall@1 0.722→0.833。
- **遇到的问题及解决方式**：无答案样本在检索层误召回 2/2 → 在生成层用「Top1 分数 < min_score 不调 LLM + LLM 判无答案」双保险拒答；跨文件细粒度问题（如「第 5 周通过标准」）Embedding 区分度不足 → 列入参数对比未命中归因，候选用混合检索/重排提升 Recall@1。

---

## 1. 本周目标与完成情况

| 目标（Week06 Roadmap） | 状态 | 交付物 |
| -------- | -------- | -------- |
| 多格式知识库导入（含 PDF） | ✅ 完成 | `JwipcKnowledgeRAG` + `pdf_reader.py` + `scripts/rag_week6_ingest.py` |
| 检索增强生成（带引用、可拒答） | ✅ 完成 | `RagGenerator`（`generator.py`）+ `scripts/rag_week6_qa.py` |
| chunk_size × TopK 参数对比 | ✅ 完成 | `scripts/rag_week6_param_compare.py` + `./poho/param_compare.md` |
| 30 条问答集（含无答案）回归 | ✅ 完成 | `examples/qa_set.json`（20 found + 10 unfound） |
| RAG HTTP API | ✅ 完成 | `src/rag_api.py`（`POST /rag/query`）+ `tests/test_rag_api.py` |
| 周总结（week06_summary） | ✅ 替代 | 本文件 |

---

## 2. 系统结构与关键数据流

```
文档源 (MD/TXT/PDF)
   │  JwipcKnowledgeRAG.add_directory()
   ▼
切分 (chunk_size/overlap)  →  Embedding(mxbai, dim=1024)  →  Qdrant(collection=jwipc_knowledge)
   │                                                                       ▲
   │  JwipcKnowledgeRAG.retrieve()  →  QdrantRetriever.search(top_k, source_filter)
   ▼
RagGenerator.answer(question)
   ├─ 召回 Top-K 片段
   ├─ 拒答保险①：Top1 余弦相似度 < min_score(0.3) → 不调 LLM，返回「资料中未找到」
   ├─ 组装提示词（含片段 + 引用格式约束）→ ChatFn(LLM) → 解析 {answer, has_answer, citations}
   ├─ 拒答保险②：has_answer=false → llm_no_answer 拒答
   ├─ 引用校验：citations[].chunk_id 必须落在召回集合；全非法则降级拒答
   └─ 返回 RagAnswer(answer, has_answer, citations, confidence, rejected_reason)
   │
   ▼
src/rag_api.py  POST /rag/query  (QueryReq extra=forbid → RagAnswer)
```

- 检索层只负责召回，拒答与生成在 `RagGenerator` 完成（职责分离）。
- LLM 与 RAG 均通过工厂注入（`rag_factory` / `chat_factory`）解耦，离线测试注入假实现即可跑通。

---

## 3. 主要代码模块及职责

| 模块 | 职责 |
| -------- | -------- |
| `src/rag/knowledge_rag.py` | `JwipcKnowledgeRAG`：按扩展名分派解析器（MD/TXT 走 `ingestion`，PDF 走 `pdf_reader`），统一产出 `Chunk` 写入 Qdrant；检索阶段复用 `QdrantRetriever` 仅召回。 |
| `src/rag/pdf_reader.py` | `parse_pdf()`：用 `pypdf` 逐页提取文本，按窗口切分为 `Chunk`，补充 `file_type="pdf"` 与 `page` 元数据；解析失败抛明确错误而非静默空结果。 |
| `src/rag/generator.py` | `RagGenerator`：召回→组装提示词→LLM 生成→引用校验→双保险拒答；`ChatFn` 注入解耦；`Citation`/`RagAnswer` Schema `extra="forbid"`；`NO_ANSWER_TEXT="资料中未找到"`。 |
| `src/rag_api.py` | `create_rag_app()` + `POST /rag/query`：HTTP 层编排，复用 `RagGenerator`/`RagAnswer` 契约；`QueryReq(question, top_k, min_score)` `extra="forbid"`。 |
| `scripts/rag_week6_ingest.py` | 知识库导入 CLI（重建集合 + 写入片段）。 |
| `scripts/rag_week6_qa.py` | 问答 CLI，调用 `RagGenerator.answer()` 做单条/批量问答。 |
| `scripts/rag_week6_param_compare.py` | 2 维参数网格（chunk_size × TopK）脚本：独立集合删除重建 + 复用 `rag.evaluate` + 逐条检索计时。 |
| `examples/qa_set.json` | 30 条问答集（20 expect_found + 10 无答案），字段 `question/expect_found/expect_source/note`。 |
| `tests/test_knowledge_rag.py` `test_rag_generator.py` `test_rag_qa_set.py` `test_rag_api.py` | 新增四个测试模块，覆盖导入、生成、拒答、API 与 30 条问答回归。 |

---

## 4. 主要 Git Commit

| Commit | 日期 | 说明 |
| -------- | -------- | -------- |
| `417ffbf` | 08-25 | func: app: Add multi-format document ingestion and vector retrieval orchestration |
| `9535d2c` | 08-25 | func: app: Enhance PDF parsing and add unit tests for document ingestion |
| `2c88173` | 08-25 | func: app: Add scripts for JwipcKnowledgeRAG Q\&A entry and ingestion process |
| `4da11de` | 08-25 | func: app: Add RagGenerator for retrieval-augmented generation and test cases |
| `348d91f` | 08-26 | func: app: Add rag_week6_param_compare.py for parameter grid evaluation of chunk_size and TopK |
| `59472a8` | 08-26 | docs: app: Add parameter comparison report for chunk_size and TopK evaluation |
| `b56598b` | 08-27 | fix: app: Re-completed ruff formatting |
| `5ab2d52` | 08-27 | func: app: Add Q\&A test set and RAG API service |
| `7556992` | 08-27 | func: app: Add relevant functional test cases |
| `3e76d8b` | 08-28 | func: app: Add metadata filter, hybrid search and rerank into src/rag |
| `56f312b` | 08-28 | func: app: Add unit tests for rag enhancement modules |
| `3b3851e` | 08-28 | func: app: Add RAG enhancement script and update parameter comparison script |
| `22cbb9a` | 08-28 | docs: app: Update parameter comparison report |
| `45d0191` | 08-28 | docs: app: Update and supplement some content |
| `5579293` | 08-28 | func: app: Add weekly task summary document |
| `2523909` | 08-29 | docs: app: Update commit hashes in week06 summary |
| `9765869` | 08-31 | docs: app: Add W06 knowledge base corpus |
| `ebd3b21` | 08-31 | func: app: Refactor W06 RAG into src/rag and add quote validation |
---

## 5. 测试范围与结果

- **全量测试**：Week06 周四闭环 **292 passed**；`ruff check` **All checks passed!**（08-27 commit `b56598b` 再次确认格式化通过）。
- **新增测试模块**：`test_knowledge_rag.py`（导入/解析）、`test_rag_generator.py`（生成 + 拒答 + 引用校验，含英文库+中文查询阈值验证）、`test_rag_qa_set.py`（30 条问答集回归）、`test_rag_api.py`（`POST /rag/query` 线协议 + 拒答契约，ASGITransport 冒烟）。
- **真实链路自检**（用户本机 Ollama + Docker Qdrant）：有答案 3 条带引用可溯源（confidence≈0.95），无答案 3 条全部 `llm_no_answer` 拒答；API 真实冒烟正常。
- **参数对比测试**：`rag_week6_param_compare.py` 跑 3×3 网格（chunk_size 300/600/900 × TopK 3/5/8），独立集合删旧重建 + 逐条计时。

### 参数对比结论（`docs/param_compare.md`）

| chunk_size | TopK  | Recall@1  | Recall@3  | Recall@5  | 无答案误召回 | 平均延迟(ms) | 导入(s) | 片段数 |
| ---------- | ----- | --------- | --------- | --------- | ------ | -------- | ----- | --- |
| 300        | 3/5/8 | 0.611     | 0.833     | 0.889     | 2/2    | ~63-66   | 14.8  | 43  |
| **600**    | **5** | **0.722** | **0.889** | **0.944** | 2/2    | **60.7** | 12.6  | 25  |
| 900        | 3/5/8 | 0.500     | 0.889     | 0.944     | 2/2    | ~63-69   | 10.3  | 22  |

**最优组合：`chunk_size=600 + TopK=5`**（Recall@5=0.944，平均延迟 60.7ms，片段数 25、导入 12.6s）。

> 口径说明：上表为 08-27 补充的 3×3 网格（300/600/900 × TopK 3/5/8）；`docs/param_compare.md` 正式报告为 2×2 网格（400/800 × TopK 3/5，最优 800+TopK5，Recall@5=0.944）。两者均为真实链路结果，3×3 为补充采样。

### 检索增强（融合进 `src/rag/`）

| 检索变体 | Recall@1 | Recall@3 | Recall@5 | 平均延迟(ms) |
| -------- | -------- | -------- | -------- | ------------ |
| 向量基线 | 0.722    | 0.889    | 0.944    | 65.3         |
| Metadata Filter（Oracle） | 1.000 | 1.000 | 1.000 | 64.8 |
| 混合检索（BM25+RRF） | **0.833** | **0.944** | **1.000** | 65.7 |
| 轻量精排（Embedding 复排） | 0.722 | 0.889 | 0.944 | ~11195 |

- 混合检索以 BM25 补专有名词/编号类精确匹配，Recall@1 提升 0.722→0.833；
- Metadata Filter 列为 Oracle Filter（用期望来源过滤），验证功能正确与召回上限，不代表生产行为；
- 轻量精排与基线共用同一 Embedding 模型故 Recall 持平，延迟因逐条重打分显著上升；真·Cross-Encoder 占位待接入。

> 实现位置：`src/rag/metadata_filter.py`（Metadata Filter）、`hybrid_search.py`（字符 bigram BM25 稀疏检索 + 混合检索 RRF 融合 + 精排适配）、`rerank.py`（Embedding 轻量复排 + CrossEncoder 占位）；`QdrantRetriever` 写库 payload 完整化并支持 `metadata` 过滤，`JwipcKnowledgeRAG` 通过 `build_retriever(strategy=...)` 一键选检索器（vector / hybrid / rerank / hybrid+rerank，默认不启用，行为与纯向量一致），`RagGenerator` 支持 `metadata=` 过滤透传。

---

## 6. 失败案例与定位过程

| 现象 | 定位 | 解决 |
| -------- | -------- | -------- |
| 无答案样本（如「君不见黄河之水天上来」「2024 巴黎奥运开幕日期」）检索层仍返回结果（误召回 2/2） | 检索层无拒答机制，仅按相似度返回 Top-K | 在 `RagGenerator` 加双保险：① Top1 分数 < `min_score(0.3)` 直接拒答不调 LLM；② LLM 判 `has_answer=false` 拒答。检索层保持纯召回。 |
| 「第 5 周的通过标准是什么？」跨文件细粒度问题未命中（roadmap_week5.md 最高分低于第 5 名） | Embedding 对跨文档细粒度事实区分度不足，相关片段被压到第 5 名之后 | 列入参数对比未命中归因；候选方案：混合检索（BM25 补精确匹配）、Rerank（Cross-Encoder 精排）提升 Recall@1。 |
| 测试中文 FakeEmbedding 单字哈希导致相关查询余弦 < 0.3 | 逻辑用例与阈值耦合 | 逻辑用例显式 `min_score=0.0` 与阈值解耦；阈值行为用「英文库 + 中文查询」+ dim=1024 单独验证。 |
| RAG API 启动失败 | ollama 服务无法启动，连接模型失败 | 重新配置将 ollama 模型存储地址配置正确 |
| 检索增强：轻量精排真实链路延迟约 11s/20 条 | 每条 query 对粗召回候选（20 条）逐条重新 Embedding | 报告如实记录；真·Cross-Encoder 待接入（`CrossEncoderReranker` 占位） |
| `--rebuild` 导入报 `404 Collection doesn't exist` | 集合只在 `QdrantRetriever.__init__` 建一次，脚本却在构造 RAG **之后**才删集合，写入路径不再检查集合 | 新增 `qdrant_store.recreate_collection` / `QdrantRetriever.recreate` / `JwipcKnowledgeRAG.rebuild`，按「删→建」原子语义并复用检索器自身 client（local 与 docker 行为一致）；`_ingest` 改调 `rag.rebuild()` |
| 只 `--ask --strategy hybrid` 报 `BigramBM25 has no index` | BM25 索引只在导入时建立，纯问答进程内为空 | 新增 `QdrantRetriever.load_chunks()`（scroll 全量点反推 Chunk）；`HybridRetriever` 首次 `search` 自动重建 BM25，空集合时回退纯向量不报错 |
| 混合检索结果的 `score` 是原路径原始分（BM25 动辄十几），`--min-score 0.3` 恒不触发 | RRF 融合分与余弦不同量纲，`search` 未回写统一分数 | `HybridRetriever.search` 排序仍用 RRF，但 `score` 回写为 `max(向量余弦, BM25归一化)`，统一到 0~1 阈值口径 |
| 产品手册参数类问题（`VT1000 的输入电压和尺寸`）Top1 命中封面页（23 字符）却答不出 | 短文本向量密度高，封面页 / 图表标题这类零信息碎片余弦最高，挤占 Top-K | `parse_pdf` 增加 `min_chars`（默认 50）过滤页面碎片；噪声清除后参数片段从向量路第 9 名升至混合路第 3 名、`score=1.0`，`--top-k 3` 即可命中 |
| 重复 `--ingest` 使 BM25 语料条目翻倍 | `BigramBM25.index` 直接覆盖列表，向量库按 ID 去重而 BM25 追加 | `BigramBM25` 改为 `dict[chunk_id, Chunk]` 存储 + 懒构建索引，`index` 按 chunk_id 覆盖合并；同时新增 `clear()` 供 `rebuild` 清空 |
| 引用错位：`citations[].chunk_id` 指向 A 片段、`quote` 却是 B 片段原文（如 `#p6#1` 配了 `#p5` 的「输入电压」），溯源翻错页 | 引用白名单只校验 chunk_id 在召回集合内，未校验 quote 与该片段文本的从属关系 | `_validate_or_remap()`：quote 属于该片段原文 → 保留；quote 命中**其他**召回片段 → 视为模型标错，自动改挂到正确片段（保留证据、修正归属，日志记录 remap）；任何片段都匹配不上 → 丢弃；全部非法降级 `invalid_citations`。真实链路验证：错位的「输入电压」引用被改挂回 `#p5#0`，两条引用均可溯源 |

---

## 7. 使用 AI 辅助的内容及人工验证

- **AI 辅助**：代码骨架、模块功能、单元测试与问答集结构、参数对比脚本与报告文档由 ai 辅助完成。
- **人工验证**（本机真实链路：Ollama qwen3 + mxbai-embed-large @ Docker Qdrant）：
  - 拉起服务跑通 RAG 全链路——有答案带引用可溯源（confidence≈0.95）、无答案全部 `llm_no_answer` 拒答；RAG API（`POST /rag/query`）真实冒烟正常。
  - 参数对比脚本在真实环境复跑：确认 chunk_size 是 Recall 主因（600 最优 Recall@1=0.722），混合检索把 Recall@1 从 0.722 提升到 0.833，轻量精排与基线持平但延迟约 11s/20 条。
  - `ruff` 与 `pytest`（全量 323 passed、ruff 全绿）在真实环境测试确认。
  - 补充知识库文件（产品手册 PDF + 长篇 TXT）实跑：`--rebuild` 重建集合 51 片段无 404；`--strategy hybrid --top-k 5` 不带 `--ingest` 直接问答，BM25 索引自动从向量库重建；`--debug` 打印的召回明细确认参数片段（VT1000 规格表）以 `score=1.0` 进入 Top-3。
  - **环境配置问题（已解决）**：本机 Ollama 模型仓库在非默认 D 盘（`D:\Xsz\ollama`）。若 Ollama 设置里的 Model location 未指向该根目录、或误填子路径（如 `manifests\registry.ollama.ai\library`），会导致 `ollama list` 为空、API 报 `model not found`；修正为指向 `D:\Xsz\ollama` 根目录后正常。另 `localhost` 优先解析 IPv6 致 11434 端口串服务，统一用 `OLLAMA_HOST=0.0.0.0` 双栈绑定规避。

---

## 8. 当前未解决问题

1. **检索层无拒答阈值**：无答案依赖生成层 `min_score`，检索层仍可能返回低分片段；可考虑检索层打分过滤或 Metadata Filter 缩小候选。
2. **LlamaIndex未直接使用**：当前项目中未直接使用LlamaIndex及其相关组件，通过设计相似的程序进行了替代。
3. **长文档单次向量化会超时**：`EmbeddingClient` 的 HTTP 超时为构造默认 30 秒（未接 `.env` 的 `TIMEOUT_SECONDS`），而 `QdrantRetriever.index` 把整个文件的片段一次性打包请求；实测 Ollama mxbai-embed-large 约 0.85 秒/条，单文件片段数超过约 32 条即超时（如《白鹿原》1506 条）。当前靠控制 chunk_size 与文件体量规避，后续需按批 embed + 逐批 upsert + 进度与断点续跑。
4. **图文排版 PDF 的表格参数提取不到**：pypdf 只能拿到文字层，规格表 / 参数表若在图片或矢量排版中则无法检索，需 OCR 或换用表格提取库。
5. **同一份产品手册含多机型规格时答案会漂移**：如 VT1000 用户手册内并存两组参数（`#p4` 为 19V DC IN，`#p5` 为 DC IN 12V / 249.83×168.43×38.95mm），问「VT1000 的输入电压」时答案取决于召回命中哪一段。需要在 Prompt 中要求模型核对「型号 + 参数」成对出现，或在切片时把机型标题与规格表绑定为同一片段。

---

## 9. 本周知识概念

- **Metadata Filter（元数据过滤）**：在向量检索之上叠加结构化过滤（来源、文件类型等），通过 Qdrant Payload 的 `MatchValue` / `MatchAny` 实现，多条件 AND。适用：需按来源/类型限定检索范围、缩小候选、降低噪声。
- **混合检索（Hybrid Search）**：向量语义召回 + 关键词（BM25）召回双路融合。向量擅长语义泛化但易漏专有名词，BM25 补足精确词面匹配；融合用 RRF（Reciprocal Rank Fusion，k=60）按排名 reciprocal 加权、不依赖分数尺度。真实链路 Recall@1 由 0.722 升至 0.833。
- **Rerank（重排序）**：对粗召回 Top-N 候选重新打分取 Top-K。轻量方案复用 Embedding 余弦复排（零额外依赖）；生产级用 CrossEncoder 交叉编码（精度更高、需额外模型，本轮留占位 `CrossEncoderReranker`）。适用：粗召回噪声大、需提升 Top-K 精度。
- **Recall@K 评测口径**：Recall@1/3/5 取决于召回排序，与请求 `TopK` 无关（评测固定取前 5 名排序统计），`TopK` 仅影响返回候选条数与延迟。
- **拒答机制分层**：检索层只负责召回、不做拒答；拒答在生成层——Top1 分数 `< min_score` 不调 LLM + LLM 判定 `has_answer=false`，双重保险。
- **LlamaIndex 组件对照（概念对齐，未引入该库）**：Reader↔`pdf_reader`/ingestion、Index↔`embeddings`/`qdrant_store`、Retriever↔`retriever`/`hybrid`、Query Engine↔`generator`，本轮以直接编写的模块实现替代，暂未使用额外框架依赖。

---

## 10. 下周计划（Roadmap 第 7 周：MCP——把内部能力做成标准工具）

- **下周目标**：理解 Host / Client / Server，开发安全、可描述、可复用的 MCP Tool。
- **必学知识**：MCP Tools / Resources / Prompts、JSON-RPC、Capability 与生命周期；stdio 与 Streamable HTTP；结构化工具输出、输入校验与错误返回；最小权限、只读优先、危险操作人工确认、路径与命令白名单。
- **阶段任务**：
  - 开发 `jwipc-dev-mcp-server`，提供 `list_files`、`read_file`、`git_log`、`check_commit_message` 四个工具。
  - 先完成 stdio Server / Client，再做本地 Streamable HTTP 版本（**不实现旧 HTTP+SSE**）。
  - 所有文件工具限制在训练根目录；所有参数使用 Pydantic 校验。
  - 写 **15 条工具测试 + 10 条安全测试**。
- **交付物**：MCP Server、MCP Client、工具说明、安全测试报告、`docs/week07_summary.md`。
- **通过标准**：可被兼容客户端发现和调用；旧 HTTP+SSE 不作为实现。
- **官方参考**：MCP 入门 / MCP 2025-11-25 Spec / MCP Python SDK。

---

## 11. 操作与测试步骤

> **运行环境**：以下命令均在 **Git Bash**（项目根目录 `D:\workspace\py_ai\week01_ai_basics`）执行。真实链路需先拉起 Ollama（qwen3 + mxbai-embed-large）与 Docker Qdrant；离线冒烟用环境变量切换。

### 11.1 环境准备

```bash
# 真实链路
export EMBEDDING_API_BASE_URL="http://localhost:11434/v1"
export EMBEDDING_MODEL_NAME="mxbai-embed-large"
export QDRANT_MODE="docker"
# 离线冒烟（FakeEmbedding + 内存 Qdrant）
export QDRANT_MODE="local" && export QDRANT_PATH=":memory:"
```

```bash
uv run python -c "from rag.embeddings import get_embedding; print(get_embedding())"
```

预期：真实链路打印 `EmbeddingClient(...)`；离线打印 `FakeEmbedding(dim=64)`。

### 11.2 知识库导入（多格式 MD/TXT/PDF）

```bash
uv run python scripts/rag_week6_ingest.py --ingest data/raw --collection jwipc_knowledge
# 清空重建后导入
uv run python scripts/rag_week6_ingest.py --ingest data/raw --collection jwipc_knowledge --rebuild
```

预期：按扩展名分派解析器，写入 Qdrant 集合 `jwipc_knowledge`，打印片段数与导入耗时。

![知识库 pytest](./poho/week6/d26_pt_knowledge.png)
![PDF/导入重建](./poho/week6/d27_ingest_rebuild.png)
![导入后问答](./poho/week6/d27_ingest_ask.png)
![ruff 检查](./poho/week6/d27_ruff.png)

### 11.3 检索增强生成问答（带引用 / 可拒答）

```bash
uv run python scripts/rag_week6_qa.py --ingest data/raw --ask "第5周的通过标准是什么？"
```

预期：有答案返回带引用片段（confidence≈0.95）；无答案问题返回「资料中未找到」并 `llm_no_answer` 拒答。

![单条问答](./poho/week6/d27_ask.png)
![无答案拒答](./poho/week6/d29_noanwser.png)

### 11.4 参数对比（chunk_size × TopK）

```bash
# 离线冒烟
uv run python scripts/rag_week6_param_compare.py --offline
# 真实链路 3×3 网格
uv run python scripts/rag_week6_param_compare.py --chunk-sizes 300,600,900 --top-ks 3,5,8
```

预期：生成 `docs/param_compare.md`，输出对比表与最优组合（chunk_size=600 + TopK=5，Recall@5=0.944）。

![参数对比运行](./poho/week6/d28_compare.png)
![离线对照](./poho/week6/d28_cp_offline.png)

### 11.5 检索增强组件用法（Metadata Filter / 混合检索 / Rerank）

统一入口为 `scripts/rag_week6_qa.py`：`--strategy` 选召回方式、`--metadata` 加过滤、`--ask` 提问题。下面所有示例基于你新增的 4 个知识库文件（均在 `data/raw/`：`VT1000用户手册-V1.0--2025.02.19.pdf`、`S102H&S102HT 中英文简易使用指南--Rev1.0--2023.10.10.pdf`、`「外国文学」《追风筝的人》…txt`、`诗经.txt`），集合名统一用 `jwipc_v3`。

> 文件名含 `&` 与空格，但导入是按**目录**（`--ingest data/raw`）进行，不会触发 shell 解析问题，无需给文件名加引号。

#### 前置：用新语料构建 / 重建集合（只做一次）

```bash
uv run python scripts/rag_week6_qa.py --ingest data/raw --collection jwipc_v3 --chunk-size 800 --rebuild
```

#### 完整测试指令（基于真实文件）

```bash
# 混合检索问答（BM25 为空时首次 search 自动从向量库重建，无需带 --ingest）
# 对应：VT1000用户手册 PDF
uv run python scripts/rag_week6_qa.py --collection jwipc_v3 --ask "VT1000 的输入电压和尺寸是多少？" --strategy hybrid

# 只从 PDF 召回（可与任意 --strategy 叠加）
# 对应：S102H&S102HT 使用指南 PDF
uv run python scripts/rag_week6_qa.py --collection jwipc_v3 --ask "S102H 支持哪些接口和硬件规格？" --metadata "file_type=pdf" --strategy hybrid
# TXT 同理：--metadata "file_type=txt" 只查《追风筝的人》与《诗经》

# 四种策略批量评测（vector / hybrid / rerank / hybrid+rerank，输出 Recall@K 表）
uv run python scripts/rag_week6_qa.py --ingest data/raw --eval

# 清空重建集合后重新导入（--rebuild 走 rag.rebuild()，先删后建）
uv run python scripts/rag_week6_qa.py --ingest data/raw --collection jwipc_v3 --chunk-size 800 --rebuild

# 拒答 / 引用异常时打印召回明细（chunk_id / score / 字符数）与 LLM 原始输出，便于排查
# 对应：诗经.txt
uv run python scripts/rag_week6_qa.py --collection jwipc_v3 --ask "《诗经》分为风、雅、颂哪几类，各有多少篇？" --strategy hybrid --debug

# 关闭「LLM 判无答案 → top_k×2 重召再问一次」重试（省一次调用）
# 对应：「外国文学」《追风筝的人》.txt
uv run python scripts/rag_week6_qa.py --collection jwipc_v3 --ask "《追风筝的人》的作者是谁？讲述了什么故事？" --no-answer-retry 1

# 重排序（在混合召回后精排，提升正确答案排序与引用准确度）
uv run python scripts/rag_week6_qa.py --collection jwipc_v3 --ask "S102HT 与 S102H 有什么区别？" --strategy hybrid+rerank
```

四个文件各一条代表性问答（快速验证召回命中）：

```bash
uv run python scripts/rag_week6_qa.py --collection jwipc_v3 --ask "VT1000 的电源接口和供电规格是什么？" --strategy hybrid
uv run python scripts/rag_week6_qa.py --collection jwipc_v3 --ask "S102HT 与 S102H 有什么区别？" --strategy hybrid
uv run python scripts/rag_week6_qa.py --collection jwipc_v3 --ask "《追风筝的人》的主要人物和情节是什么？" --strategy hybrid
uv run python scripts/rag_week6_qa.py --collection jwipc_v3 --ask "《诗经》共有多少篇？" --strategy hybrid
```

#### 指令调用说明（什么情况用什么）

| 你想做什么 | 用哪些参数 | 说明 |
|---|---|---|
| 快速问答，问题表述和原文接近 | 默认即可（`--strategy vector` 可省略） | 纯向量召回，速度最快 |
| 问题含专有名词 / 型号 / 缩写，纯向量答不准 | `--strategy hybrid` | 向量 + BM25 互补，救回术语类漏召 |
| 只查某类文件（缩小范围） | `--metadata "file_type=pdf"` 或 `file_type=txt` | 可叠加任意 `--strategy` |
| 召回条数多、正确答案排不到最前、引用不准 | `--strategy rerank` 或 `--strategy hybrid+rerank` | rerank 在召回后精排；`hybrid+rerank` = 混合召回再精排 |
| 答不上来 / 引用错位，想看召回细节 | `--debug` | 打印每次召回的 score / len / chunk_id 与 LLM 原始输出 |
| 确认会拒答、想省一次 LLM 调用 | `--no-answer-retry 1` | 关闭「无答案 → 扩召回重试」 |
| 语料有变动 / 首次导入 | `--ingest data/raw --rebuild` | 先删后建，重建集合 |
| 对比四种策略效果 | `--ingest data/raw --eval` | 输出 Recall@1/3/5 表 |

行为约定：

1. **混合检索的 BM25 索引按需重建**：导入时写入；对已有集合单独提问时，首次 `search` 会从 Qdrant scroll 出全部点反推语料重建（仅 hybrid / hybrid+rerank 生效），无需重跑 `--ingest`。
2. **混合检索的 `score` 为 0~1 阈值口径**：排序仍由 RRF 决定，但返回的 `score` 取「向量余弦 / BM25 归一化分」的较大值，使 `--min-score` 在混合模式下按与纯向量一致的语义生效。
3. **生成侧拒答重试**：LLM 首判 `has_answer=false` 时用 `top_k × 2` 重新召回再问一次（`--no-answer-retry`，默认 2，传 1 关闭），仍无答案才拒答。第二轮提示词更大，资源紧张的环境可用 `--no-answer-retry 1` 关闭。

### 11.6 RAG API 冒烟

```bash
uv run pytest tests/test_rag_api.py -q
```

预期：通过 `POST /rag/query` 线协议与拒答契约测试（ASGITransport 冒烟）。

![RAG API 冒烟](./poho/week6/d29_ragapi.png)
![API 测试](./poho/week6/d29_pt_api.png)

### 11.7 全量回归与格式检查

```bash
uv run pytest -q
uv run ruff check .
```

预期：全量 **292 passed**；`ruff` 全绿（All checks passed!）。

![pytest 全部](./poho/week6/d27_ptall.png)
![ruff 终检](./poho/week6/d29_ruff.png)

