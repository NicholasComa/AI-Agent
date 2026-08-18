"""真实 Embedding + Qdrant 检索演示。

从项目根目录运行（脚本自带 ``src/`` 的 sys.path 引导，无需 PYTHONPATH）::

    uv run python scripts/rag_week5_real_demo.py

流程：
1. 载入 ``.env``，根据 ``EMBEDDING_*`` 决定使用真实 Embedding 还是离线 FakeEmbedding。
2. 用探针文本确定向量维度，并与 Qdrant 集合的 ``vector_size`` 对齐。
3. 把 ``data/raw`` 下的 ``.md`` / ``.txt`` 切分为片段，按 ``.env`` 的 ``QDRANT_*`` 建立 Qdrant 索引。
4. 对若干样例查询做检索，打印 ``chunk_id/source/score/text``。

不调用任何 LLM；Qdrant 运行模式由 ``.env`` 的 ``QDRANT_MODE`` 决定
（默认 local ``:memory:``，无需外部服务）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# 引导 src/ 到 sys.path，保证 `from rag.xxx import ...` 可用。
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from dataclasses import replace  # noqa: E402

from rag.embeddings import EmbeddingClient, EmbeddingTransientError, get_embedding  # noqa: E402
from rag.ingestion import build_chunks  # noqa: E402
from rag.qdrant_store import get_qdrant_config  # noqa: E402
from rag.retriever import QdrantRetriever  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"

SAMPLE_QUERIES = [
    "什么是向量距离和 TopK？",
    "Qdrant 的 Payload 和 Filter 是什么？",
    "RAG 中为什么要让检索与生成职责分离？",
    "红豆生南国",  # 诗词查询：验证 data/raw 中的简体诗词 md 已入知识库
]


def _main() -> int:
    paths = sorted(RAW_DIR.glob("*.md")) + sorted(RAW_DIR.glob("*.txt"))
    if not paths:
        print(f"[error] 未在 {RAW_DIR} 找到任何 .md/.txt 源文档")
        return 1

    print("=== 1. 选择 Embedder（依据 .env 的 EMBEDDING_*） ===")
    emb = get_embedding()
    print(f"embedder = {type(emb).__name__}")

    # 探针：先向量化一个样本，确定真实维度，避免 vector_size 不匹配。
    probe = emb.embed(["probe-text"])[0]
    dim = len(probe)
    print(f"vector dim = {dim}")

    if isinstance(emb, EmbeddingClient):
        print("使用真实 Embedding 接口（mxbai-embed-large @ localhost:11434）")
    else:
        print(
            "[warn] 未检测到 EMBEDDING_API_BASE_URL / EMBEDDING_MODEL_NAME，回退到离线 FakeEmbedding"
        )

    print("\n=== 2. 建立 Qdrant 索引（配置来自 .env 的 QDRANT_*） ===")
    cfg = get_qdrant_config()
    if cfg.vector_size != dim:
        print(
            f"[warn] .env 的 QDRANT_VECTOR_SIZE={cfg.vector_size} 与实际 Embedding "
            f"维度 {dim} 不一致，本次以实际维度 {dim} 为准（建议同步 .env）"
        )
        cfg = replace(cfg, vector_size=dim)
    print(
        f"mode={cfg.mode} path={cfg.path} collection={cfg.collection_name} "
        f"vector_size={cfg.vector_size}"
    )
    try:
        retriever = QdrantRetriever(emb, cfg)
    except EmbeddingTransientError as exc:
        print(f"[error] Embedding 调用失败（真实接口连接不上？确认 Ollama 已启动）：{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 无法初始化 Qdrant 检索器：{exc}")
        return 1

    chunks = build_chunks([str(p) for p in paths], chunk_size=300, overlap=60)
    print(f"源文件 {len(paths)} 个，切分片段 {len(chunks)} 个")
    if len(chunks) < 30:
        print(f"[warn] 片段数 {len(chunks)} < 30，建议补充训练资料或调小 chunk_size")
    try:
        n = retriever.index(chunks)
    except EmbeddingTransientError as exc:
        print(f"[error] Embedding 调用失败（真实接口连接不上？确认 Ollama 已启动）：{exc}")
        return 1
    print(f"已写入 Qdrant {n} 个点")

    print("\n=== 3. 检索（返回 chunk_id/source/score/text，不调用 LLM） ===")
    for query in SAMPLE_QUERIES:
        print(f"\nQuery: {query}")
        try:
            results = retriever.search(query, top_k=3)
        except EmbeddingTransientError as exc:
            print(f"  [error] Embedding 调用失败（真实接口连接不上？确认 Ollama 已启动）：{exc}")
            return 1
        if not results:
            print("  (无结果)")
            continue
        for r in results:
            print(f"  score={r.score:.4f}  chunk_id={r.chunk_id}  source={r.source}")
            print(f"    text={r.text[:60]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
