"""Day 4 - structured output schema for requirement analysis.

Defines :class:`RequirementAnalysis`, the Pydantic model that the LLM must
populate. It is the contract that ties together the system prompt
(:mod:`src.prompts`), the chat call (:class:`src.llm_client.LlmClient`),
and the parser/validator in :mod:`tests.test_structured_output`.

Three design choices worth knowing:

1. ``extra='forbid'`` - if the LLM returns fields not declared here, Pydantic
   raises ``ValidationError`` instead of silently accepting them. This catches
   hallucinations at the boundary.
2. ``Field(..., ge=0.0, le=1.0)`` on ``confidence`` - a numeric range
   guarantee that's hard to get from a free-text prompt alone.
3. Every field has a ``description=`` string. The system prompt in
   :mod:`src.prompts` mirrors these descriptions so the schema and the
   prompt cannot drift apart.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#需求分析输出模型
class RequirementAnalysis(BaseModel):
    """Structured analysis of a customer's product requirement.

    Returned by the LLM (as a JSON object) and validated by Pydantic.
    The model has no methods - it is a pure data contract.
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
