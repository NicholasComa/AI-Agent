"""可注入的 Fake ChatFn。

真实链路里 ``chat_fn`` 会是 :class:`src.llm_client.LlmClient` 的适配；测试与
演示则注入这里提供的确定性实现，保证 6 节点可脱离模型稳定跑通。

契约与 :data:`src.rag.generator.ChatFn` 一致：输入 messages，返回文本。
节点在 system 消息上挂了 ``task`` 键（真实适配器忽略该键），Fake 据此路由
返回对应的结构化 JSON，避免把「该节点该返回什么」写死在节点内部。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

type ChatFn = Callable[[list[dict[str, str]]], Awaitable[str]]


async def fake_chat(messages: list[dict[str, str]]) -> str:
    """根据 system 消息里的 ``task`` 键返回对应的确定性 JSON 文本。"""
    task = "classify"
    for message in messages:
        if message.get("task"):
            task = message["task"]
            break
    user_text = messages[-1]["content"] if messages else ""

    if task == "classify":
        return _classify_json(user_text)
    if task == "functional_points":
        return json.dumps(
            {
                "functional_points": [
                    "支持用户登录与注册",
                    "提供商品浏览与搜索",
                    "实现购物车与订单管理",
                    "接入多种支付方式",
                ]
            },
            ensure_ascii=False,
        )
    if task == "risk":
        return json.dumps(
            {
                "risks": [
                    "支付链路需满足等保与资金安全合规",
                    "高并发下需保证库存与订单一致性",
                    "第三方登录依赖外部服务可用性",
                ]
            },
            ensure_ascii=False,
        )
    if task == "test_points":
        return json.dumps(
            {
                "test_points": [
                    "正常：完整下单到支付成功",
                    "异常：余额不足或支付失败回滚",
                    "边界：超长商品标题与特殊字符",
                    "回归：历史订单查询不受影响",
                ]
            },
            ensure_ascii=False,
        )
    return "{}"


def _classify_json(text: str) -> str:
    """按需求文本启发式返回分类结果，覆盖正常 / 含糊 / 无关三类。"""
    if any(key in text for key in ("天气", "今天", "无关", "闲聊")):
        return json.dumps(
            {
                "category": "other",
                "confidence": 0.08,
                "clarification_questions": ["请提供产品需求描述，而非闲聊内容"],
                "title": "非产品需求输入",
            },
            ensure_ascii=False,
        )
    if len(text) < 25 or "我想做个东西" in text or "推荐东西" in text:
        return json.dumps(
            {
                "category": "other",
                "confidence": 0.3,
                "clarification_questions": [
                    "这个系统面向哪些用户？",
                    "核心要解决什么问题？",
                    "主要在什么场景使用？",
                ],
                "title": "信息不足的需求",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "category": "web",
            "confidence": 0.9,
            "clarification_questions": [],
            "title": "B2C 电商网站",
        },
        ensure_ascii=False,
    )


def make_fake_chat() -> ChatFn:
    """返回默认 Fake ChatFn（演示 / 测试注入用）。"""
    return fake_chat
