"""评测层测试用的替身与小工具。

刻意不复用 ``tests/observability`` 里的同名替身：两个测试目录在 pytest 的
``sys.path`` 处理下都只是普通目录，跨目录导入会依赖 rootdir 的插入顺序，而
``tests/observability`` 这个目录名与 ``src/observability`` 同名，一旦被当成包
导入就会遮住真正的被测模块。评测层的替身都只依赖 ``tmp_path``，重复二十行比
制造一个隐蔽的导入歧义更划算。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from evaluation.dataset import load_cases  # noqa: F401  (供测试直接引用)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = PROJECT_ROOT / "data" / "golden" / "week10_golden.jsonl"


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    """把若干字典写成 JSONL。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def qa_case(**overrides: Any) -> dict[str, Any]:
    """一条最小可用的知识问答用例。"""
    case: dict[str, Any] = {
        "id": "qa-999",
        "scenario": "knowledge_qa",
        "group": "core",
        "input": {"query": "什么是 RAG？", "top_k": 3, "min_score": 0.0},
        "reference": {
            "expect_found": True,
            "expect_sources": ["rag_concepts.md"],
            "expect_points": ["检索增强生成"],
        },
        "checks": ["retrieval_hit@3", "citation_source_hit", "answer_point_coverage>=0.6"],
        "note": "替身",
    }
    case.update(overrides)
    return case


def req_case(**overrides: Any) -> dict[str, Any]:
    """一条最小可用的需求分析用例。"""
    case: dict[str, Any] = {
        "id": "req-999",
        "scenario": "requirement_analysis",
        "group": "normal",
        "input": {"text": "做一个电商网站"},
        "reference": {
            "expect_category": "web",
            "expect_needs_clarify": False,
            "expect_points": ["登录"],
        },
        "checks": ["category_match", "clarification_expected"],
        "note": "替身",
    }
    case.update(overrides)
    return case


def tool_case(**overrides: Any) -> dict[str, Any]:
    """一条最小可用的工具调用用例。"""
    case: dict[str, Any] = {
        "id": "tool-999",
        "scenario": "tool_call",
        "group": "denied",
        "input": {"tool": "read_file", "arguments": {"path": "../../etc/passwd"}},
        "reference": {
            "expect_tool": "read_file",
            "expect_outcome": "denied",
            "expect_deny_kind": "forbidden",
        },
        "checks": ["tool_selected", "args_schema_valid", "outcome_match", "deny_enforced"],
        "note": "替身",
    }
    case.update(overrides)
    return case


def workflow_state(**overrides: Any) -> dict[str, Any]:
    """一份合规的工作流状态。"""
    state: dict[str, Any] = {
        "category": "web",
        "confidence": 0.9,
        "needs_clarify": False,
        "clarification_questions": [],
        "functional_points": ["支持用户登录"],
        "risks": ["支付合规"],
        "test_points": ["正常：登录成功"],
        "trace": ["classify", "functional_points"],
    }
    state.update(overrides)
    return state


def make_chat(replies: list[str]) -> Callable[[list[dict[str, str]]], Awaitable[str]]:
    """按顺序返回既定答复的对话函数；用完后重复最后一条。"""
    state = {"index": 0}

    async def _chat(messages: list[dict[str, str]]) -> str:  # noqa: ARG001
        index = min(state["index"], len(replies) - 1)
        state["index"] += 1
        return replies[index]

    return _chat


def constant_chat(reply: str) -> Callable[[list[dict[str, str]]], Awaitable[str]]:
    """永远返回同一答复的对话函数。"""

    async def _chat(messages: list[dict[str, str]]) -> str:  # noqa: ARG001
        return reply

    return _chat
