# 参数对比报告：chunk_size x TopK

- 生成时间：2026-08-26 11:17:03
- Embedding：EmbeddingClient（模型 mxbai-embed-large，维度 1024）
- Qdrant：mode=docker，语料 16 个源文件，检索集 20 条（有答案 18 / 无答案 2）

## 一、3x3 对比总表

| chunk_size | TopK | Recall@1 | Recall@3 | Recall@5 | 无答案误召回 | 平均检索延迟 (ms) | 导入耗时 (s) | 片段数 |
|---|---|---|---|---|---|---|---|---|
| 300 | 3 | 0.611 | 0.833 | 0.889 | 2/2 | 66.2 | 14.8 | 43 |
| 300 | 5 | 0.611 | 0.833 | 0.889 | 2/2 | 66.6 | 14.8 | 43 |
| 300 | 8 | 0.611 | 0.833 | 0.889 | 2/2 | 62.1 | 14.8 | 43 |
| 600 | 3 | 0.722 | 0.889 | 0.944 | 2/2 | 63.9 | 12.6 | 25 |
| 600 | 5 | 0.722 | 0.889 | 0.944 | 2/2 | 60.7 | 12.6 | 25 |
| 600 | 8 | 0.722 | 0.889 | 0.944 | 2/2 | 62.6 | 12.6 | 25 |
| 900 | 3 | 0.500 | 0.889 | 0.944 | 2/2 | 64.4 | 10.3 | 22 |
| 900 | 5 | 0.500 | 0.889 | 0.944 | 2/2 | 62.8 | 10.3 | 22 |
| 900 | 8 | 0.500 | 0.889 | 0.944 | 2/2 | 69.0 | 10.3 | 22 |

## 二、结论

最优组合：**chunk_size=600 + TopK=5**（Recall@1=0.722，Recall@5=0.944，平均延迟 60.7 ms）。
- chunk_size 影响片段粒度与数量：小窗口片段更细、更易定位，但片段数多、导入耗时更长；大窗口上下文更完整，但可能把不相关内容卷入同一片段。本网格中 chunk_size 是 Recall 的主因（600 的 Recall@1 明显高于 300/900）。
- TopK 在本报告中不影响 Recall@1/3/5：评测对每条查询固定取前 5 名排序计算 Recall，因此请求 TopK=3/5/8 得到的 Recall 数值一致；TopK 真正影响的是返回给生成阶段的候选条数与（边际）延迟。TopK 应按「生成所需上下文规模」选择，并用生成阶段的无答案阈值（min_score）抵消多余噪音，而非用来提升 Recall。

## 三、未命中样本（按 chunk_size 分组）

### chunk_size=300（4 条未命中）

1. **第 5 周的通过标准是什么？**
   - 期望来源：roadmap_week5.md
   - 前 5 来源：rag_pipeline.md, rag_pipeline.md, qdrant_basics.md, rag_concepts.md, rag_evaluation.md
   - 归因：roadmap_week5.md：最高分 0.6517 低于第 5 名 0.7042，（Embedding 表达不足）
2. **怎么让知识库问答避免编造答案？**
   - 期望来源：rag_concepts.md
   - 前 5 来源：qdrant_basics.md, poem_ci_鹧鸪天_晏几道.md, rag_pipeline.md, rag_evaluation.md, embedding_models.md
   - 归因：rag_concepts.md：最高分 0.5977 低于第 5 名 0.6038，（Embedding 表达不足）
3. **君不见黄河之水天上来 出自李白的哪首诗？**
   - 期望来源：（空）
   - 前 5 来源：poem_ci_卜算子_李之仪.md, poem_tang_怨情_李白.md, poem_ci_卜算子_王观.md, poem_ci_临江仙_晏几道.md, poem_tang_辋川集 鹿柴_王维.md
   - 归因：无答案样本仍返回了结果（应拒答却未拒答）
4. **2024 年巴黎奥运会开幕式的举办日期是？**
   - 期望来源：（空）
   - 前 5 来源：poem_ci_鹧鸪天_晏几道.md, qdrant_basics.md, poem_tang_横吹曲辞 出塞 一_王昌龄.md, rag_pipeline.md, rag_concepts.md
   - 归因：无答案样本仍返回了结果（应拒答却未拒答）

### chunk_size=600（3 条未命中）

1. **第 5 周的通过标准是什么？**
   - 期望来源：roadmap_week5.md
   - 前 5 来源：rag_pipeline.md, rag_evaluation.md, rag_evaluation.md, rag_concepts.md, rag_concepts.md
   - 归因：roadmap_week5.md：最高分 0.6316 低于第 5 名 0.6636，（Embedding 表达不足）
2. **君不见黄河之水天上来 出自李白的哪首诗？**
   - 期望来源：（空）
   - 前 5 来源：poem_ci_卜算子_李之仪.md, poem_tang_怨情_李白.md, poem_ci_卜算子_王观.md, poem_ci_临江仙_晏几道.md, poem_tang_辋川集 鹿柴_王维.md
   - 归因：无答案样本仍返回了结果（应拒答却未拒答）
3. **2024 年巴黎奥运会开幕式的举办日期是？**
   - 期望来源：（空）
   - 前 5 来源：poem_ci_鹧鸪天_晏几道.md, poem_tang_横吹曲辞 出塞 一_王昌龄.md, rag_pipeline.md, poem_tang_怨情_李白.md, poem_ci_卜算子_李之仪.md
   - 归因：无答案样本仍返回了结果（应拒答却未拒答）

### chunk_size=900（3 条未命中）

1. **第 5 周的通过标准是什么？**
   - 期望来源：roadmap_week5.md
   - 前 5 来源：rag_pipeline.md, rag_evaluation.md, embedding_models.md, rag_evaluation.md, rag_concepts.md
   - 归因：roadmap_week5.md：最高分 0.6355 低于第 5 名 0.6701，（Embedding 表达不足）
2. **君不见黄河之水天上来 出自李白的哪首诗？**
   - 期望来源：（空）
   - 前 5 来源：poem_ci_卜算子_李之仪.md, poem_tang_怨情_李白.md, poem_ci_卜算子_王观.md, poem_ci_临江仙_晏几道.md, poem_tang_辋川集 鹿柴_王维.md
   - 归因：无答案样本仍返回了结果（应拒答却未拒答）
3. **2024 年巴黎奥运会开幕式的举办日期是？**
   - 期望来源：（空）
   - 前 5 来源：poem_ci_鹧鸪天_晏几道.md, poem_tang_横吹曲辞 出塞 一_王昌龄.md, rag_pipeline.md, poem_tang_怨情_李白.md, poem_ci_卜算子_李之仪.md
   - 归因：无答案样本仍返回了结果（应拒答却未拒答）

## 四、检索增强方案（08-28 已落地）

以下三组件已实现并融合进 `src/rag/` 主链路（详见 4.4 实测）；均为**可选、默认关闭**——不传参数时行为与纯向量检索一致。

### 4.1 Metadata Filter（元数据过滤）

- 概念：在向量检索的同时按 Payload 元数据（source / 日期 / 类别 / 章节等）加过滤条件，缩小候选范围。
- 适用场景：知识库按来源或主题分域（如不同部门资料），查询可先限定域再检索。
- 实现：`src/rag/metadata_filter.py`（`build_filter` / `source_is` / `file_type_is`）+ `QdrantRetriever.search(metadata=...)`；写库时 payload 保留完整元数据（`file_type` / `page` 不再丢弃）。本项目 `source_filter` 精确过滤是特例，可与之 AND 合并。生成链路可通过 `RagGenerator(..., metadata=...)` 直接使用。
### 4.2 混合检索（Hybrid Search）

- 概念：向量语义检索（mxbai-embed-large）+ 关键词稀疏检索（BM25 等）双路召回，用 RRF（Reciprocal Rank Fusion）或加权合并排序。
- 适用场景：含专有名词、编号、精确术语的查询（如 `Recall@K`、`QDRANT_MODE`），向量检索易丢精确匹配，BM25 可补位；两端互补通常能显著提升 Recall@1。
- 实现：`src/rag/hybrid_search.py`（字符 bigram `BigramBM25`，不引入分词器；`HybridRetriever`，向量路与 BM25 路各取 Top-30，RRF k=60 融合）。
- 工程约定：BM25 索引按 `chunk_id` 覆盖合并（重复导入不翻倍），且首次 `search` 时若索引为空会自动从向量库 scroll 出全量点重建，因此「已导入的库直接换 hybrid 提问」无需重跑导入；排序仍由 RRF 决定，但返回的 `score` 回写为 `max(向量余弦, BM25 归一化)`，与生成阶段 `min_score`（0~1）阈值口径一致。
### 4.3 Rerank（精排）

- 概念：召回 Top-K（如 20~50 条）后用 Cross-Encoder 逐条与 query 打分重排，只把最相关的少量片段（如 3~5 条）交给生成。
- 适用场景：TopK 需要取大（保 Recall）但生成上下文必须精（降噪音、省 token）时；代价是每次查询多一轮推理，需在延迟与精度间权衡。
- 实现：`src/rag/rerank.py`（`EmbeddingReranker` 轻量复排：复用 Embedding 对 query 与候选重打分；`CrossEncoderReranker` 为占位，真实接入需 sentence-transformers + HF 模型，Ollama 不支持 Cross-Encoder 推理）。

### 4.4 落地实测（2026-08-28，真实链路）

- 环境：Ollama `mxbai-embed-large`（dim 1024）+ Docker Qdrant；chunk_size=600；检索集 20 条（有答案 18 / 无答案 2）。

| 检索变体 | Recall@1 | Recall@3 | Recall@5 | 无答案误召回 | 平均检索延迟 (ms) |
|---|---|---|---|---|---|
| 向量基线（原链路） | 0.722 | 0.889 | 0.944 | 2/2 | 65.3 |
| Metadata Filter（Oracle） | 1.000 | 1.000 | 1.000 | 2/2 | 64.8 |
| **混合检索** | **0.833** | **0.944** | **1.000** | 2/2 | 65.7 |
| 轻量精排 | 0.722 | 0.889 | 0.944 | 2/2 | ~11195 |

- 结论：
  - **混合检索 Recall@1 0.722 → 0.833（+0.111）**，Recall@5 达 1.000；BM25 以专有名词补位，救回 §三 中「第 5 周通过标准」那条 Embedding 表达不足（0.6316 < 0.6636）的未命中。
  - Metadata Filter 列为 Oracle Filter（用期望来源过滤），仅验证过滤功能正确与召回上限，不代表生产行为；生产用法是先按业务元数据（来源/类型/章节）限定域再检索。
  - 轻量精排与向量基线共用同一 Embedding 模型，Recall 持平；代价是每条查询对粗召回 20 条重新打分，真实链路延迟约 11s/20 条。价值在扩大粗召回范围，真·Cross-Encoder 精排为后续接入项（`CrossEncoderReranker` 占位）。
- 编排方式（可选、默认关闭）：统一出口两处——构造期用 `build_retriever(embedder, cfg, strategy=...)` 一键选 `vector / hybrid / rerank / hybrid+rerank` 后注入 `JwipcKnowledgeRAG(retriever=...)`；查询期用 `RagGenerator(..., metadata=...)` 或 `retrieve(..., metadata=...)` 启用元数据过滤。默认不传即原纯向量行为。脚本入口统一为 `scripts/rag_week6_qa.py`（`--strategy` / `--metadata` / `--eval`）。
- LlamaIndex 对照（Roadmap 必学项，本项目未引入库）：Reader ↔ `rag.pdf_reader` / `rag.ingestion`；Index ↔ `rag.embeddings` / `rag.qdrant_store`；Retriever ↔ `rag.retriever` / `rag.hybrid_search`；Query Engine ↔ `rag.generator`。主线以手写实现等价覆盖组件职责。
