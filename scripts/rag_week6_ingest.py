"""JwipcKnowledgeRAG 文档导入入口：多格式（Markdown / TXT / PDF）导入 Qdrant。

从项目根目录运行（脚本自带 ``src/`` 的 sys.path 引导，无需 PYTHONPATH）::

    uv run python scripts/rag_week6_ingest.py --ingest data/raw --collection jwipc_knowledge

默认离线：``FakeEmbedding`` + 内存 Qdrant，无需外部服务即可验证导入链路。
接入真实 Embedding / Docker Qdrant 时通过 ``.env`` 配置 ``EMBEDDING_*`` /
``QDRANT_*`` 即可，无需改动本脚本。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 引导 src/ 到 sys.path，保证 `from rag.xxx import ...` 可用。
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from rag.knowledge_rag import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
