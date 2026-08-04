"""Day 13 —— DevAssistantAgent（研发助手代理）& Agent 中间件（Middleware，挂载在 Agent 运行过程上的拦截器）测试。

覆盖目标
========

1. **类：TraceRecorder（追踪记录器）** —— 单元测试（append 追加 / filter 过滤 / 序列 顺序）。
2. **类：TraceMiddleware（追踪中间件）** —— 三钩子（hook，运行中可插入自定义逻辑的位置）都被调用、记录关键字段。
3. **类：SafeToolMiddleware（安全工具中间件）** —— 工具抛异常 → 类型：ToolMessage（工具消息）且**不**冒泡（bubble up，异常向上传播）（终止条件 C 的兜底）。
4. **Agent Loop（代理循环）终止条件 D** —— ``recursion_limit``（递归上限，限制循环最大步数）截断无限工具循环，抛「类型：GraphRecursionError（图递归错误，当循环超过递归上限时由 LangGraph 抛出）」（LangGraph 编排库的标准行为）。
5. **函数：build_devassistant_agent（构造助手代理）** —— 烟雾测试 smoke test（最小可用验证）+ AppConfig 注入 + 完整闭环（端到端跑通工具调用到最终回答）。

测试结构说明
============

* 全部用 ``FakeToolCapableChatModel``（假模型，Day 12 引入的测试替身，模拟真实模型但不联网），支持「方法：bind_tools（绑定工具，让模型知道有哪些工具可用）」，
  按序返回「类型：AIMessage（AI 消息，模型产出的消息）」（含 ``tool_calls`` 工具调用列表）。
* ``asyncio_mode = "auto"``（异步模式，见 :file:`pyproject.toml` 配置），所有 async（异步，不阻塞的并发执行）测试无需 ``@pytest.mark.asyncio`` 装饰器。
* LangChain v1 运行时走 async 异步路径，所以调用「方法：agent.ainvoke（异步调用，不阻塞地执行 Agent）」；
  同步的「方法：agent.invoke（同步调用）」在挂着 ``abefore_model`` / ``awrap_tool_call`` 等异步中间件时会失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 把 ``src`` 目录加入 Python 搜索路径，让 ``from devagent.tools import ...`` 在 pytest 下也能工作
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from devagent.dev_assistant_agent import (  # noqa: E402
    DEFAULT_RECURSION_LIMIT,
    SYSTEM_PROMPT,
    build_devassistant_agent,
)
from devagent.middleware import (  # noqa: E402
    SafeToolMiddleware,
    TraceMiddleware,
    TraceRecorder,
)
from devagent.tools import (  # noqa: E402
    read_text_file,
)
from fake_models import FakeToolCapableChatModel  # noqa: E402

# ---------------------------------------------------------------------------
# 通用 fixture（测试夹具，pytest 在每个测试前自动准备的可复用资源）:
# 构造一个临时训练目录（tmp_path，pytest 提供的临时目录），避免与 Day 12 测试相互污染
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_train_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """通过「方法：read_text_file.configure（配置函数）」把沙箱 sandbox（受限运行环境）根目录指向 tmp_path 临时目录。"""
    rtf_mod = sys.modules[read_text_file.__module__]
    rtf_mod.configure(train_dir=tmp_path, max_file_bytes=50_000)
    yield tmp_path
    # 测试结束后还原默认值（避免后续测试受污染）
    rtf_mod.configure(train_dir=Path("./training_data").resolve(), max_file_bytes=50_000)


# ===========================================================================
# 1) 类：TraceRecorder（追踪记录器）—— 单元
# ===========================================================================


class TestTraceRecorder:
    def test_record_appends_event(self) -> None:
        rec = TraceRecorder()
        rec.record(kind="x", foo=1)
        rec.record(kind="y", bar=2)
        assert len(rec) == 2
        assert rec.events[0] == {"kind": "x", "foo": 1}
        assert rec.events[1] == {"kind": "y", "bar": 2}

    def test_of_type_filters_by_kind(self) -> None:
        rec = TraceRecorder()
        rec.record(kind="before_model")
        rec.record(kind="after_model")
        rec.record(kind="before_model")
        bm = rec.of_type("before_model")  # 按事件种类 kind 过滤
        assert len(bm) == 2
        assert all(e["kind"] == "before_model" for e in bm)

    def test_kinds_returns_sequence(self) -> None:
        rec = TraceRecorder()
        rec.record(kind="before_model")
        rec.record(kind="after_model")
        rec.record(kind="tool_call")
        # kinds() 返回按顺序的事件种类列表，验证钩子触发顺序
        assert rec.kinds() == ["before_model", "after_model", "tool_call"]

    def test_empty_recorder_has_zero_events(self) -> None:
        rec = TraceRecorder()
        assert len(rec) == 0
        assert rec.kinds() == []
        assert rec.of_type("anything") == []


# ===========================================================================
# 2) 类：TraceMiddleware（追踪中间件）—— 三钩子都能记录
# ===========================================================================


class TestTraceMiddleware:
    async def test_before_model_records_step_and_message_count(self) -> None:
        """第一次进模型（方法：abefore_model 模型调用前钩子）→ step=1, message_count=1（只有 user 用户消息）。"""
        from langchain_core.messages import AIMessage

        rec = TraceRecorder()
        model = FakeToolCapableChatModel(responses=[AIMessage(content="hi")])
        from langchain.agents import create_agent
        from langchain.tools import tool

        @tool
        def noop() -> str:
            """noop（无操作，测试用空工具）"""
            return "ok"

        agent = create_agent(model=model, tools=[noop], middleware=[TraceMiddleware(recorder=rec)])
        await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]})

        bm_events = rec.of_type("before_model")
        assert len(bm_events) == 1
        assert bm_events[0]["step"] == 1
        # 用户输入 1 条消息（human 消息）
        assert bm_events[0]["message_count"] == 1

    async def test_after_model_records_tool_calls_and_has_final_false(self) -> None:
        """模型返回 tool_calls 工具调用 → after_model 记录工具调用列表, has_final=False（仍有后续步骤）。"""
        from langchain.agents import create_agent
        from langchain.tools import tool
        from langchain_core.messages import AIMessage, ToolCall

        @tool
        def noop() -> str:
            """noop（无操作，测试用空工具）"""
            return "ok"

        rec = TraceRecorder()
        model = FakeToolCapableChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[ToolCall(name="noop", args={}, id="c1")],
                ),
                AIMessage(content="done"),
            ]
        )
        agent = create_agent(model=model, tools=[noop], middleware=[TraceMiddleware(recorder=rec)])
        await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]})

        am_events = rec.of_type("after_model")
        assert len(am_events) == 2
        # 第 1 次 after_model:有 tool_calls 工具调用, has_final=False
        assert am_events[0]["tool_calls"] == [{"name": "noop", "args": {}, "id": "c1"}]
        assert am_events[0]["has_final"] is False
        # 第 2 次 after_model:无 tool_calls 工具调用（已不再调工具）, has_final=True（已是最终回答）
        assert am_events[1]["tool_calls"] == []
        assert am_events[1]["has_final"] is True

    async def test_wrap_tool_call_records_success(self) -> None:
        """工具成功 → tool_call 事件 ok=True, result_preview 结果预览有内容。"""
        from langchain.agents import create_agent
        from langchain.tools import tool
        from langchain_core.messages import AIMessage, ToolCall

        @tool
        def noop() -> str:
            """noop（无操作，测试用空工具）"""
            return "ok-result"

        rec = TraceRecorder()
        model = FakeToolCapableChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[ToolCall(name="noop", args={}, id="c1")],
                ),
                AIMessage(content="done"),
            ]
        )
        agent = create_agent(model=model, tools=[noop], middleware=[TraceMiddleware(recorder=rec)])
        await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]})

        tool_events = rec.of_type("tool_call")
        assert len(tool_events) == 1
        assert tool_events[0]["ok"] is True
        assert tool_events[0]["name"] == "noop"
        assert "ok-result" in tool_events[0]["result_preview"]

    async def test_wrap_tool_call_records_error_and_reraises(self) -> None:
        """工具抛异常 → tool_call 事件 ok=False,然后 ``raise`` 抛出（不吞，让上层看见）。"""
        from langchain.agents import create_agent
        from langchain.tools import tool
        from langchain_core.messages import AIMessage, ToolCall

        @tool
        def boom() -> str:
            """boom（测试用，必然抛异常的工具）"""
            raise RuntimeError("synthetic explosion 合成爆炸错误")

        rec = TraceRecorder()
        model = FakeToolCapableChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[ToolCall(name="boom", args={}, id="c1")],
                ),
            ]
        )
        # 仅挂 TraceMiddleware（追踪中间件）—— SafeToolMiddleware（安全工具中间件）不在,异常应冒泡（向上传播）
        agent = create_agent(
            model=model,
            tools=[boom],
            middleware=[TraceMiddleware(recorder=rec)],
        )
        with pytest.raises(RuntimeError, match="synthetic explosion"):
            await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]})

        tool_events = rec.of_type("tool_call")
        assert len(tool_events) == 1
        assert tool_events[0]["ok"] is False
        assert "RuntimeError" in tool_events[0]["error"]
        assert "synthetic explosion" in tool_events[0]["error"]


# ===========================================================================
# 3) 类：SafeToolMiddleware（安全工具中间件）—— 终止条件 C（异常兜底）
# ===========================================================================


class TestSafeToolMiddleware:
    async def test_tool_exception_becomes_tool_error_message(self) -> None:
        """工具抛异常 → SafeToolMiddleware 转成类型：ToolMessage（工具消息），内容以 'TOOL_ERROR: ...' 开头。"""
        from langchain.agents import create_agent
        from langchain.tools import tool
        from langchain_core.messages import AIMessage, ToolCall

        @tool
        def boom() -> str:
            """boom（测试用，必然抛异常的工具）"""
            raise ValueError("kaboom 砰")

        model = FakeToolCapableChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[ToolCall(name="boom", args={}, id="c1")],
                ),
                AIMessage(content="recovered 已恢复"),
            ]
        )
        agent = create_agent(
            model=model,
            tools=[boom],
            middleware=[SafeToolMiddleware()],
        )
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]})
        msgs = result["messages"]
        types = [m.type for m in msgs]
        # human 用户消息 → ai（含 tool_call 工具调用）→ tool（TOOL_ERROR 工具错误）→ ai（final 最终回答）
        assert types == ["human", "ai", "tool", "ai"]
        # tool 工具消息必须以 TOOL_ERROR 起头
        tool_msg = msgs[2]
        assert tool_msg.content.startswith("TOOL_ERROR:")
        assert "ValueError" in tool_msg.content
        assert "kaboom" in tool_msg.content
        # agent 不崩溃,继续给 final answer 最终回答
        assert msgs[-1].content == "recovered 已恢复"

    async def test_safe_tool_preserves_tool_call_id(self) -> None:
        """ToolMessage 的 tool_call_id（工具调用标识，用于把结果与对应调用关联）必须等于触发它的 ToolCall（工具调用请求，模型要求调用某工具时生成的数据结构）.id（回填链路依赖）。"""
        from langchain.agents import create_agent
        from langchain.tools import tool
        from langchain_core.messages import AIMessage, ToolCall

        @tool
        def boom() -> str:
            """boom（测试用，必然抛异常的工具）"""
            raise RuntimeError("x")

        model = FakeToolCapableChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[ToolCall(name="boom", args={}, id="unique-call-id-007")],
                ),
                AIMessage(content="done"),
            ]
        )
        agent = create_agent(model=model, tools=[boom], middleware=[SafeToolMiddleware()])
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]})
        tool_msg = result["messages"][2]
        # tool_call_id 必须回填空工具的原始 id，模型才能把结果对上这次调用
        assert tool_msg.tool_call_id == "unique-call-id-007"

    async def test_safe_tool_not_swallow_normal_results(self) -> None:
        """SafeToolMiddleware 对正常返回**不**改写,只兜（兜底处理）异常。"""
        from langchain.agents import create_agent
        from langchain.tools import tool
        from langchain_core.messages import AIMessage, ToolCall

        @tool
        def echo(text: str) -> str:
            """echo（回显工具，原样返回输入）"""
            return f"got:{text}"

        model = FakeToolCapableChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[ToolCall(name="echo", args={"text": "hi"}, id="c1")],
                ),
                AIMessage(content="done"),
            ]
        )
        agent = create_agent(model=model, tools=[echo], middleware=[SafeToolMiddleware()])
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]})
        tool_msg = result["messages"][2]
        assert tool_msg.content == "got:hi"


# ===========================================================================
# 4) Agent Loop（代理循环）终止条件 D —— recursion_limit（递归上限）截断
# ===========================================================================


class TestAgentLoopScenarioD:
    async def test_recursion_limit_stops_infinite_tool_calls(self) -> None:
        """模型永远只返回 tool_calls 工具调用 → ``recursion_limit`` 触发 LangGraph（状态图编排库）截断。

        期望行为:抛「类型：GraphRecursionError（图递归错误）」(LangGraph 标准行为),
        不死循环（infinite loop，永不停止的循环）。
        """
        from langchain.tools import tool
        from langchain_core.messages import AIMessage, ToolCall
        from langgraph.errors import GraphRecursionError

        @tool
        def noop() -> str:
            """noop（无操作，测试用空工具）"""
            return "ok"

        # 给 20 次响应（永不给 final answer 最终回答），期望在 recursion_limit=4 时被截断
        responses = [
            AIMessage(
                content="",
                tool_calls=[ToolCall(name="noop", args={}, id=f"c{i}")],
            )
            for i in range(20)
        ]
        model = FakeToolCapableChatModel(responses=responses)
        from langchain.agents import create_agent

        agent = create_agent(
            model=model,
            tools=[noop],
            middleware=[SafeToolMiddleware()],
        )
        with pytest.raises(GraphRecursionError, match=r"Recursion limit of 4"):
            await agent.ainvoke(
                {"messages": [{"role": "user", "content": "go"}]},
                config={"recursion_limit": 4},
            )

    async def test_default_recursion_limit_constant_is_eight(self) -> None:
        """``DEFAULT_RECURSION_LIMIT = 8``（默认递归上限为 8）—— Week 3 plan（第三周计划）推荐 8-12。"""
        assert DEFAULT_RECURSION_LIMIT == 8


# ===========================================================================
# 5) 函数：build_devassistant_agent（构造助手代理）—— 工厂烟雾
# ===========================================================================


class TestBuildDevAssistantAgent:
    async def test_returns_invokeable_agent(self) -> None:
        """工厂返回一个 LangGraph「类型：CompiledStateGraph（编译状态图）」，有 ainvoke 异步调用 / invoke 同步调用 方法。"""
        from langchain_core.messages import AIMessage

        model = FakeToolCapableChatModel(responses=[AIMessage(content="hi")])
        agent = build_devassistant_agent(model=model)
        assert hasattr(agent, "invoke")  # 是否具备同步调用方法
        assert hasattr(agent, "ainvoke")  # 是否具备异步调用方法
        # smoke 烟雾测试（最小可用验证）
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "hi"}]})
        assert result["messages"][-1].content == "hi"

    async def test_includes_all_three_tools(self) -> None:
        """系统挂载 calculator 计算器 / read_text_file 读文本文件 / check_commit_message 校验提交信息 三个工具。"""
        from langchain_core.messages import AIMessage

        model = FakeToolCapableChatModel(responses=[AIMessage(content="ok")])
        agent = build_devassistant_agent(model=model)
        # 工具列表位于 ``agent.nodes['tools'].bound.tools_by_name``（CompiledStateGraph 内部包装：
        # ``nodes['tools']`` 是「类型：PregelNode（Pregel 节点，LangGraph 内部的图节点类型）」，
        # 其 ``bound`` 属性才是「类型：ToolNode（工具节点，实际执行工具的节点）」）
        tool_names: set[str] = set(agent.nodes["tools"].bound.tools_by_name.keys())
        assert {"calculator", "read_text_file", "check_commit_message"} == tool_names

    async def test_system_prompt_describes_constraints(self) -> None:
        """system_prompt 系统提示词 必须含工具名 + "不要凭空回答" 之类的约束字符串。"""
        # 验证 SYSTEM_PROMPT 模块常量本身（CompiledStateGraph 编译状态图不直接暴露 system_prompt 字段）
        assert "calculator" in SYSTEM_PROMPT
        assert "read_text_file" in SYSTEM_PROMPT
        assert "check_commit_message" in SYSTEM_PROMPT

    async def test_appconfig_injects_train_dir_into_read_text_file(
        self, isolated_train_dir: Path
    ) -> None:
        """``config=AppConfig（应用配置，train_dir=...）`` → read_text_file 沙箱 sandbox 根目录跟随更新。"""
        from langchain_core.messages import AIMessage, ToolCall

        from config import AppConfig

        # 在沙箱根目录写一个文件
        (isolated_train_dir / "hi.txt").write_text("hello-sandbox 你好沙箱", encoding="utf-8")

        cfg = AppConfig(
            model_name="fake",
            api_base_url="http://fake",
            train_dir=str(isolated_train_dir),
            max_file_bytes=50_000,
        )
        model = FakeToolCapableChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            name="read_text_file",
                            args={"path": "hi.txt"},
                            id="c1",
                        )
                    ],
                ),
                AIMessage(content="read done 读完了"),
            ]
        )
        agent = build_devassistant_agent(model=model, config=cfg)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "read hi.txt"}]})
        tool_msg = result["messages"][2]
        assert tool_msg.content.startswith('{"ok": true')  # 结构化信封 envelope 成功标记
        assert "hello-sandbox" in tool_msg.content

    async def test_full_loop_with_trace(self) -> None:
        """calculator 计算器 → 结果 → final answer 最终回答 全闭环，trace 追踪记录 至少含 5 事件（2× before/after + tool）。"""
        from langchain_core.messages import AIMessage, ToolCall

        rec = TraceRecorder()
        model = FakeToolCapableChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            name="calculator",
                            args={"expression": "2 + 3"},
                            id="c1",
                        )
                    ],
                ),
                AIMessage(content="5"),
            ]
        )
        agent = build_devassistant_agent(model=model, recorder=rec)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "算 2+3"}]})
        types = [m.type for m in result["messages"]]
        assert types == ["human", "ai", "tool", "ai"]
        # final 最终回答必须是纯回答,无 tool_calls 工具调用
        assert result["messages"][-1].tool_calls == []
        assert result["messages"][-1].content == "5"
        # trace 应至少 5 事件
        assert len(rec) >= 5
        # 事件顺序: before_model → after_model → tool_call → before_model → after_model
        assert rec.kinds()[:3] == ["before_model", "after_model", "tool_call"]
        assert rec.kinds()[3:] == ["before_model", "after_model"]
        # tool_call 事件本身要看到 calculator 计算器 + 5
        tool_events = rec.of_type("tool_call")
        assert len(tool_events) == 1
        assert tool_events[0]["name"] == "calculator"
        assert "5" in tool_events[0]["result_preview"]

    async def test_no_recorder_works_fine(self) -> None:
        """不传 recorder 追踪记录器 → agent 正常工作,只是不记录 trace 追踪记录。"""
        from langchain_core.messages import AIMessage

        model = FakeToolCapableChatModel(responses=[AIMessage(content="ok")])
        agent = build_devassistant_agent(model=model)  # 无 recorder 追踪记录器
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]})
        assert result["messages"][-1].content == "ok"


# ===========================================================================
# 6) 单元级辅助:类：TraceRecorder（追踪记录器）共享性回归（regression，防止已知 bug 复发）
# ===========================================================================


class TestRecorderIdentity:
    """确保「类：TraceMiddleware（追踪中间件）」正确共享「类：TraceRecorder（追踪记录器）」（回归测试）。

    Day 13 实现里踩过一个坑:``TraceRecorder.__bool__`` 因为 ``events == []``（空列表）
    在布尔语境（被当作 True/False 判断时）返回 False，被 ``recorder or TraceRecorder()`` 当成“未提供”重新构造，
    导致外部 recorder 永远收不到事件。修复:用 ``is not None`` 判空。
    """

    def test_trace_middleware_preserves_passed_recorder(self) -> None:
        rec = TraceRecorder()
        mw = TraceMiddleware(recorder=rec)
        assert mw.recorder is rec  # 必须是同一实例,不是新建的替身

    def test_trace_middleware_default_creates_new_recorder(self) -> None:
        mw = TraceMiddleware()
        assert isinstance(mw.recorder, TraceRecorder)
        # 默认实例 != 任何外部实例（确认它是新创建的）
        assert mw.recorder is not TraceRecorder()
