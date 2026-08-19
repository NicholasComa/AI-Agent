"""Recall@K 检索评测入口。

从项目根目录运行（脚本自带 ``src/`` 的 sys.path 引导，无需 PYTHONPATH）::

    uv run python scripts/rag_week5_eval.py            # 真实 Embedding（需 Ollama 在线）
    uv run python scripts/rag_week5_eval.py --offline  # 强制离线 FakeEmbedding

流程：
1. 载入 ``.env``，按 ``EMBEDDING_*`` / ``QDRANT_*`` 配置选择 Embedder 与 Qdrant 检索器。
2. 把 ``data/raw`` 下的文档切分、向量化后写入 Qdrant 索引。
3. 加载 ``datasets/retrieval_set.json``（20 条人工标注检索集）。
4. 逐条检索统计 Recall@1/3/5，未命中样本做环节归因。
5. 生成 ``docs/week05_retrieval_eval.md`` 评测报告并打印摘要。

不调用任何 LLM；检索与评测均由本地模块完成。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# 引导 src/ 到 sys.path，保证 `from rag.xxx import ...` 可用。
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from rag.embeddings import EmbeddingClient, get_embedding  # noqa: E402
from rag.evaluate import (  # noqa: E402
    DEFAULT_REPORT_PATH,
    evaluate,
    load_dataset,
    write_report,
)
from rag.ingestion import build_chunks  # noqa: E402
from rag.qdrant_store import get_qdrant_config  # noqa: E402
from rag.retriever import QdrantRetriever  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "raw"
DEFAULT_DATASET = REPO_ROOT / "datasets" / "retrieval_set.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAG 检索 Recall@K 评测")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="知识库源文档目录")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="检索集 JSON 路径")
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH), help="报告输出路径")
    parser.add_argument("--chunk-size", type=int, default=300, help="切分窗口大小")
    parser.add_argument("--overlap", type=int, default=60, help="切分重叠长度")
    parser.add_argument("--top-ks", default="1,3,5", help="逗号分隔的 K 取值")
    parser.add_argument(
        "--offline", action="store_true", help="强制离线 FakeEmbedding（不连真实接口）"
    )
    return parser.parse_args()


def _main() -> int:
    args = _parse_args()
    if args.offline:
        os.environ["EMBEDDING_API_BASE_URL"] = ""
        os.environ["EMBEDDING_MODEL_NAME"] = ""
    top_ks = tuple(sorted({int(k) for k in args.top_ks.split(",") if k.strip()}))
    if not top_ks:
        print("[error] --top-ks 至少需要一个正整数 K")
        return 1

    raw_dir = Path(args.raw_dir)
    paths = sorted(raw_dir.glob("*.md")) + sorted(raw_dir.glob("*.txt"))
    if not paths:
        print(f"[error] 未在 {raw_dir} 找到任何 .md/.txt 源文档")
        return 1

    print("=== 1. 选择 Embedder 与 Qdrant 配置（依据 .env） ===")
    emb = get_embedding()
    probe = emb.embed(["probe-text"])[0]
    dim = len(probe)
    print(f"embedder = {type(emb).__name__}（dim={dim}）")
    if args.offline or not isinstance(emb, EmbeddingClient):
        print("[warn] 离线模式：使用 FakeEmbedding，Recall 仅供流程验证，不代表真实语义检索水平")

    cfg = get_qdrant_config()
    if cfg.vector_size != dim:
        print(
            f"[warn] .env 的 QDRANT_VECTOR_SIZE={cfg.vector_size} 与实际维度 {dim} 不一致，"
            f"以实际维度 {dim} 为准（建议同步 .env）"
        )
        from dataclasses import replace

        cfg = replace(cfg, vector_size=dim)
    print(f"qdrant mode={cfg.mode} collection={cfg.collection_name} vector_size={cfg.vector_size}")

    print("\n=== 2. 建立索引 ===")
    retriever = QdrantRetriever(emb, cfg)
    chunks = build_chunks([str(p) for p in paths], chunk_size=args.chunk_size, overlap=args.overlap)
    print(f"源文件 {len(paths)} 个，切分片段 {len(chunks)} 个")
    n = retriever.index(chunks)
    print(f"已写入 Qdrant {n} 个点")

    print("\n=== 3. 评测 Recall@K ===")
    items = load_dataset(args.dataset)
    print(f"评测集 {len(items)} 条")
    summary, misses = evaluate(retriever, items, top_ks=top_ks)
    for k in sorted(summary.recall):
        print(f"  Recall@{k} = {summary.recall[k]:.3f}（{summary.hits[k]}/{summary.answerable}）")
    if summary.unanswerable:
        print(
            f"  无答案样本误召回 {summary.false_recalled}/{summary.unanswerable}"
            f"（前 {max(summary.recall)} 条仍返回结果）"
        )

    print("\n=== 4. 生成报告 ===")
    meta = {
        "embedder": type(emb).__name__,
        "model": getattr(emb, "model", "FakeEmbedding(离线)"),
        "dim": dim,
        "mode": cfg.mode,
        "collection": cfg.collection_name,
        "chunk_count": len(chunks),
        "offline": bool(args.offline),
    }
    report = write_report(
        args.report,
        items=items,
        summary=summary,
        misses=misses,
        meta=meta,
    )
    print(f"报告已写入：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
