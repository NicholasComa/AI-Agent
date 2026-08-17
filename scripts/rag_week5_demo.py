"""RAG 列表检索演示：导入 → 切分 → 向量化 → 列表检索。

从项目根目录运行（脚本自带 ``src/`` 的 sys.path 引导，无需 PYTHONPATH）::

    uv run python scripts/rag_week5_demo.py

演示内容：
1. 读取 ``data/raw`` 下的 ``.md`` / ``.txt`` 并切分为片段，打印片段总数（应 >= 30）。
2. 用 :class:`FakeEmbedding` 建立内存索引（离线，无需真实 embedding 接口）。
3. 对若干样例查询做检索，打印 ``chunk_id / source / score / text``（含一条诗词查询）。

不调用任何 LLM，不依赖外部服务。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 引导 src/ 到 sys.path，保证 `from rag.xxx import ...` 可用。
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from rag.embeddings import FakeEmbedding
from rag.ingestion import build_chunks
from rag.retriever import ListRetriever

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"

SAMPLE_QUERIES = [
    "什么是向量距离和 TopK？",
    "Qdrant 的 Payload 和 Filter 是什么？",
    "RAG 中为什么要让检索与生成职责分离？",
    "红豆生南国",  # 诗词查询：验证 data/raw 中的简体诗词 md 已入知识库
]


def main() -> int:
    paths = sorted(RAW_DIR.glob("*.md")) + sorted(RAW_DIR.glob("*.txt"))
    if not paths:
        print(f"[error] 未在 {RAW_DIR} 找到任何 .md/.txt 源文档")
        return 1

    print("=== 1. 文档导入与切分 ===")
    chunks = build_chunks([str(p) for p in paths], chunk_size=300, overlap=60)
    print(f"源文件 {len(paths)} 个，切分片段 {len(chunks)} 个")
    for chunk in chunks[:5]:
        print(f"  {chunk.chunk_id}  (len={len(chunk.text)})  {chunk.text[:40]!r}")
    if len(chunks) >= 30:
        print(f"[OK] 片段数 {len(chunks)} >= 30，满足「至少 30 个文档片段」")
    else:
        print(f"[FAIL] 片段数 {len(chunks)} < 30，需补充训练资料或调小 chunk_size")
        return 1

    print("\n=== 2. 建立索引（FakeEmbedding，离线） ===")
    retriever = ListRetriever(FakeEmbedding(dim=128))
    retriever.index(chunks)
    print(f"已索引 {len(retriever)} 个片段")

    print("\n=== 3. 列表检索（返回 chunk_id/source/score/text，不调用 LLM） ===")
    for query in SAMPLE_QUERIES:
        print(f"\nQuery: {query}")
        for r in retriever.search(query, top_k=3):
            print(f"  score={r.score:.4f}  chunk_id={r.chunk_id}  source={r.source}")
            print(f"    text={r.text[:60]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
