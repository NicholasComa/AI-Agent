"""Recall@K 检索评测。

职责：
- 从 ``examples/retrieval_set.json`` 加载带人工标注的评测集（
  query / expected_sources / note）。
- 对每条 query 用检索器取 Top-K，统计 Recall@1 / @3 / @5；
  无答案样本（``expected_sources`` 为空）不参与 Recall，单独统计误召回。
- 对未命中样本做环节归因（解析/覆盖、过滤、切分、Embedding、TopK/排序），
  对应第 5 周通过标准「能判断问题出在哪一类环节」；切分归因依赖调用方
  注入 :class:`ChunkingProbe`（见 :mod:`rag.probes`），评测本身不触碰外部服务。
- 生成 Markdown 评测报告（评测集构成、Recall 统计、错误归因）。

本模块不依赖外部服务：检索器由调用方注入（:class:`QdrantRetriever`
或 :class:`ListRetriever` 均可），评测本身只做数值统计与文本归因。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .retriever import RetrievalResult

logger = logging.getLogger(__name__)

DEFAULT_TOP_KS = (1, 3, 5)
DEFAULT_REPORT_PATH = Path("docs/week05_retrieval_eval.md")

# 过滤正确性探测的检索宽度：取足够宽以覆盖同来源全部片段，
# 用于校验「source_filter 之后是否只返回该来源」。
_FILTER_PROBE_K = 50


class Retriever(Protocol):
    """评测所需的检索器接口（``search`` 签名与 :class:`QdrantRetriever` 对齐）。"""

    def search(
        self,
        query: str,
        top_k: int = 3,
        source_filter: str | None = None,
    ) -> list[RetrievalResult]:
        """检索 Top-K 片段；``source_filter`` 非 None 时按来源精确过滤。"""
        ...


class ChunkingProbe(Protocol):
    """切分归因探测能力：用不同切分参数重建索引并检索。

    由调用方注入（如 :mod:`rag.probes` 的 :class:`QdrantChunkingProbe`），
    评测模块只依赖本协议读取结果，不直接触碰 Embedding / 向量库，
    从而保持 :mod:`rag.evaluate` 不依赖外部服务。
    """

    def best_scores(
        self,
        query: str,
        source: str,
        *,
        top_k: int = 5,
    ) -> dict[str, float]:
        """按各切分方案检索一次，返回 ``{方案描述: 该来源最高分}``。"""
        ...


@dataclass(frozen=True)
class EvalItem:
    """一条评测样本。"""

    query: str
    expected_sources: tuple[str, ...]  # 期望命中的来源文件名（ground truth）
    note: str = ""


@dataclass(frozen=True)
class EvalSummary:
    """评测统计结果。"""

    total: int  # 样本总数
    answerable: int  # 有期望来源的样本数（参与 Recall 计算）
    unanswerable: int  # 无期望来源的样本数（观察误召回）
    hits: dict[int, int]  # K -> 命中样本数
    recall: dict[int, float]  # K -> Recall@K
    false_recalled: int  # 无答案样本中前 max(K) 条仍返回结果的样本数


def load_dataset(path: str | Path) -> list[EvalItem]:
    """从 JSON 文件加载评测集并校验字段。"""
    p = Path(path)
    if not p.exists():
        msg = f"retrieval set not found: {p}"
        raise FileNotFoundError(msg)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"invalid JSON in {p}: {exc.msg}"
        raise ValueError(msg) from exc
    if not isinstance(raw, list):
        msg = f"retrieval set must be a JSON array, got {type(raw).__name__}"
        raise ValueError(msg)

    items: list[EvalItem] = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            msg = f"item {i} must be an object, got {type(row).__name__}"
            raise ValueError(msg)
        query = row.get("query")
        if not isinstance(query, str) or not query.strip():
            msg = f"item {i}: 'query' must be a non-empty string"
            raise ValueError(msg)
        expected = row.get("expected_sources")
        if not isinstance(expected, list):
            msg = f"item {i}: 'expected_sources' must be a list"
            raise ValueError(msg)
        items.append(
            EvalItem(
                query=query,
                expected_sources=tuple(str(s) for s in expected),
                note=str(row.get("note", "")),
            )
        )
    return items


def evaluate(
    retriever: Retriever,
    items: list[EvalItem],
    top_ks: tuple[int, ...] = DEFAULT_TOP_KS,
    chunking_probe: ChunkingProbe | None = None,
    detail_out: list[dict[str, Any]] | None = None,
) -> tuple[EvalSummary, list[dict[str, Any]]]:
    """对每条样本做检索并统计 Recall@K。

    命中规则：期望来源非空时，前 K 条结果里出现任一期望来源即算命中；
    期望来源为空（无答案样本）时不计入 Recall，只统计是否误召回。

    Args:
        retriever: 检索器（实现 :class:`Retriever` 协议）。
        items: 评测样本列表。
        top_ks: 参与统计的 K 值元组。
        chunking_probe: 可选，切分归因探测；注入后可为疑似
            「Embedding 表达不足」的未命中样本进一步区分切分参数问题。
        detail_out: 可选，收集全部样本的检索明细（query / expected_sources /
            results / kind，未命中附 diagnosis），供控制台展示；不传则跳过。

    Returns:
        (summary, misses)：``misses`` 为未命中 / 误召回样本的明细
        （含 query、期望来源、前 K 来源、归因），供报告使用。
    """
    max_k = max(top_ks)
    hits = dict.fromkeys(top_ks, 0)
    answerable = 0
    unanswerable = 0
    false_recalled = 0
    misses: list[dict[str, Any]] = []

    for item in items:
        results = retriever.search(item.query, top_k=max_k)
        top_sources = [r.source for r in results]
        if detail_out is not None:
            detail_out.append(
                {
                    "query": item.query,
                    "expected_sources": list(item.expected_sources),
                    "results": [{"source": r.source, "score": r.score} for r in results],
                }
            )
        if not item.expected_sources:
            unanswerable += 1
            if detail_out is not None:
                detail_out[-1]["kind"] = "false_recall" if results else "clean"
            if results:
                false_recalled += 1
                misses.append(
                    {
                        "kind": "false_recall",
                        "query": item.query,
                        "expected_sources": [],
                        "top_sources": top_sources,
                        "note": item.note,
                        "diagnosis": "无答案样本仍返回了结果（应拒答却未拒答）",
                    }
                )
            continue

        answerable += 1
        expected = set(item.expected_sources)
        for k in top_ks:
            if any(r.source in expected for r in results[:k]):
                hits[k] += 1
        hit = any(r.source in expected for r in results[:max_k])
        if detail_out is not None:
            detail_out[-1]["kind"] = "hit" if hit else "miss"
        if not hit:
            diagnosis = diagnose_miss(
                retriever,
                item.query,
                item.expected_sources,
                results,
                chunking_probe=chunking_probe,
            )
            misses.append(
                {
                    "kind": "miss",
                    "query": item.query,
                    "expected_sources": list(item.expected_sources),
                    "top_sources": top_sources,
                    "note": item.note,
                    "diagnosis": diagnosis,
                }
            )
            if detail_out is not None:
                detail_out[-1]["diagnosis"] = diagnosis

    recall = {k: (hits[k] / answerable if answerable else 0.0) for k in top_ks}
    summary = EvalSummary(
        total=len(items),
        answerable=answerable,
        unanswerable=unanswerable,
        hits=hits,
        recall=recall,
        false_recalled=false_recalled,
    )
    return summary, misses


def diagnose_miss(
    retriever: Retriever,
    query: str,
    expected_sources: tuple[str, ...],
    results: list[RetrievalResult],
    chunking_probe: ChunkingProbe | None = None,
) -> str:
    """对未命中样本做环节归因（启发式，非精确诊断）。

    对每个期望来源单独按 ``source_filter`` 检索，比较其最高分与普通
    检索第 K 名的分数：

    - 过滤结果包含其它来源 → 过滤逻辑错误；
    - 该来源在索引中无任何片段 → 解析 / 知识库覆盖问题；
    - 普通检索无结果但过滤检索可命中 → 检索链路异常；
    - 最高分不低于第 K 名却被排挤出候选 → TopK / 排序问题；
    - 最高分明显低于第 K 名 → Embedding 表达不足；若注入
      :class:`ChunkingProbe`，先用不同切分参数重建索引对比，能提升到
      第 K 名水平即判为切分参数问题。

    探测过程中任何异常只记录日志并跳过该来源，不阻断整体归因。
    """
    kth_score = results[-1].score if results else 0.0
    kth_rank = len(results)
    parts: list[str] = []
    for src in expected_sources:
        try:
            filtered = retriever.search(query, top_k=_FILTER_PROBE_K, source_filter=src)
        except TypeError:
            parts.append(f"{src}：检索器不支持来源过滤，无法定位（请用 QdrantRetriever 复跑）")
            continue
        except Exception as exc:
            logger.exception("filter probe failed query=%r source=%r", query, src)
            parts.append(f"{src}：过滤探测执行异常（{type(exc).__name__}），跳过该来源")
            continue
        if not filtered:
            parts.append(f"{src}：索引中无该来源片段（解析 / 知识库覆盖问题）")
            continue
        wrong = {r.source for r in filtered if r.source != src}
        if wrong:
            parts.append(f"{src}：过滤逻辑错误（source_filter={src!r} 却返回了 {sorted(wrong)}）")
            continue
        if not results:
            parts.append(f"{src}：普通检索无任何结果但过滤检索可命中（检索链路异常）")
            continue
        best = filtered[0].score
        if best >= kth_score - 1e-9:
            parts.append(
                f"{src}：最高分 {best:.4f} 不低于第 {kth_rank} 名 {kth_score:.4f}，"
                "正确片段被排挤出候选（TopK / 排序问题）"
            )
        else:
            parts.append(
                f"{src}：最高分 {best:.4f} 低于第 {kth_rank} 名 {kth_score:.4f}，"
                f"{_attrib_chunking_or_embedding(chunking_probe, query, src, kth_score, kth_rank)}"
            )
    return "；".join(parts)


def _attrib_chunking_or_embedding(
    chunking_probe: ChunkingProbe | None,
    query: str,
    source: str,
    kth_score: float,
    kth_rank: int,
) -> str:
    """区分「切分参数问题」与「Embedding 表达不足」。

    注入 :class:`ChunkingProbe` 时，用不同切分参数重建索引并检索该来源；
    若任一方案的最高分能达到第 K 名水平，说明调整切分参数即可改善召回，
    判为切分参数问题；否则维持 Embedding 表达不足。探测异常只降级到
    Embedding 归因并附注，不抛出。
    """
    if chunking_probe is None:
        return "（Embedding 表达不足）"
    try:
        scores = chunking_probe.best_scores(query, source)
    except Exception as exc:
        logger.exception("chunking probe failed query=%r source=%r", query, source)
        return f"（Embedding 表达不足；切分探测执行异常：{type(exc).__name__}）"
    if not scores:
        return "（Embedding 表达不足）"
    best_name, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score >= kth_score - 1e-9:
        return (
            f"调整切分参数（{best_name}）后最高分 {best_score:.4f} "
            f"达到第 {kth_rank} 名水平，判定为切分参数问题"
        )
    return "（Embedding 表达不足）"


def write_report(
    path: str | Path,
    *,
    items: list[EvalItem],
    summary: EvalSummary,
    misses: list[dict[str, Any]],
    meta: dict[str, Any],
) -> Path:
    """生成 Markdown 评测报告并返回写入路径。

    Args:
        path: 报告输出路径。
        items: 评测集全部样本。
        summary: :func:`evaluate` 返回的统计结果。
        misses: :func:`evaluate` 返回的未命中 / 误召回明细。
        meta: 运行环境信息（embedder / model / dim / mode / collection /
            chunk_count 等），用于报告「在什么环境下测得」。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# 检索评测报告（Recall@K）\n")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(
        f"- Embedding：{meta.get('embedder', '?')}"
        f"（模型 {meta.get('model', '-')}，维度 {meta.get('dim', '-')}）"
    )
    lines.append(
        f"- Qdrant：mode={meta.get('mode', '?')}，collection={meta.get('collection', '?')}，"
        f"索引片段 {meta.get('chunk_count', '?')} 个"
    )
    lines.append("")

    # 评测集构成
    lines.append("## 一、评测集构成\n")
    note_counts: dict[str, int] = {}
    for item in items:
        key = item.note or "未分类"
        note_counts[key] = note_counts.get(key, 0) + 1
    lines.append(
        f"共 **{summary.total}** 条：有期望来源 {summary.answerable} 条（参与 Recall 计算）、"
        f"无期望来源 {summary.unanswerable} 条（观察误召回）。"
    )
    for key, count in sorted(note_counts.items()):
        lines.append(f"- {key}：{count} 条")
    lines.append("")

    # Recall 统计
    lines.append("## 二、Recall@K 统计\n")
    lines.append("| K | 命中数 | Recall |")
    lines.append("|---|--------|--------|")
    for k in sorted(summary.recall):
        lines.append(
            f"| @{k} | {summary.hits[k]} / {summary.answerable} | {summary.recall[k]:.3f} |"
        )
    lines.append("")
    lines.append(
        f"无答案样本误召回：{summary.false_recalled} / {summary.unanswerable}"
        f"（前 {max(summary.recall)} 条仍返回了结果，理想为 0）\n"
    )

    # 错误样本与归因
    miss_items = [m for m in misses if m["kind"] == "miss"]
    false_items = [m for m in misses if m["kind"] == "false_recall"]
    lines.append("## 三、错误样本与归因\n")
    if not miss_items and not false_items:
        lines.append("无未命中 / 误召回样本。")
    if miss_items:
        lines.append(f"### 未命中（{len(miss_items)} 条）\n")
        for i, m in enumerate(miss_items, 1):
            lines.append(f"{i}. **{m['query']}**")
            lines.append(f"   - 期望来源：{', '.join(m['expected_sources']) or '（空）'}")
            lines.append(
                f"   - 前 {max(summary.recall)} 来源：{', '.join(m['top_sources']) or '（无结果）'}"
            )
            lines.append(f"   - 归因：{m['diagnosis']}")
        lines.append("")
    if false_items:
        lines.append(f"### 无答案误召回（{len(false_items)} 条）\n")
        for i, m in enumerate(false_items, 1):
            lines.append(f"{i}. **{m['query']}**")
            lines.append(
                f"   - 前 {max(summary.recall)} 来源：{', '.join(m['top_sources']) or '（无结果）'}"
            )
            lines.append(f"   - 说明：{m['diagnosis']}")
        lines.append("")

    # 结论
    lines.append("## 四、结论与下一步\n")
    recall3 = summary.recall.get(3, 0.0)
    if recall3 >= 0.8:
        verdict = "Recall@3 较高，检索质量整体可用；可继续优化 Recall@1。"
    elif recall3 >= 0.5:
        verdict = "Recall@3 中等，存在可定位的召回缺口，见上表归因。"
    else:
        verdict = "Recall@3 偏低，建议优先处理归因中高频出现的环节。"
    lines.append(verdict)
    lines.append(
        "- 改进方向：按归因频次优先处理 —— 覆盖缺失补资料、切分参数调整、"
        "Embedding 换模型、TopK / 过滤阈值调整。\n"
    )

    p.write_text("\n".join(lines), encoding="utf-8")
    logger.info("evaluation.report.written path=%s", p)
    return p
