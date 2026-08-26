# 参数对比报告：chunk_size x TopK

- 生成时间：2026-08-26 11:17:03
- Embedding：EmbeddingClient（模型 mxbai-embed-large，维度 1024）
- Qdrant：mode=docker，语料 16 个源文件，检索集 20 条（有答案 18 / 无答案 2）

## 一、2x2 对比总表

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

最优组合：**chunk_size=600 + TopK=5**（Recall@5=0.944，平均延迟 60.7 ms）。
- chunk_size 影响片段粒度与数量：小窗口片段更细、更易定位，但片段数多、导入耗时更长；大窗口上下文更完整，但可能把不相关内容卷入同一片段。
- TopK 影响召回条数与生成上下文规模：TopK 越大召回率越高，但延迟与上下文噪音随之上升；配合生成阶段的无答案阈值（min_score）可抵消部分噪音。

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

## 四、候选优化方案

### 4.1 Metadata Filter（元数据过滤）

- 概念：在向量检索的同时按 Payload 元数据（source / 日期 / 类别 / 章节等）加过滤条件，缩小候选范围。
- 适用场景：知识库按来源或主题分域（如不同部门资料），查询可先限定域再检索；本项目 ``QdrantRetriever.search`` 已支持 ``source_filter`` 精确过滤，可在此基础上扩展。
### 4.2 混合检索（Hybrid Search）

- 概念：向量语义检索（mxbai-embed-large）+ 关键词稀疏检索（BM25 等）双路召回，用 RRF（Reciprocal Rank Fusion）或加权合并排序。
- 适用场景：含专有名词、编号、精确术语的查询（如 ``Recall@K``、``QDRANT_MODE``），向量检索易丢精确匹配，BM25 可补位；两端互补通常能显著提升 Recall@1。
### 4.3 Rerank（精排）

- 概念：召回 Top-K（如 20~50 条）后用 Cross-Encoder 逐条与 query 打分重排，只把最相关的少量片段（如 3~5 条）交给生成。
- 适用场景：TopK 需要取大（保 Recall）但生成上下文必须精（降噪音、省 token）时；代价是每次查询多一轮 Cross-Encoder 推理，需在延迟与精度间权衡。
