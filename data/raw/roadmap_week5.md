# 第 5 周 Roadmap 任务说明

本文档来自《南宁软件组 AI Agent 应用开发 12 周学习 Roadmap》第 5 周：RAG 基础（文档、切分、Embedding 与 Qdrant）。

## 本周目标

不依赖一键知识库，理解 RAG 每个环节并做出可检查的检索结果。也就是说，要亲自实现文档解析、切分、向量化、检索的每一个步骤，而不是直接使用某个平台封装好的一键知识库功能。

## 必学知识

本周需要掌握的知识点包括：文档解析、Chunk、Overlap、Metadata、Embedding、向量距离、TopK；检索与生成的职责分离；召回错误、上下文污染和幻觉来源；Qdrant 的 Collection、Point、Vector、Payload、Filter 以及 Local Mode 与 Docker 两种运行方式。

## 阶段任务

第一项任务：使用公开训练资料建立小型知识库，至少包含 30 个文档片段。片段来自对公开资料的切分，每条片段都要有唯一的标识和来源信息。

第二项任务：先用 Python 列表实现最小相似度检索，再迁移到 Qdrant。先用纯列表和余弦相似度把检索跑通，理解检索的本质，之后再引入向量数据库做工程化替换。

第三项任务：检索接口必须返回 chunk_id、source、score、text 四个字段，且不先调用 LLM。检索阶段只负责召回，不负责生成，这是职责分离的具体体现。

第四项任务：准备 20 个检索问题，人工标注每个问题的期望来源，统计 Recall@K 指标，用数据衡量检索质量。

## 交付物

本周需要交付的内容包括：ingestion.py（文档导入与切分）、retriever.py（检索器）、Qdrant 配置、20 条检索集、检索评测报告。其中检索评测报告要给出 Recall@K 的统计结果和错误分析。

## 通过标准

本周的通过标准是：能够根据错误结果判断问题出在解析、切分、Embedding、过滤还是 TopK 环节。也就是说，当检索结果不对时，要能定位到具体是哪一个环节出了问题，而不是笼统地说「检索效果不好」。

## 官方参考

本周的官方参考资料包括 Qdrant 的 Local Quickstart、Qdrant 官方文档，以及 LlamaIndex 的 Loading 文档。优先阅读官方 Quickstart，跑通最小示例后再深入 Concepts 与 Guides。
