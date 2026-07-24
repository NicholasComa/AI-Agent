from __future__ import annotations

"""Day 4 - 需求分析的系统提示词与消息构造器。

本模块持有与 :class:`src.schemas.RequirementAnalysis` 配对的
**提示词契约**。两者必须保持同步：下面的系统提示词定义了要求 LLM 产出什么，
而 :class:`RequirementAnalysis` 定义了 Pydantic 解析器会接受什么。

有三个刻意为之的选择：

1. **强制只输出 JSON。** 系统提示词明确禁止 markdown 代码块、前言和尾巴文字。
   原样包含单词 ``"JSON"``，以便需要它的 OpenAI 兼容服务（包括 Ollama、
   DeepSeek、OpenAI、vLLM）可以启用 ``response_format=json_object`` 模式。
2. **三段提示词内示例**（清晰 / 模糊 / 无关），而不是一段。
   这教会模型输入空间三个角落的样子，并显著提升在边界情形下的稳定性。
3. **感知 schema 的引导。** 逐字段的描述镜像了 :mod:`src.schemas` 里的
   ``Field(..., description=...)``，这样将来维护者若调整了某个约束，会注意到
   提示词与 schema 之间的不一致。
"""


SYSTEM_PROMPT: str = """\
你是资深产品需求分析师。你的任务是把客户的一段原始需求文字转写成结构化JSON。

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


## 构建标准化消息格式：把"用户问题"包装成 LLM 需要的"消息格式"
def build_messages(customer_text: str, system_prompt: str = SYSTEM_PROMPT) -> list[dict[str, str]]:
    """为一次结构化输出对话调用构造 messages 列表。

    Args:
        customer_text: 原始客户输入（单条用户话语）。
        system_prompt: 覆盖默认的系统提示词（主要用于测试）。

    Returns:
        一个包含两个元素的列表：``[system, user]``，可直接喂给
        :meth:`src.llm_client.LlmClient.chat`。
    """
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": customer_text},
    ]
