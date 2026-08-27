"""2x2 参数网格评测：chunk_size x TopK 对召回效果与延迟的影响。

从项目根目录运行（脚本自带 ``src/`` 的 sys.path 引导，无需 PYTHONPATH）::

    uv run python scripts/rag_week6_param_compare.py            # 真实链路（Ollama + Qdrant）
    uv run python scripts/rag_week6_param_compare.py --offline  # 离线 FakeEmbedding 冒烟

流程：
1. 载入 ``.env`` 选择 Embedder 与 Qdrant 配置。
2. 对每个 ``chunk_size`` 建立独立集合（前缀 ``--collection-prefix``），导入语料并计时。
3. 对每个 ``top_k`` 逐条检索计时（含 query 向量化与 Qdrant 搜索），统计平均延迟。
4. 复用 :func:`rag.evaluate.evaluate` 统计 Recall@1/3/5 与无答案误召回。
5. 汇总 2x2 对比表与候选方案，生成 Markdown 报告。
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

# 引导 src/ 到 sys.path，保证 `from rag.xxx import ...` 可用。
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from rag.embeddings import EmbeddingClient, get_embedding  # noqa: E402
from rag.evaluate import evaluate, load_dataset  # noqa: E402
from rag.knowledge_rag import JwipcKnowledgeRAG, build_qdrant_config  # noqa: E402
from rag.qdrant_store import connect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "raw"
DEFAULT_DATASET = REPO_ROOT / "examples" / "retrieval_set.json"
DEFAULT_REPORT = REPO_ROOT / "docs" / "param_compare.md"
EVAL_TOP_KS = (1, 3, 5)  # 每个组合统一统计这三档 Recall


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="chunk_size x TopK 参数对比评测")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="知识库源文档目录")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="检索集 JSON 路径")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报告输出路径")
    parser.add_argument("--chunk-sizes", default="400,800", help="逗号分隔的 chunk_size 取值")
    parser.add_argument("--top-ks", default="3,5", help="逗号分隔的 TopK 取值")
    parser.add_argument("--overlap", type=int, default=80, help="切分重叠长度")
    parser.add_argument("--collection-prefix", default="jwipc_cmp", help="集合名前缀")
    parser.add_argument(
        "--offline", action="store_true", help="强制离线 FakeEmbedding（不连真实接口）"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="打印 httpx / rag 模块 INFO 日志（默认仅 WARNING）"
    )
    return parser.parse_args()


def _parse_ints(raw: str, name: str) -> list[int]:
    vals = sorted({int(k) for k in raw.split(",") if k.strip()})
    if not vals:
        msg = f"--{name} 至少需要一个正整数"
        raise SystemExit(msg)
    return vals


def _timed_search_avg(retriever: Any, queries: list[str], top_k: int) -> float:
    """对全部查询逐条检索计时，返回平均延迟（毫秒）。"""
    t0 = time.perf_counter()
    for q in queries:
        retriever.search(q, top_k=top_k)
    elapsed = time.perf_counter() - t0
    return elapsed / len(queries) * 1000.0 if queries else 0.0


def _rebuild_collection(cfg: Any) -> None:
    """删除同名前缀下可能存在的旧集合，保证每次评测从空集合开始。"""
    client = connect(cfg)
    if client.collection_exists(cfg.collection_name):
        client.delete_collection(cfg.collection_name)
        logging.getLogger("param_compare").info("deleted old collection %s", cfg.collection_name)
    client.close()


def _write_report(
    path: str | Path,
    *,
    meta: dict[str, Any],
    rows: list[dict[str, Any]],
    misses_by_cs: dict[int, list[dict[str, Any]]],
) -> Path:
    """生成 2x2 参数对比 Markdown 报告。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# 参数对比报告：chunk_size x TopK\n")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- Embedding：{meta['embedder']}（模型 {meta['model']}，维度 {meta['dim']}）")
    lines.append(
        f"- Qdrant：mode={meta['mode']}，语料 {meta['source_count']} 个源文件，"
        f"检索集 {meta['dataset_size']} 条（有答案 {meta['answerable']} / 无答案 {meta['unanswerable']}）"
    )
    if meta.get("offline"):
        lines.append(
            "- **口径说明**：离线 FakeEmbedding 运行，Recall 仅供流程验证，不代表真实语义水平"
        )
    lines.append("")

    lines.append("## 一、2x2 对比总表\n")
    lines.append(
        "| chunk_size | TopK | Recall@1 | Recall@3 | Recall@5 | 无答案误召回 | 平均检索延迟 (ms) | 导入耗时 (s) | 片段数 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['chunk_size']} | {r['top_k']} | {r['recall1']:.3f} | {r['recall3']:.3f} "
            f"| {r['recall5']:.3f} | {r['false_recalled']}/{r['unanswerable']} "
            f"| {r['avg_ms']:.1f} | {r['ingest_s']:.1f} | {r['chunk_count']} |"
        )
    lines.append("")

    lines.append("## 二、结论\n")
    best = max(rows, key=lambda r: (r["recall5"], -r["avg_ms"]))
    lines.append(
        f"最优组合：**chunk_size={best['chunk_size']} + TopK={best['top_k']}**"
        f"（Recall@5={best['recall5']:.3f}，平均延迟 {best['avg_ms']:.1f} ms）。"
    )
    lines.append(
        "- chunk_size 影响片段粒度与数量：小窗口片段更细、更易定位，但片段数多、导入耗时更长；"
        "大窗口上下文更完整，但可能把不相关内容卷入同一片段。"
    )
    lines.append(
        "- TopK 影响召回条数与生成上下文规模：TopK 越大召回率越高，但延迟与上下文噪音随之上升；"
        "配合生成阶段的无答案阈值（min_score）可抵消部分噪音。"
    )
    lines.append("")

    lines.append("## 三、未命中样本（按 chunk_size 分组）\n")
    for cs in sorted(misses_by_cs):
        items = misses_by_cs[cs]
        lines.append(f"### chunk_size={cs}（{len(items)} 条未命中）\n")
        if not items:
            lines.append("无未命中样本。\n")
            continue
        for i, m in enumerate(items, 1):
            lines.append(f"{i}. **{m['query']}**")
            lines.append(f"   - 期望来源：{', '.join(m['expected_sources']) or '（空）'}")
            lines.append(
                f"   - 前 {max(EVAL_TOP_KS)} 来源：{', '.join(m['top_sources']) or '（无结果）'}"
            )
            lines.append(f"   - 归因：{m['diagnosis']}")
        lines.append("")

    lines.append("## 四、候选优化方案\n")
    lines.append("### 4.1 Metadata Filter（元数据过滤）\n")
    lines.append(
        "- 概念：在向量检索的同时按 Payload 元数据（source / 日期 / 类别 / 章节等）加过滤条件，"
        "缩小候选范围。"
    )
    lines.append(
        "- 适用场景：知识库按来源或主题分域（如不同部门资料），查询可先限定域再检索；"
        "本项目 ``QdrantRetriever.search`` 已支持 ``source_filter`` 精确过滤，可在此基础上扩展。"
    )
    lines.append("### 4.2 混合检索（Hybrid Search）\n")
    lines.append(
        "- 概念：向量语义检索（mxbai-embed-large）+ 关键词稀疏检索（BM25 等）双路召回，"
        "用 RRF（Reciprocal Rank Fusion）或加权合并排序。"
    )
    lines.append(
        "- 适用场景：含专有名词、编号、精确术语的查询（如 ``Recall@K``、``QDRANT_MODE``），"
        "向量检索易丢精确匹配，BM25 可补位；两端互补通常能显著提升 Recall@1。"
    )
    lines.append("### 4.3 Rerank（精排）\n")
    lines.append(
        "- 概念：召回 Top-K（如 20~50 条）后用 Cross-Encoder 逐条与 query 打分重排，"
        "只把最相关的少量片段（如 3~5 条）交给生成。"
    )
    lines.append(
        "- 适用场景：TopK 需要取大（保 Recall）但生成上下文必须精（降噪音、省 token）时；"
        "代价是每次查询多一轮 Cross-Encoder 推理，需在延迟与精度间权衡。"
    )
    lines.append("")

    p.write_text("\n".join(lines), encoding="utf-8")
    logging.getLogger("param_compare").info("report written path=%s", p)
    return p


def _main() -> int:
    args = _parse_args()
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("rag").setLevel(logging.WARNING)
    if args.offline:
        os.environ["EMBEDDING_API_BASE_URL"] = ""
        os.environ["EMBEDDING_MODEL_NAME"] = ""
    chunk_sizes = _parse_ints(args.chunk_sizes, "chunk-sizes")
    top_ks = _parse_ints(args.top_ks, "top-ks")

    raw_dir = Path(args.raw_dir)
    sources = sorted({*raw_dir.glob("*.md"), *raw_dir.glob("*.txt"), *raw_dir.glob("*.pdf")})
    if not sources:
        print(f"[error] 未在 {raw_dir} 找到任何 .md/.txt/.pdf 源文档")
        return 1

    print("=== 1. 选择 Embedder 与 Qdrant 配置（依据 .env） ===")
    emb = get_embedding()
    dim = len(emb.embed(["probe-text"])[0])
    print(f"embedder = {type(emb).__name__}（dim={dim}）")
    if args.offline or not isinstance(emb, EmbeddingClient):
        print("[warn] 离线模式：使用 FakeEmbedding，Recall 仅供流程验证")

    items = load_dataset(args.dataset)
    answerable = sum(1 for it in items if it.expected_sources)
    unanswerable = len(items) - answerable
    print(f"评测集 {len(items)} 条（有答案 {answerable} / 无答案 {unanswerable}）")

    rows: list[dict[str, Any]] = []
    misses_by_cs: dict[int, list[dict[str, Any]]] = {}
    for cs in chunk_sizes:
        collection = f"{args.collection_prefix}_cs{cs}"
        base = build_qdrant_config(collection, emb)
        cfg = replace(base, vector_size=dim) if base.vector_size != dim else base
        _rebuild_collection(cfg)

        print(f"\n=== 2. chunk_size={cs}：导入 {len(sources)} 个源 ===")
        rag = JwipcKnowledgeRAG(emb, cfg, chunk_size=cs, overlap=args.overlap)
        t0 = time.perf_counter()
        chunks = rag.add_directory(str(raw_dir))
        ingest_s = time.perf_counter() - t0
        print(f"切分片段 {len(chunks)} 个，导入耗时 {ingest_s:.1f}s")

        # 复用知识库内部的 QdrantRetriever（支持 source_filter 归因探测）。
        retriever = rag._retriever  # noqa: SLF001 - 评测入口复用内部检索器
        top_misses: list[dict[str, Any]] | None = None
        for tk in top_ks:
            print(f"\n=== 3. top_k={tk}：逐条检索计时 + Recall 统计 ===")
            avg_ms = _timed_search_avg(retriever, [it.query for it in items], tk)
            summary, misses = evaluate(retriever, items, top_ks=EVAL_TOP_KS)
            if top_misses is None:
                top_misses = misses
            print(
                f"Recall@1={summary.recall[1]:.3f}  Recall@3={summary.recall[3]:.3f}  "
                f"Recall@5={summary.recall[5]:.3f}  误召回 {summary.false_recalled}/{summary.unanswerable}"
            )
            print(f"平均检索延迟 {avg_ms:.1f} ms（{len(items)} 条查询）")
            rows.append(
                {
                    "chunk_size": cs,
                    "top_k": tk,
                    "recall1": summary.recall[1],
                    "recall3": summary.recall[3],
                    "recall5": summary.recall[5],
                    "false_recalled": summary.false_recalled,
                    "unanswerable": summary.unanswerable,
                    "avg_ms": avg_ms,
                    "ingest_s": ingest_s,
                    "chunk_count": len(chunks),
                }
            )
        misses_by_cs[cs] = top_misses or []
        with contextlib.suppress(Exception):
            rag._retriever._client.close()  # noqa: SLF001 - 释放连接

    print("\n=== 4. 汇总 ===")
    print("| chunk_size | TopK | Recall@1 | Recall@3 | Recall@5 | 延迟(ms) | 导入(s) | 片段数 |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['chunk_size']} | {r['top_k']} | {r['recall1']:.3f} | {r['recall3']:.3f} "
            f"| {r['recall5']:.3f} | {r['avg_ms']:.1f} | {r['ingest_s']:.1f} | {r['chunk_count']} |"
        )

    print("\n=== 5. 生成报告 ===")
    meta = {
        "embedder": type(emb).__name__,
        "model": getattr(emb, "model", "FakeEmbedding(离线)"),
        "dim": dim,
        "mode": cfg.mode,
        "source_count": len(sources),
        "dataset_size": len(items),
        "answerable": answerable,
        "unanswerable": unanswerable,
        "offline": bool(args.offline),
    }
    report = _write_report(
        args.report,
        meta=meta,
        rows=rows,
        misses_by_cs=misses_by_cs,
    )
    print(f"报告已写入：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
