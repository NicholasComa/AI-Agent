"""检索增强组件使用演示：Metadata Filter / 混合检索 / Rerank。

脚本自带 ``src/`` 的 sys.path 引导，从项目根目录直接运行即可（无需 PYTHONPATH）。
演示四种检索配置，共用同一批样例文档 ``data/raw``：

1. 默认纯向量检索（与原生 QdrantRetriever 行为一致）；
2. Metadata Filter：按 source / file_type 过滤召回；
3. 混合检索：向量 + BM25 双路召回，RRF 融合排序；
4. Rerank：粗召回后用 Embedding 复排取 Top-K。

检索增强生成（RagGenerator）只依赖 ``rag.retrieve`` 的返回结构，因此四种检索
配置可原样接入同一生成链路（见文末 ``--qa`` 冒烟示例）。

运行方式：

    # 离线（FakeEmbedding + 内存 Qdrant），直接跑通四种用法
    uv run python scripts/rag_week6_usage.py

    # 真实链路（mxbai-embed-large @ Ollama + Docker Qdrant）
    export EMBEDDING_API_BASE_URL=http://localhost:11434/v1
    export EMBEDDING_MODEL_NAME=mxbai-embed-large
    export QDRANT_MODE=docker
    export QDRANT_HOST=localhost
    export QDRANT_PORT=6333
    uv run python scripts/rag_week6_usage.py

组件默认全部关闭，仅注入时才生效；不注入 retriever / reranker 时即纯向量检索，
可放心作为既有代码的向后兼容升级。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 引导 src/ 到 sys.path，保证 `from rag.xxx import ...` 可用。
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from rag.bm25 import BigramBM25  # noqa: E402
from rag.embeddings import get_embedding  # noqa: E402
from rag.filters import file_type_is, source_is  # noqa: E402
from rag.generator import RagAnswer, RagGenerator  # noqa: E402
from rag.hybrid import HybridRetriever  # noqa: E402
from rag.knowledge_rag import JwipcKnowledgeRAG, build_qdrant_config  # noqa: E402
from rag.rerank import EmbeddingReranker  # noqa: E402
from rag.retriever import QdrantRetriever, RetrievalResult  # noqa: E402

# 与样例文档（古诗词 / RAG 概念 / Qdrant 基础）相关的几条查询。
SAMPLE_QUERIES = [
    "RAG 检索常用的离线评估指标有哪些？",
    "Qdrant 里怎么用 payload 做元数据过滤？",
    "mxbai-embed-large 向量的维度是多少？",
    "李清照的词有哪些特点？",
]


def build_search_rag(collection: str, embedder):
    """用法 1：默认纯向量检索（不注入任何增强组件）。"""
    cfg = build_qdrant_config(collection, embedder)
    return JwipcKnowledgeRAG(embedder, cfg)


def build_filter_rag(collection: str, embedder):
    """用法 2：Metadata Filter —— 检索器本身不变，仅在 retrieve 时传过滤条件。"""
    cfg = build_qdrant_config(collection, embedder)
    return JwipcKnowledgeRAG(embedder, cfg)


def build_hybrid_rag(collection: str, embedder):
    """用法 3：混合检索 —— 把向量检索器与 BM25 包装成 HybridRetriever 注入。"""
    cfg = build_qdrant_config(collection, embedder)
    base = QdrantRetriever(embedder, cfg)  # 向量路，HybridRetriever 会同步喂给 BM25 路
    hybrid = HybridRetriever(base, BigramBM25())
    return JwipcKnowledgeRAG(embedder, cfg, retriever=hybrid)


def build_rerank_rag(collection: str, embedder, coarse_top_k: int = 20):
    """用法 4：Rerank —— 注入 EmbeddingReranker，粗召回 coarse_top_k 后复排取 Top-K。"""
    cfg = build_qdrant_config(collection, embedder)
    return JwipcKnowledgeRAG(
        embedder,
        cfg,
        reranker=EmbeddingReranker(embedder),
        coarse_top_k=coarse_top_k,
    )


def print_results(label: str, query: str, results: list[RetrievalResult]) -> None:
    """统一打印一组检索结果。"""
    print(f"\n=== [{label}] query: {query} ===")
    if not results:
        print("  (无召回结果)")
        return
    for rank, r in enumerate(results, start=1):
        snippet = r.text.replace("\n", " ")[:60]
        print(f"  #{rank} score={r.score:.4f} source={r.source} chunk={r.chunk_id}")
        print(f"       {snippet}")


def run_mode(
    mode: str,
    factory,
    embedder,
    data_dir: Path,
    queries: list[str],
    top_k: int,
) -> None:
    """构造某模式的 RAG、导入样例文档、对每条查询召回并打印。"""
    collection = f"demo_{mode}"
    rag = factory(collection, embedder)
    rag.add_directory(data_dir)  # 把样例文档切分写入该模式专属集合
    print(f"\n########## 模式: {mode} （已导入 {rag.count()} 个片段） ##########")

    for q in queries:
        if mode == "filter":
            # Metadata Filter 演示：按来源文件名过滤（样例文档均为 .md → file_type=text）
            results = rag.retrieve(
                q,
                top_k=top_k,
                metadata=source_is("rag_concepts.md", "rag_pipeline.md"),
            )
            print_results("filter: source∈{rag_concepts,rag_pipeline}", q, results)
            # file_type 维度过滤（本数据集全为 text，故等价于不过滤；接入 PDF 后会真正收窄）
            results = rag.retrieve(q, top_k=top_k, metadata=file_type_is("text"))
            print_results("filter: file_type=text", q, results)
        else:
            results = rag.retrieve(q, top_k=top_k)
            print_results(mode, q, results)


def make_stub_chat(rag: JwipcKnowledgeRAG, query: str):
    """构造一个离线占位 ChatFn：引用本次召回 Top1，返回合法 RagAnswer JSON。

    真实使用时把这里换成 ``src.llm_client.LlmClient`` 的异步适配即可，
    检索/生成解耦，四种检索配置无需改动生成侧。
    """
    probe = rag.retrieve(query, top_k=1)
    top = probe[0] if probe else None
    top_id = top.chunk_id if top else ""
    top_src = top.source if top else ""

    async def chat(messages: list[dict[str, str]]) -> str:
        return json.dumps(
            {
                "answer": f"示例：根据《{top_src}》中的内容可回答该问题。",
                "has_answer": True,
                "citations": [
                    {"source": top_src, "chunk_id": top_id, "quote": "（示例引用原文）"}
                ],
                "confidence": 0.9,
            },
            ensure_ascii=False,
        )

    return chat


async def demo_qa(rag: JwipcKnowledgeRAG, query: str) -> RagAnswer:
    """端到端冒烟：检索增强生成链路复用 rag.retrieve 的返回结构。"""
    generator = RagGenerator(rag, make_stub_chat(rag, query), top_k=3, min_score=0.0)
    return await generator.answer(query)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索增强组件使用演示")
    parser.add_argument(
        "--data-dir",
        default="data/raw",
        help="样例文档目录（默认 data/raw，含 .md 文档）",
    )
    parser.add_argument("--top-k", type=int, default=3, help="每次召回的片段数")
    parser.add_argument("--query", help="仅对该查询运行全部模式（默认跑 SAMPLE_QUERIES）")
    parser.add_argument(
        "--no-qa",
        action="store_true",
        help="跳过文末检索增强生成（RagGenerator）冒烟示例",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"[ERROR] 样例文档目录不存在: {data_dir}")
        return 1

    embedder = get_embedding()
    print(f"embedder: {type(embedder).__name__}")
    queries = [args.query] if args.query else SAMPLE_QUERIES

    # 四个用法逐一演示（各自独立集合，互不影响）。
    run_mode("baseline", build_search_rag, embedder, data_dir, queries, args.top_k)
    run_mode("filter", build_filter_rag, embedder, data_dir, queries, args.top_k)
    run_mode("hybrid", build_hybrid_rag, embedder, data_dir, queries, args.top_k)
    run_mode("rerank", build_rerank_rag, embedder, data_dir, queries, args.top_k)

    if not args.no_qa:
        print("\n########## 端到端冒烟：混合检索 + 检索增强生成 ##########")
        rag = build_hybrid_rag("demo_qa", embedder)
        rag.add_directory(data_dir)
        for q in queries[:2]:
            answer = asyncio.run(demo_qa(rag, q))
            print(f"\nquery: {q}")
            print(f"  has_answer={answer.has_answer} confidence={answer.confidence:.2f}")
            print(f"  answer={answer.answer}")
            for c in answer.citations:
                print(f"  citation: {c.source} / {c.chunk_id}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
