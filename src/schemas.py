"""Day 4 - 需求分析的结构化输出模型。

定义 :class:`RequirementAnalysis`，即 LLM 必须填充的 Pydantic 模型。
它是把下面三者绑定在一起的契约：系统提示词（:mod:`src.prompts`）、
对话调用（:class:`src.llm_client.LlmClient`），以及
:mod:`tests.test_structured_output` 里的解析/校验器。

有三个值得一提的设计选择：

1. ``extra='forbid'`` - 如果 LLM 返回了这里没有声明的字段，Pydantic 会
   抛出 ``ValidationError``，而不是默默接受。这能在边界处拦住幻觉。
2. ``confidence`` 上的 ``Field(..., ge=0.0, le=1.0)`` - 一个数值范围保证，
   光靠自由文本提示词很难拿到。
3. 每个字段都带有 ``description=`` 字符串。:mod:`src.prompts` 里的系统提示词
   镜像了这些描述，从而让 schema 和提示词不会各说各话。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


# 需求分析输出模型
class RequirementAnalysis(BaseModel):
    """对客户产品需求的结构化分析。

    由 LLM 返回（作为 JSON 对象），并由 Pydantic 做校验。
    该模型没有任何方法——它就是一个纯数据契约。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        ...,
        min_length=2,
        max_length=80,
        description="一句话需求标题，2-80 字符，能独立成句",
    )
    category: str = Field(
        ...,
        description=(
            "需求分类，建议从以下选项中挑选一个："
            "web / mobile / api / data / ai / desktop / embedded / other"
        ),
    )
    functional_points: list[str] = Field(
        default_factory=list,
        description="2-6 条功能要点，每条 1 句话，动词开头",
    )
    risks: list[str] = Field(
        default_factory=list,
        description="1-4 条潜在风险（技术 / 合规 / 性能 / 资源等），每条 1 句话",
    )
    clarification_questions: list[str] = Field(
        default_factory=list,
        description=("0-5 条需要向客户进一步确认的问题；输入越模糊应越多；无关或清晰输入可为空"),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "模型对此分析的置信度，0.0 = 完全猜的，1.0 = 非常确定。对模糊/无关输入应主动降低"
        ),
    )
