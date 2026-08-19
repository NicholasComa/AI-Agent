# 检索评测报告（Recall@K）

- 生成时间：2026-08-18 15:55:59
- Embedding：EmbeddingClient（模型 mxbai-embed-large，维度 1024）
- Qdrant：mode=docker，collection=rag_chunks，索引片段 41 个

## 一、评测集构成

共 **20** 条：有期望来源 18 条（参与 Recall 计算）、无期望来源 2 条（观察误召回）。
- 事实型-任务：1 条
- 事实型-术语：1 条
- 事实型-概念：5 条
- 事实型-流程：1 条
- 事实型-评测：1 条
- 同义改写：1 条
- 同义改写-幻觉：1 条
- 知识库无对应片段-无法回答：1 条
- 知识库无此诗-无法回答：1 条
- 诗词-唐诗：2 条
- 诗词-宋词：2 条
- 跨文档：2 条
- 跨文档-错误归因：1 条

## 二、Recall@K 统计

| K | 命中数 | Recall |
|---|--------|--------|
| @1 | 14 / 18 | 0.778 |
| @3 | 15 / 18 | 0.833 |
| @5 | 16 / 18 | 0.889 |

无答案样本误召回：2 / 2（前 5 条仍返回了结果，理想为 0）

## 三、错误样本与归因

### 未命中（2 条）

1. **第 5 周的通过标准是什么？**
   - 期望来源：roadmap_week5.md
   - 前 5 来源：rag_pipeline.md, rag_pipeline.md, rag_evaluation.md, rag_evaluation.md, rag_concepts.md
   - 归因：roadmap_week5.md：最高分 0.6347 低于第 5 名 0.6904（Embedding 表达不足）
2. **怎么让知识库问答避免编造答案？**
   - 期望来源：rag_concepts.md
   - 前 5 来源：rag_pipeline.md, poem_ci_鹧鸪天_晏几道.md, rag_pipeline.md, rag_evaluation.md, embedding_models.md
   - 归因：rag_concepts.md：最高分 0.5984 低于第 5 名 0.6037（Embedding 表达不足）

### 无答案误召回（2 条）

1. **君不见黄河之水天上来 出自李白的哪首诗？**
   - 前 5 来源：poem_ci_卜算子_李之仪.md, poem_tang_怨情_李白.md, poem_ci_卜算子_王观.md, rag_pipeline.md, poem_ci_临江仙_晏几道.md
   - 说明：无答案样本仍返回了结果（应拒答却未拒答）
2. **2024 年巴黎奥运会开幕式的举办日期是？**
   - 前 5 来源：poem_ci_鹧鸪天_晏几道.md, poem_tang_横吹曲辞 出塞 一_王昌龄.md, rag_pipeline.md, rag_pipeline.md, rag_evaluation.md
   - 说明：无答案样本仍返回了结果（应拒答却未拒答）

## 四、结论与下一步

Recall@3 较高，检索质量整体可用；可继续优化 Recall@1。
- 改进方向：按归因频次优先处理 —— 覆盖缺失补资料、切分参数调整、Embedding 换模型、TopK / 过滤阈值调整。
