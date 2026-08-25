"""JwipcKnowledgeRAG 问答入口：文档导入 + 检索增强生成（引用 + 拒答）。

从项目根目录运行（脚本自带 ``src/`` 的 sys.path 引导，无需 PYTHONPATH）::

    # 仅导入知识库
    uv run python scripts/rag_week6_qa.py --ingest data/raw --collection jwipc_knowledge

    # 导入并提问
    uv run python scripts/rag_week6_qa.py --ingest data/raw --ask "什么是Qdrant？"

    # 仅对已有集合提问（可调 top_k / 拒答阈值）
    uv run python scripts/rag_week6_qa.py --ask "什么是Qdrant？" --top-k 5 --min-score 0.3

真实链路由 ``.env`` 决定：Embedding 走 ``EMBEDDING_*``（Ollama mxbai-embed-large），
向量库走 ``QDRANT_*``（Docker Qdrant），生成走 ``API_BASE_URL`` / ``MODEL_NAME``
（Ollama 对话模型）。离线验证请用单元测试（FakeEmbedding + 假 LLM）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 引导 src/ 到 sys.path，保证 `from rag.xxx import ...` 可用。
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from dotenv import load_dotenv

from config import load_config
from llm_client import LlmClient
from rag.embeddings import get_embedding
from rag.generator import RagGenerator
from rag.knowledge_rag import JwipcKnowledgeRAG, build_qdrant_config

REPO_ROOT = Path(__file__).resolve().parent.parent


async def _answer(rag: JwipcKnowledgeRAG, question: str, *, top_k: int, min_score: float) -> None:
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

        answer = await RagGenerator(rag, chat, top_k=top_k, min_score=min_score).answer(question)
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


async def _run(args) -> int:
    embedder = get_embedding()
    cfg = build_qdrant_config(args.collection, embedder)
    if args.rebuild:
        from rag.qdrant_store import connect

        client = connect(cfg)
        if client.collection_exists(cfg.collection_name):
            client.delete_collection(cfg.collection_name)

    rag = JwipcKnowledgeRAG(embedder, cfg, chunk_size=args.chunk_size, overlap=args.overlap)
    if args.ingest:
        target = Path(args.ingest)
        chunks = rag.add_directory(target) if target.is_dir() else rag.add_document(target)
        print(
            f"ingested {len(chunks)} chunks into collection={cfg.collection_name}; "
            f"total={rag.count()}"
        )
    if args.ask:
        await _answer(rag, args.ask, top_k=args.top_k, min_score=args.min_score)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="JwipcKnowledgeRAG 问答工具")
    parser.add_argument("--ingest", help="待导入的文件或目录路径（可选）")
    parser.add_argument("--collection", default="jwipc_knowledge", help="Qdrant 集合名")
    parser.add_argument("--chunk-size", type=int, default=400, help="单片段目标字符数")
    parser.add_argument("--overlap", type=int, default=80, help="相邻片段重叠字符数")
    parser.add_argument("--rebuild", action="store_true", help="导入前删除并重建集合")
    parser.add_argument("--ask", help="要回答的问题")
    parser.add_argument("--top-k", type=int, default=3, help="每次召回片段数")
    parser.add_argument("--min-score", type=float, default=0.3, help="Top1 相似度拒答阈值")
    args = parser.parse_args(argv)
    if not args.ingest and not args.ask:
        parser.error("至少提供 --ingest 或 --ask 之一")

    load_dotenv(dotenv_path=REPO_ROOT / ".env")
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
