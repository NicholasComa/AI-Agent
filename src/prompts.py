from __future__ import annotations

"""Day 4 - system prompt and message-builder for requirement analysis.

This module owns the **prompt contract** that pairs with
:class:`src.schemas.RequirementAnalysis`. The two must stay in sync: the
system prompt below defines what the LLM is asked to produce, and
:class:`RequirementAnalysis` defines what the Pydantic parser will accept.

Three deliberate choices:

1. **Force JSON-only output.** The system prompt explicitly forbids
   markdown code fences, preamble, and trailing text. The word ``"JSON"``
   is included verbatim so OpenAI-compatible ``response_format=json_object``
   mode can engage on providers that require it (including Ollama, DeepSeek,
   OpenAI, vLLM).
2. **Three in-prompt examples** (clear / vague / off-topic) instead of one.
   This teaches the model the three corners of the input space and
   dramatically improves stability on edge cases.
3. **Schema-aware guidance.** The field-by-field description mirrors the
   ``Field(..., description=...)`` in :mod:`src.schemas`, so if a future
   maintainer tweaks a constraint they will notice the mismatch.
"""


SYSTEM_PROMPT: str = """\
你是资深产品需求分析师。你的任务是把客户的一段原始需求文字转写成结构化 JSON。

## 严格要求
- **只输出合法 JSON 对象**，禁止任何额外文本（不要 markdown 代码块、不要解释、不要前后缀）。
- 严格匹配以下 6 个字段，字段名严格一致，**不要新增任何字段**。
- 如果输入模糊、缺失信息或与产品需求无关，confidence 字段应主动降低。

## 字段定义
1. **title**: 一句话需求标题，2-80 字符，能独立成句。
2. **category**: 需求分类，从 web / mobile / api / data / ai / desktop / embedded / other 中选一个。
3. **functional_points**: 2-6 条功能要点，每条 1 句话，动词开头（如"用户可登录"）。
4. **risks**: 1-4 条潜在风险（技术 / 合规 / 性能 / 资源等），每条 1 句话。
5. **clarification_questions**: 0-5 条需要向客户进一步确认的问题；输入越模糊应越多。
6. **confidence**: 0.0-1.0 的浮点数，表示你对此分析的置信度。

## 示例

### 示例 1（清晰需求）
输入：做一个电商网站，登录+商品浏览+购物车+支付
输出：{"title":"电商网站","category":"web","functional_points":["用户登录","商品浏览","购物车管理","支付下单"],"risks":["支付安全合规","高并发性能"],"clarification_questions":["需要支持哪些支付方式？"],"confidence":0.9}

### 示例 2（模糊需求）
输入：我想做个东西
输出：{"title":"未明确的产品需求","category":"other","functional_points":[],"risks":["需求范围不清","资源投入无法估算"],"clarification_questions":["您想做什么类型的产品？","目标用户是谁？","期望的核心功能是什么？","预算和时间线如何？"],"confidence":0.2}

### 示例 3（无关输入）
输入：今天天气真好
输出：{"title":"非产品需求输入","category":"other","functional_points":[],"risks":[],"clarification_questions":["请提供产品需求描述"],"confidence":0.05}

## 当前任务
请把以下客户输入转写为符合上述 schema 的 JSON。
"""

##构建标准化消息格式 把"用户问题"包装成 LLM 需要的"消息格式"
def build_messages(customer_text: str, system_prompt: str = SYSTEM_PROMPT) -> list[dict[str, str]]:
    """Build the messages list for a structured-output chat call.

    Args:
        customer_text: the raw customer input (a single user turn).
        system_prompt: override the default system prompt (mainly for tests).

    Returns:
        A two-element list: ``[system, user]`` ready to feed into
        :meth:`src.llm_client.LlmClient.chat`.
    """
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": customer_text},
    ]
