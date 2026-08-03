"""Day 1 — Agent Loop 最小验证。

目的：保证 LangChain v1 ``create_agent`` 接入成功 + 跑通最小 Agent Loop。

本文件只验证最简场景：

* 空工具 + 模型直接给 final answer → loop 单步终止。
* 验证模型调用与 Agent Loop 的关系：模型只调用 1 次，因为没有 ``tool_calls``。

工具调用 / 工具结果回填 / 多步循环 等场景在 Day 2–Day 3 用
``FakeMessagesListChatModel`` 自定义子类验证；本测试不替它们做前置。
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel


def test_create_agent_smoke_returns_compiled_agent() -> None:
    """``create_agent`` 必须能成功构造一个可调用的 Agent 对象。"""
    model = FakeListChatModel(responses=["dummy"])
    agent = create_agent(model=model, tools=[], system_prompt="smoke test")
    # CompiledStateGraph 有 invoke / stream 入口
    assert hasattr(agent, "invoke")
    assert hasattr(agent, "stream")


def test_minimal_loop_terminates_with_empty_tools() -> None:
    """Day 1 验收:空工具 + 模型直接给 final answer → 单步终止。

    返回结构是 ``{"messages": [HumanMessage, AIMessage, ...]}``,
    最后一条必须是无 ``tool_calls`` 的 AIMessage(即 final answer)。

    核心不变量:空工具下 Agent Loop 只调用模型 **1 次**。用计数包装
    兜底,避免 ``FakeListChatModel`` 循环复用 response 掩盖「多调一次」
    的回归。
    """
    calls = {"n": 0}

    class CountingModel(FakeListChatModel):
        def invoke(self, *args, **kwargs):
            calls["n"] += 1
            return super().invoke(*args, **kwargs)

    model = CountingModel(responses=["hi from agent"])
    agent = create_agent(model=model, tools=[], system_prompt="You are a test agent.")
    result = agent.invoke({"messages": [{"role": "user", "content": "say hi"}]})

    # 1. 返回结构
    assert "messages" in result
    msgs = result["messages"]

    # 2. 至少包含用户输入与模型最终回答
    assert len(msgs) >= 2
    assert msgs[0].type == "human"
    assert msgs[-1].type == "ai"

    # 3. 终止条件:模型未产生 tool_calls → 视为 final answer
    final = msgs[-1]
    assert final.tool_calls == []
    assert final.content == "hi from agent"

    # 4. 核心不变量:空工具下模型只被调用一次(单 tick)
    assert calls["n"] == 1


def test_agent_loop_vs_single_model_call() -> None:
    """对比:模型调用 vs Agent Loop。

    * 真接调模型:产生 1 条 AIMessage,模型被调用 1 次。
    * Agent Loop 跑一次:消息列表更长(human + ai,经状态图流转),
      但底层模型同样只被调用 1 次 —— 即一个 tick 内两者等价。

    用计数包装分别测量两段,避免依赖 ``FakeListChatModel`` 单元素
    循环复用而掩盖调用次数差异。
    """
    calls = {"n": 0}

    class CountingModel(FakeListChatModel):
        def invoke(self, *args, **kwargs):
            calls["n"] += 1
            return super().invoke(*args, **kwargs)

    model = CountingModel(responses=["just one tick"])
    agent = create_agent(model=model, tools=[], system_prompt="")

    # 1. 直调模型 —— 一次调用,返回 1 条 AIMessage
    calls["n"] = 0
    direct = model.invoke("ping")
    assert direct.content == "just one tick"
    assert calls["n"] == 1

    # 2. Agent Loop —— 同样只调一次,但状态图包装后消息列表更长
    calls["n"] = 0
    loop_result = agent.invoke({"messages": [{"role": "user", "content": "ping"}]})
    msgs = loop_result["messages"]
    # 至少 HumanMessage + AIMessage
    types = [m.type for m in msgs]
    assert types[0] == "human"
    assert types[-1] == "ai"
    # 最终回答内容一致
    assert msgs[-1].content == "just one tick"
    # 底层模型同样只被调用一次
    assert calls["n"] == 1
