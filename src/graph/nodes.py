"""RequirementAnalysisWorkflow 的 7 个节点（一节点一职责，硬约束）。

图不把逻辑堆在单个节点或单个 Prompt 里：分类、功能点、检索、风险、测试点、
报告各由一个节点负责，歧义时由 ``clarify`` 节点中断等待人工补充。

节点间通过函数注入的 :class:`Deps` 拿到 ``chat_fn`` / ``rag`` / ``config``，
因此节点本身不依赖具体客户端，测试可注入 Fake。每个节点返回**局部 patch**
（只回自己负责的字段），并读出旧 ``trace`` 追加自身节点名后回写，使执行路径
始终可观察。列表型字段（trace / human_answers）一律先读旧值再追加，避免被
后续节点整体覆盖。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langgraph.types import interrupt

from .config import WorkflowConfig
from .state import WorkflowState

# LLM 调用契约：输入 messages，返回模型输出文本（awaitable）。
# 与 src.rag.generator.ChatFn 保持一致。
type ChatFn = Callable[[list[dict[str, str]]], Awaitable[str]]


@dataclass
class Deps:
    """节点运行所需的外部依赖，由 :func:`graph.workflow.build_requirement_workflow` 注入。"""

    chat_fn: ChatFn
    """异步对话函数（messages -> text）；真实链路适配 LlmClient，测试注入 Fake。"""
    rag: object | None
    """可选 RAG 检索器（实现 retrieve(query, top_k) -> list[RetrievalResult]）；None 时视为降级。"""
    config: WorkflowConfig
    """工作流运行参数。"""


def _trace(state: WorkflowState) -> list[str]:
    """读出当前 trace，供节点追加自身节点名后回写。"""
    return list(state.get("trace") or [])


def _extract_json(text: str) -> dict[str, Any]:
    """从模型文本里抠出第一个 JSON 对象；找不到或非法时返回空 dict。

    兼容 ```json 代码围栏与前后杂文本，避免真实模型偶尔的格式噪声导致整节点失败。
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return {}


async def classify(state: WorkflowState, *, deps: Deps) -> dict:
    """分类 + 置信度：复用 schemas.RequirementAnalysis 的字段作为输出契约。"""
    messages = [
        {
            "role": "system",
            "task": "classify",
            "content": (
                "你是需求分析师。判断需求分类（web/mobile/api/data/ai/desktop/embedded/other）、"
                "置信度 0-1，以及是否需要向用户澄清。仅输出 JSON："
                '{"category": str, "confidence": float, "clarification_questions": [str]}。'
            ),
        },
        {"role": "user", "content": state["requirement_text"]},
    ]
    raw = await deps.chat_fn(messages)
    obj = _extract_json(raw)
    confidence = float(obj.get("confidence", 0.0))
    confidence = max(0.0, min(1.0, confidence))
    questions = obj.get("clarification_questions") or []
    needs_clarify = confidence < deps.config.ambiguity_threshold or bool(questions)
    return {
        "category": obj.get("category", "other"),
        "confidence": confidence,
        "clarification_questions": list(questions),
        "needs_clarify": needs_clarify,
        "trace": _trace(state) + ["classify"],
    }


async def functional_points(state: WorkflowState, *, deps: Deps) -> dict:
    """产出 2-6 条动词开头的功能点，结合人工补充。"""
    # 拼接人工补充答案（无则空串）
    extra = "\n".join(state.get("human_answers") or [])
    user_content = f"需求：{state['requirement_text']}"
    if extra:
        user_content += f"\n人工补充：{extra}"
    messages = [
        {
            "role": "system",
            "task": "functional_points",
            "content": '拆解需求为 2-6 条功能要点，每条动词开头、一句话。仅输出 JSON：{"functional_points": [str]}。',
        },
        {"role": "user", "content": user_content},
    ]
    raw = await deps.chat_fn(messages)
    obj = _extract_json(raw)
    return {
        "functional_points": list(obj.get("functional_points") or []),
        "trace": _trace(state) + ["functional_points"],
    }


async def rag_retrieve(state: WorkflowState, *, deps: Deps) -> dict:
    """调用 RAG 检索并把结果落成 rag_context。

    rag 为 None 或检索抛错时置 ``rag_degraded=True`` 并继续，绝不抛异常中断图。
    """
    if deps.rag is None:
        return {
            "rag_context": [],
            "rag_degraded": True,
            "trace": _trace(state) + ["rag_retrieve"],
        }
    try:
        query = state["requirement_text"]
        if state.get("human_answers"):
            query += " " + " ".join(state["human_answers"])
        results = deps.rag.retrieve(query, top_k=deps.config.rag_top_k)
        context = [
            {
                "chunk_id": r.chunk_id,
                "source": r.source,
                "text": r.text,
                "score": r.score,
            }
            for r in results
            if r.score >= deps.config.rag_min_score
        ]
        return {
            "rag_context": context,
            "rag_degraded": len(context) == 0,
            "trace": _trace(state) + ["rag_retrieve"],
        }
    except Exception:
        return {
            "rag_context": [],
            "rag_degraded": True,
            "trace": _trace(state) + ["rag_retrieve"],
        }


async def risk(state: WorkflowState, *, deps: Deps) -> dict:
    """结合功能点与检索资料输出 1-4 条风险；RAG 降级时追加依据不足提示。"""
    messages = [
        {
            "role": "system",
            "task": "risk",
            "content": '结合功能点与检索资料，输出 1-4 条潜在风险（技术/合规/性能/资源）。仅输出 JSON：{"risks": [str]}。',
        },
        {
            "role": "user",
            "content": f"功能点：{state.get('functional_points') or []}\n检索资料：{state.get('rag_context') or []}",
        },
    ]
    raw = await deps.chat_fn(messages)
    obj = _extract_json(raw)
    risks = list(obj.get("risks") or [])
    if state.get("rag_degraded"):
        risks = risks + ["内部资料检索不足，风险判断依据有限，仅供参考"]
    return {"risks": risks, "trace": _trace(state) + ["risk"]}


async def test_points(state: WorkflowState, *, deps: Deps) -> dict:
    """产出覆盖正常 / 异常 / 边界 / 回归四类的测试点。"""
    messages = [
        {
            "role": "system",
            "task": "test_points",
            "content": '基于功能点与风险，写出测试点，覆盖正常/异常/边界/回归四类。仅输出 JSON：{"test_points": [str]}。',
        },
        {
            "role": "user",
            "content": f"功能点：{state.get('functional_points') or []}\n风险：{state.get('risks') or []}",
        },
    ]
    raw = await deps.chat_fn(messages)
    obj = _extract_json(raw)
    return {
        "test_points": list(obj.get("test_points") or []),
        "trace": _trace(state) + ["test_points"],
    }


def report(state: WorkflowState) -> dict:
    """汇总报告（纯格式化，不调模型），引用追溯来自 rag_context 的 source+chunk_id。"""
    lines = [
        f"# 需求分析报告（分类：{state.get('category', '?')}，置信度：{state.get('confidence', 0.0):.2f}）",
        "",
        "## 功能点",
    ]
    for point in state.get("functional_points") or []:
        lines.append(f"- {point}")
    lines.append("")
    lines.append("## 风险")
    for item in state.get("risks") or []:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 测试点")
    for item in state.get("test_points") or []:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 引用资料")
    context = state.get("rag_context") or []
    if state.get("rag_degraded") or not context:
        lines.append("- 本次未检索到内部资料，结论仅供参考。")
    else:
        for chunk in context:
            lines.append(f"- {chunk['source']}#{chunk['chunk_id']}（相似度 {chunk['score']:.2f}）")
    return {"report": "\n".join(lines), "trace": _trace(state) + ["report"]}


async def clarify(state: WorkflowState, *, deps: Deps) -> dict:
    """歧义时中断，列出澄清问题；人工补充后以 resume 值继续并写入 human_answers。"""
    questions = state.get("clarification_questions") or ["请补充需求关键信息"]
    # interrupt 在此挂起整个图，返回值即人工输入；非 list 时归一为单条字符串。
    reply = interrupt({"questions": questions, "round": state.get("clarify_rounds", 0) + 1})
    previous = list(state.get("human_answers") or [])
    if isinstance(reply, list):
        previous.extend(str(item) for item in reply)
    else:
        previous.append(str(reply))
    return {
        "human_answers": previous,
        "clarify_rounds": state.get("clarify_rounds", 0) + 1,
        "trace": _trace(state) + ["clarify"],
    }


def route_after_classify(state: WorkflowState) -> str:
    """classify 之后：歧义走 clarify，否则直接进入 functional_points。"""
    return "clarify" if state.get("needs_clarify") else "functional_points"
