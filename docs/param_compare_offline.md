# 参数对比报告：chunk_size x TopK

- 生成时间：2026-08-26 11:23:39
- Embedding：FakeEmbedding（模型 FakeEmbedding(离线)，维度 1024）
- Qdrant：mode=docker，语料 16 个源文件，检索集 20 条（有答案 18 / 无答案 2）
- **口径说明**：离线 FakeEmbedding 运行，Recall 仅供流程验证，不代表真实语义水平

## 一、2x2 对比总表

| chunk_size | TopK | Recall@1 | Recall@3 | Recall@5 | 无答案误召回 | 平均检索延迟 (ms) | 导入耗时 (s) | 片段数 |
|---|---|---|---|---|---|---|---|---|
| 400 | 3 | 0.722 | 0.833 | 0.889 | 2/2 | 9.5 | 0.3 | 35 |
| 400 | 5 | 0.722 | 0.833 | 0.889 | 2/2 | 15.0 | 0.3 | 35 |
| 800 | 3 | 0.556 | 0.889 | 0.889 | 2/2 | 13.6 | 0.3 | 22 |
| 800 | 5 | 0.556 | 0.889 | 0.889 | 2/2 | 9.2 | 0.3 | 22 |

## 二、结论

最优组合：**chunk_size=400 + TopK=3**（Recall@1=0.722，Recall@5=0.889，平均延迟 9.5 ms）。
- chunk_size 影响片段粒度与数量：小窗口片段更细、更易定位，但片段数多、导入耗时更长；大窗口上下文更完整，但可能把不相关内容卷入同一片段。本网格中 chunk_size 是 Recall 的主因（400 的 Recall@1 高于 800）。
- TopK 在本报告中不影响 Recall@1/3/5：评测对每条查询固定取前 5 名排序计算 Recall，因此请求 TopK=3/5 得到的 Recall 数值一致；TopK 真正影响的是返回给生成阶段的候选条数与（边际）延迟。TopK 应按「生成所需上下文规模」选择，并用生成阶段的无答案阈值（min_score）抵消多余噪音，而非用来提升 Recall。

## 三、未命中样本（按 chunk_size 分组）

### chunk_size=400（4 条未命中）

1. **文本向量化之后，检索问题变成了什么计算？**
   - 期望来源：rag_concepts.md
   - 前 5 来源：embedding_models.md, embedding_models.md, poem_tang_渭城曲_王维.md, roadmap_week5.md, embedding_models.md
   - 归因：rag_concepts.md：最高分 0.0000 不低于第 5 名 0.0000，正确片段被排挤出候选（TopK / 排序问题）
2. **怎么让知识库问答避免编造答案？**
   - 期望来源：rag_concepts.md
   - 前 5 来源：qdrant_basics.md, embedding_models.md, poem_tang_渭城曲_王维.md, roadmap_week5.md, embedding_models.md
   - 归因：rag_concepts.md：最高分 0.0000 不低于第 5 名 0.0000，正确片段被排挤出候选（TopK / 排序问题）
3. **君不见黄河之水天上来 出自李白的哪首诗？**
   - 期望来源：（空）
   - 前 5 来源：rag_concepts.md, embedding_models.md, poem_tang_渭城曲_王维.md, roadmap_week5.md, embedding_models.md
   - 归因：无答案样本仍返回了结果（应拒答却未拒答）
4. **2024 年巴黎奥运会开幕式的举办日期是？**
   - 期望来源：（空）
   - 前 5 来源：embedding_models.md, embedding_models.md, poem_tang_渭城曲_王维.md, roadmap_week5.md, embedding_models.md
   - 归因：无答案样本仍返回了结果（应拒答却未拒答）

### chunk_size=800（4 条未命中）

1. **文本向量化之后，检索问题变成了什么计算？**
   - 期望来源：rag_concepts.md
   - 前 5 来源：roadmap_week5.md, roadmap_week5.md, embedding_models.md, poem_tang_渭城曲_王维.md, embedding_models.md
   - 归因：rag_concepts.md：最高分 0.0000 不低于第 5 名 0.0000，正确片段被排挤出候选（TopK / 排序问题）
2. **怎么让知识库问答避免编造答案？**
   - 期望来源：rag_concepts.md
   - 前 5 来源：qdrant_basics.md, roadmap_week5.md, embedding_models.md, poem_tang_渭城曲_王维.md, roadmap_week5.md
   - 归因：rag_concepts.md：最高分 0.0000 不低于第 5 名 0.0000，正确片段被排挤出候选（TopK / 排序问题）
3. **君不见黄河之水天上来 出自李白的哪首诗？**
   - 期望来源：（空）
   - 前 5 来源：rag_concepts.md, roadmap_week5.md, embedding_models.md, poem_tang_渭城曲_王维.md, roadmap_week5.md
   - 归因：无答案样本仍返回了结果（应拒答却未拒答）
4. **2024 年巴黎奥运会开幕式的举办日期是？**
   - 期望来源：（空）
   - 前 5 来源：roadmap_week5.md, roadmap_week5.md, embedding_models.md, poem_tang_渭城曲_王维.md, embedding_models.md
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
