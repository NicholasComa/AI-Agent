"""JwipcKnowledgeRAG 问答入口：文档导入 + 检索增强生成（引用 + 拒答）。

检索增强组件（Metadata Filter / 混合检索 / Rerank）通过命令行参数可选启用：

- ``--strategy``：选择召回策略（vector / hybrid / rerank / hybrid+rerank），
  决定「怎么召回」；混合检索的 BM25 索引在导入文档时与向量索引一同建立，
  对已有集合首次启用 hybrid 时无需重导——首次 ``--ask`` 会自动从向量库
  scroll 出语料重建 BM25（仅在 hybrid 模式下生效）；
- ``--metadata``：本次查询的元数据过滤条件，决定「查哪部分」，每次查询可变，
  可与任意策略叠加（``key=value`` 精确匹配；``key=a,b`` 命中任一；``;`` 分隔多键）；
- ``--eval``：对检索集批量评测四种策略，输出与 ``docs/param_compare.md``
  同列的 Recall@K 表格；``--report`` 可把表格落盘为 Markdown；
- ``--debug``：打印召回明细（chunk_id / score / 长度）与 LLM 原始输出。

从项目根目录运行（脚本自带 ``src/`` 的 sys.path 引导，无需 PYTHONPATH）::

    # 仅导入知识库（默认纯向量）
    uv run python scripts/rag_week6_qa.py --ingest data/raw --collection jwipc_knowledge

    # 导入并提问（纯向量，与历史用法完全兼容）
    uv run python scripts/rag_week6_qa.py --ingest data/raw --ask "什么是Qdrant？"

    # 混合检索问答（首次会自动从向量库重建 BM25，无需带 --ingest）
    uv run python scripts/rag_week6_qa.py --collection jwipc_v2 --ask "第5周通过标准" --strategy hybrid

    # Metadata Filter：只从 PDF 召回（可与任意 --strategy 叠加）
    uv run python scripts/rag_week6_qa.py --ask "..." --metadata "file_type=pdf"

    # 四种策略批量评测（输出 Recall@K 表，与 param_compare.md 同口径）
    uv run python scripts/rag_week6_qa.py --ingest data/raw --eval

    # 拒答时打印 LLM 原始输出，便于排查提示词 / 模型判定问题
    uv run python scripts/rag_week6_qa.py --ask "..." --strategy hybrid --debug

真实链路由 ``.env`` 决定：Embedding 走 ``EMBEDDING_*``（Ollama mxbai-embed-large），
向量库走 ``QDRANT_*``（Docker Qdrant），生成走 ``API_BASE_URL`` / ``MODEL_NAME``
（Ollama 对话模型）。离线验证请用单元测试（FakeEmbedding + 假 LLM）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# 引导 src/ 到 sys.path，保证 `from rag.xxx import ...` 可用。
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from dotenv import load_dotenv  # noqa: E402

from config import load_config  # noqa: E402
from llm_client import LlmClient  # noqa: E402
from rag.embeddings import get_embedding  # noqa: E402
from rag.generator import RagGenerator  # noqa: E402
from rag.knowledge_rag import (  # noqa: E402
    RETRIEVAL_STRATEGIES,
    JwipcKnowledgeRAG,
    build_qdrant_config,
    build_retriever,
)
from rag.metadata_filter import MetadataConditions  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_metadata(text: str) -> MetadataConditions:
    """解析 ``key=value`` / ``key=a,b`` / ``k1=v1;k2=v2`` 形式的过滤条件。"""
    conditions: MetadataConditions = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            msg = f"invalid metadata condition: {part!r} (expected key=value)"
            raise ValueError(msg)
        key, _, raw = part.partition("=")
        key = key.strip()
        values = [v.strip() for v in raw.split(",") if v.strip()]
        if not key or not values:
            msg = f"invalid metadata condition: {part!r}"
            raise ValueError(msg)
        conditions[key] = values[0] if len(values) == 1 else values
    return conditions


def _build_rag(
    embedder,
    collection: str,
    *,
    strategy: str,
    chunk_size: int,
    overlap: int,
) -> JwipcKnowledgeRAG:
    """按策略构造知识库（retriever 由策略工厂产出）。"""
    cfg = build_qdrant_config(collection, embedder)
    retriever = build_retriever(embedder, cfg, strategy=strategy)
    return JwipcKnowledgeRAG(
        embedder, cfg, retriever=retriever, chunk_size=chunk_size, overlap=overlap
    )


def _ingest(rag: JwipcKnowledgeRAG, target: Path, *, rebuild: bool) -> None:
    """（可选）清空重建后导入文档。

    ``rebuild`` 走 ``rag.rebuild()``：复用检索器自身的 Qdrant 客户端，
    按「删→建」原子语义操作，规避「先构造 RAG 建集合→再删集合→首次 upsert 撞
    404」的历史时序问题；local 与 docker 模式下行为一致。
    """
    if rebuild:
        rag.rebuild()
    chunks = rag.add_directory(target) if target.is_dir() else rag.add_document(target)
    print(
        f"ingested {len(chunks)} chunks into collection={rag.config.collection_name}; "
        f"total={rag.count()}"
    )


def _print_recall(
    rag: JwipcKnowledgeRAG,
    question: str,
    *,
    top_k: int,
    metadata: MetadataConditions | None,
    label: str,
) -> list[tuple[str, float, int]]:
    """调试辅助：打印一次召回的 chunk_id / 归一分数 / 字符长度。"""
    results = rag.retrieve(question, top_k=top_k, metadata=metadata)
    print(f"\n[recall:{label}] top_k={top_k} returned={len(results)}")
    for i, r in enumerate(results):
        print(f"  [{i}] score={r.score:.4f} len={len(r.text)} {r.chunk_id}")
    return [(r.chunk_id, r.score, len(r.text)) for r in results]


async def _answer(
    rag: JwipcKnowledgeRAG,
    question: str,
    *,
    top_k: int,
    min_score: float,
    metadata: MetadataConditions | None,
    debug: bool = False,
    no_answer_retry: int = 2,
) -> None:
    """执行一次检索增强生成并打印答案与引用。"""
    app_cfg = load_config()
    llm = LlmClient(
        base_url=app_cfg.api_base_url,
        model=app_cfg.model_name,
        timeout_seconds=app_cfg.timeout_seconds,
    )
    try:

        async def chat(messages: list[dict[str, str]]) -> str:
            result = await llm.chat(
                messages,
                extra_body={"response_format": {"type": "json_object"}, "temperature": 0.0},
            )
            return result.text

        if debug:
            _print_recall(rag, question, top_k=top_k, metadata=metadata, label="primary")

        generator = RagGenerator(
            rag,
            chat,
            top_k=top_k,
            min_score=min_score,
            metadata=metadata,
            no_answer_retry=no_answer_retry,
        )
        answer = await generator.answer(question)
    finally:
        await llm.aclose()

    print(f"\nQ: {question}")
    print(f"A: {answer.answer}")
    if answer.has_answer:
        print(f"   confidence={answer.confidence:.2f} citations={len(answer.citations)}")
        for cite in answer.citations:
            print(f"   - [{cite.chunk_id}] {cite.source}")
            print(f"     {cite.quote}")
    else:
        print(f"   (rejected: {answer.rejected_reason})")
        if debug and answer.raw_llm_text:
            print(f"   raw_llm: {answer.raw_llm_text[:300]}")


def _run_eval(args, embedder) -> int:
    """对四种策略各建独立集合、导入语料并评测，输出与 param_compare.md 同列表格。"""
    from rag.evaluate import evaluate, load_dataset

    items = load_dataset(args.eval_set)
    data_dir = Path(args.ingest or "data/raw")
    if not data_dir.is_dir():
        print(f"[ERROR] 评测语料目录不存在: {data_dir}（用 --ingest 指定）")
        return 1

    rows: list[dict[str, object]] = []
    for strategy in RETRIEVAL_STRATEGIES:
        collection = f"{args.collection}_eval_{strategy.replace('+', '_')}"
        rag = _build_rag(
            embedder, collection, strategy=strategy, chunk_size=args.chunk_size, overlap=args.overlap
        )
        if args.rebuild:
            rag.rebuild()
        chunks = rag.add_directory(data_dir)

        retriever = rag.retriever  # 复用已喂入向量 + BM25 数据的检索器
        summary, _ = evaluate(retriever, items)

        start = time.perf_counter()
        for item in items:
            retriever.search(item.query, top_k=5)
        latency_ms = (time.perf_counter() - start) * 1000 / len(items)

        rows.append(
            {
                "strategy": strategy,
                "recall1": summary.recall[1],
                "recall3": summary.recall[3],
                "recall5": summary.recall[5],
                "false_recalled": f"{summary.false_recalled}/{summary.unanswerable}",
                "latency_ms": latency_ms,
                "chunks": len(chunks),
            }
        )
        print(
            f"eval done: strategy={strategy} chunks={len(chunks)} "
            f"recall@5={summary.recall[5]:.3f}"
        )

    header = (
        f"| {'策略':<14} | Recall@1 | Recall@3 | Recall@5 | 无答案误召回 | 平均延迟(ms) | 片段数 |"
    )
    sep = "|---|---|---|---|---|---|---|"
    print("\n" + header)
    print(sep)
    lines = [header, sep]
    for row in rows:
        line = (
            f"| {row['strategy']:<14} | {row['recall1']:.3f} | {row['recall3']:.3f} "
            f"| {row['recall5']:.3f} | {row['false_recalled']} | {row['latency_ms']:.1f} "
            f"| {row['chunks']} |"
        )
        print(line)
        lines.append(line)
    lines.append("")
    lines.append(
        f"- 评测集：{args.eval_set}（{len(items)} 条）；语料：{data_dir}；"
        f"Embedding：{type(embedder).__name__}"
    )
    lines.append("- 与 docs/param_compare.md 同口径：Recall@K 由前 5 名排序统计，"
                 "与请求 TopK 无关。")

    if args.report:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nreport written: {report}")
    return 0


def _run(args) -> int:
    embedder = get_embedding()
    if args.eval:
        return _run_eval(args, embedder)

    metadata = parse_metadata(args.metadata) if args.metadata else None
    rag = _build_rag(
        embedder,
        args.collection,
        strategy=args.strategy,
        chunk_size=args.chunk_size,
        overlap=args.overlap,
    )
    if args.ingest:
        _ingest(rag, Path(args.ingest), rebuild=args.rebuild)
    if args.ask:
        asyncio.run(
            _answer(
                rag,
                args.ask,
                top_k=args.top_k,
                min_score=args.min_score,
                metadata=metadata,
                debug=args.debug,
                no_answer_retry=args.no_answer_retry,
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="JwipcKnowledgeRAG 问答工具")
    parser.add_argument("--ingest", help="待导入的文件或目录路径（可选）")
    parser.add_argument("--collection", default="jwipc_knowledge", help="Qdrant 集合名")
    parser.add_argument(
        "--strategy",
        default="vector",
        choices=list(RETRIEVAL_STRATEGIES),
        help="召回策略：vector / hybrid / rerank / hybrid+rerank（默认 vector）",
    )
    parser.add_argument(
        "--metadata",
        help="元数据过滤条件，如 file_type=pdf 或 source=a.md,b.md（可 ; 分隔多键）",
    )
    parser.add_argument("--chunk-size", type=int, default=400, help="单片段目标字符数")
    parser.add_argument("--overlap", type=int, default=80, help="相邻片段重叠字符数")
    parser.add_argument("--rebuild", action="store_true", help="导入前删除并重建集合")
    parser.add_argument("--ask", help="要回答的问题")
    parser.add_argument("--top-k", type=int, default=3, help="每次召回片段数")
    parser.add_argument("--min-score", type=float, default=0.3, help="Top1 相似度拒答阈值")
    parser.add_argument(
        "--no-answer-retry",
        type=int,
        default=2,
        help="LLM 判无答案时用 top_k×N 重召再问一次的倍率；传 1 关闭重试",
    )
    parser.add_argument(
        "--eval",
        action="store_true",
        help="批量评测四种策略，输出与 param_compare.md 同列的 Recall@K 表",
    )
    parser.add_argument(
        "--eval-set",
        default="examples/retrieval_set.json",
        help="评测集路径（默认 examples/retrieval_set.json）",
    )
    parser.add_argument("--report", help="评测表落盘路径（可选，Markdown）")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="打印召回明细（chunk_id/score/长度）与 LLM 原始输出，便于排查拒答",
    )
    args = parser.parse_args(argv)
    if args.no_answer_retry < 1:
        parser.error("--no-answer-retry must be >= 1")
    if not args.eval and not args.ingest and not args.ask:
        parser.error("至少提供 --ingest / --ask / --eval 之一")

    load_dotenv(dotenv_path=REPO_ROOT / ".env")
    try:
        return _run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
