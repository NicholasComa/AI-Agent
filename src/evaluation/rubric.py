"""Rubric 打分：LLM-as-a-Judge，并把调用本身纳入追踪。

边界声明
--------

Rubric **不是主评测**。规则与标注比对（:mod:`evaluation.metrics`）是确定性、
可离线、可重复的，占检查项的大头；本模块只覆盖规则够不着的表达质量，且它的
结论只作参考。三个已知偏差必须写进报告，不能只写分数：

1. **判定不稳定** —— 同一份输入重复判 ``EVAL_JUDGE_REPEATS`` 次，报告里给出
   一致率。一致率低的样本，其分数没有解释力，应转人工复核。
2. **长度与位置偏好** —— 打分提示词固定评分锚点，且**只要求输出分数与理由**，
   不给候选答案，避免把顺序暴露成偏好信号。
3. **自洽偏差** —— 判定模型与被测模型相同时，它会系统性偏袒自己的输出风格。
   报告里必须标注这一条，不把 Judge 分数当质量结论。

追踪
----

打分调用经 :func:`observability.traced_chat` 包过，每次判定产出一条
``generation`` span。这样「谁在打分、打了多少次、耗了多少 token」与业务请求
在同一套 trace 里可查，而不是散在另一套日志中。
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from observability import Tracer, traced_chat

JUDGE_MODEL_ENV = "EVAL_JUDGE_MODEL"
"""判定模型的环境变量名；缺省与主模型一致（即不设该键）。"""

JUDGE_REPEATS_ENV = "EVAL_JUDGE_REPEATS"
"""重复判定次数的环境变量名。"""

DEFAULT_REPEATS = 3
"""缺省重复次数。取 3 是「多数票能成立」的最小值：两次打平时无法定众数。"""

MAX_REPEATS = 9
"""重复次数上限，防止误配一个很大的值把本地模型跑爆。"""

RUBRIC_MAX_SCORE = 5
"""5 分制。"""

_ANCHORS = """评分锚点（严格按此对齐，不要自由发挥）：
5 分：要点齐全、表述准确、无冗余、无臆造内容。
4 分：要点基本齐全，表述略有冗余或措辞不精确，但无事实错误。
3 分：覆盖约一半要点，或存在不影响结论的含糊表述。
2 分：仅覆盖少量要点，或存在与参考资料不符的表述。
1 分：完全未回答问题，或大面积臆造。"""

_SYSTEM_PROMPT = f"""你是一名严格的技术回答评审员。请依据给定的参考资料与问题，
对「待评回答」的**表达质量**打分。

{_ANCHORS}

只输出一个 JSON 对象，不要输出任何其它文字、不要用代码块包裹：
{{"score": <1-5 的整数>, "reason": "<不超过 40 字的中文理由>"}}
"""

#正则表达式用于从判定模型的输出中提取分数和理由
_SCORE_RE = re.compile(r'"score"\s*:\s*([1-5])')
_REASON_RE = re.compile(r'"reason"\s*:\s*"([^"]*)"')


def resolve_judge_model(default: str | None = None) -> str | None:
    """读判定模型名；环境变量为空时返回 ``default``。"""
    raw = os.getenv(JUDGE_MODEL_ENV)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def resolve_repeats() -> int:
    """读重复次数，非法值回退缺省并夹到 ``[1, MAX_REPEATS]``。"""
    raw = os.getenv(JUDGE_REPEATS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_REPEATS
    try:
        value = int(raw.strip())
    except ValueError:
        return DEFAULT_REPEATS
    return max(1, min(value, MAX_REPEATS))


class RubricResult(BaseModel):
    """一条回答的 Rubric 判定结果。

    Attributes:
        answer_id: 被测用例标识。
        scores: 每次判定的分数，顺序即判定顺序。
        mode_score: 众数分；出现次数并列时取较低的那个（偏保守）。
        agreement: 众数出现次数 ÷ 总次数，取值 ``[0, 1]``。
        reasons: 每次判定给出的理由。
        model: 判定模型名。
        parsed: 成功解析出分数的次数 ÷ 总次数。
    """

    model_config = ConfigDict(extra="forbid")

    answer_id: str
    scores: list[int] = Field(default_factory=list)
    mode_score: int | None = None
    agreement: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    model: str | None = None
    parsed: float = 0.0

    @property
    def stable(self) -> bool:
        """一致率是否达到「可采信」的下限（全部判定同分）。"""
        return self.agreement >= 1.0


def parse_score(text: str) -> tuple[int, str] | None:
    """从判定输出里抠出分数与理由。

    容忍模型把 JSON 包在代码块里、或前后带解释文字——本地小模型经常这样。
    抠不出来时返回 ``None``，由调用方计入未解析而不是当成 0 分：把解析失败
    当 0 分会让「判定模型不听话」伪装成「回答质量差」。

    Returns:
        ``(score, reason)``；无法解析时 ``None``。
    """
    score_match = _SCORE_RE.search(text)
    if score_match is None:
        # 退化路径：有些模型只回一个裸数字。
        bare = re.fullmatch(r"\s*([1-5])\s*", text)
        if bare is None:
            return None
        return int(bare.group(1)), ""
    reason_match = _REASON_RE.search(text)
    reason = reason_match.group(1).strip() if reason_match else ""
    return int(score_match.group(1)), reason


def build_messages(question: str, answer: str, *, reference: str = "") -> list[dict[str, str]]:
    """组装打分消息。

    只有 system 与一条 user 消息，``task`` 键固定为 ``rubric``，便于替身按任务
    路由。提示词里**不含**候选答案或其它样本，因此不存在位置偏好。
    """
    user_parts = [f"问题：{question}"]
    if reference:
        user_parts.append(f"参考资料：{reference}")
    user_parts.append(f"待评回答：{answer}")
    return [
        {"role": "system", "content": _SYSTEM_PROMPT, "task": "rubric"},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


class RubricScorer:
    """重复打分并计算一致率。"""

    def __init__(
        self,
        chat_fn: Callable[[list[dict[str, str]]], Awaitable[str]],
        *,
        model: str | None = None,
        repeats: int = DEFAULT_REPEATS,
        tracer: Tracer | None = None,
    ) -> None:
        """初始化。

        Args:
            chat_fn: 异步对话函数，签名 ``(messages) -> str``。
            model: 判定模型名；给了 ``tracer`` 时会写进 ``generation`` span。
            repeats: 重复次数。
            tracer: 追踪门面；为 ``None`` 时不产生 span（仅供单测直接构造）。
        """
        self._chat = traced_chat(chat_fn, tracer, model=model) if tracer else chat_fn
        self._model = model
        self._repeats = max(1, min(int(repeats), MAX_REPEATS))

    @property
    def repeats(self) -> int:
        """实际生效的重复次数。"""
        return self._repeats

    async def score(
        self,
        *,
        answer_id: str,
        question: str,
        answer: str,
        reference: str = "",
    ) -> RubricResult:
        """重复判定同一条回答。"""
        messages = build_messages(question, answer, reference=reference)
        scores: list[int] = []
        reasons: list[str] = []
        for _ in range(self._repeats):
            raw = await self._chat(messages)
            parsed = parse_score(raw)
            if parsed is None:
                continue
            score, reason = parsed
            scores.append(score)
            reasons.append(reason)

        mode_score: int | None = None
        agreement = 0.0
        if scores:
            counts = Counter(scores)
            top = max(counts.values())
            # 并列时取较低分：偏保守，避免「判高判低各半」被记成高分。
            mode_score = min(score for score, count in counts.items() if count == top)
            agreement = top / len(scores)

        return RubricResult(
            answer_id=answer_id,
            scores=scores,
            mode_score=mode_score,
            agreement=agreement,
            reasons=reasons,
            model=self._model,
            parsed=len(scores) / self._repeats,
        )


def judge_note(model: str | None) -> str:
    """生成写进报告的判定局限声明。"""
    return (
        f"Rubric 判定模型 {model or '(与主模型相同)'}；"
        "该分数仅为参考，不作质量结论，且存在自洽偏差（判定模型与被测模型相同）。"
    )


def dumps(result: RubricResult) -> str:
    """把判定结果压成一行 JSON，便于写进报告。"""
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False)


__all__ = [
    "DEFAULT_REPEATS",
    "JUDGE_MODEL_ENV",
    "JUDGE_REPEATS_ENV",
    "MAX_REPEATS",
    "RUBRIC_MAX_SCORE",
    "RubricResult",
    "RubricScorer",
    "build_messages",
    "dumps",
    "judge_note",
    "parse_score",
    "resolve_judge_model",
    "resolve_repeats",
]
