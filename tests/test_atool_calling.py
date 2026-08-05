"""Day 14 — Week 3 Day 4: 20 条 Agent 层端到端用例（4 类覆盖）。

设计要点
--------

* 用 :class:`FakeToolCapableChatModel`（Day 12 引入的假模型）注入预设
  ``tool_calls``,不依赖真实 LLM,离线可跑。
* 复用 :func:`build_devassistant_agent` 工厂（Day 3）,与 day13 测试保持
  一致的调用方式(``agent.ainvoke(...)``)。
* 工具异常通过 :func:`monkeypatch.setattr` 注入,覆盖工具对象的 ``_run``
  方法(``BaseTool.invoke`` 内部走 ``self._run``,覆盖 ``_run`` 是拦截
  工具实际执行的稳定路径);
  :class:`SafeToolMiddleware` 自动把异常转成 ``TOOL_ERROR: ...`` 的
  ``ToolMessage``,实现"Agent 不崩溃、能继续或终止"。
"""

from __future__ import annotations

import sys

from langchain_core.messages import AIMessage, ToolCall
from langgraph.errors import GraphRecursionError

sys.path.insert(0, "src")
sys.path.insert(0, "tests")

import pytest

from devagent.dev_assistant_agent import build_devassistant_agent  # noqa: E402
from devagent.middleware import SafeToolMiddleware  # noqa: E402
from devagent.tools import (  # noqa: E402
    read_text_file,
)
from fake_models import FakeToolCapableChatModel  # noqa: E402

# ---------- 辅助 ----------


def _ai_with_tool_calls(name: str, args: dict, call_id: str) -> AIMessage:
    """构造一个预设 ``tool_call`` 的 ``AIMessage``。"""
    return AIMessage(content="", tool_calls=[ToolCall(name=name, args=args, id=call_id)])


def _ai_final(text: str) -> AIMessage:
    """构造一个最终回答的 ``AIMessage``(不调工具)。"""
    return AIMessage(content=text)


def _install_tool_failure(monkeypatch: pytest.MonkeyPatch, tool_obj, exc: BaseException) -> None:
    """把工具的 ``_run`` 替换为抛指定异常,触发 SafeToolMiddleware 兜底。

    LangChain ``BaseTool.invoke`` 内部调用 ``self._run(**input)``;
    覆盖 ``_run`` 是拦截工具实际执行的稳定路径。
    """

    def boom_run(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(tool_obj, "_run", boom_run)


# ---------- fixtures ----------


@pytest.fixture
def isolated_train_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """每个测试一个独立临时沙箱目录,测试后还原默认值。

    复用 day13 测试里的同款 fixture(见 ``tests/test_devagent_agent.py``)。
    """
    monkeypatch.setattr(sys.modules[read_text_file.__module__], "TRAIN_DIR", tmp_path)
    yield tmp_path


# ---------- 测试 1-6: 正确选工具 ----------


class TestCorrectToolSelection:
    """6 条: fake model 预设 ``tool_call``,Agent 正确执行工具并继续。"""

    async def test_case_01_calculator_complex_expression(self, isolated_train_dir):
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls("calculator", {"expression": "(12+8)*3"}, "c1"),
                _ai_final("(12+8)*3 = 60"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "计算 (12+8)*3"}]})
        types = [m.type for m in result["messages"]]
        assert types == ["human", "ai", "tool", "ai"]
        tool_msg = result["messages"][2]
        assert '"ok": true' in tool_msg.content
        assert '"value": 60' in tool_msg.content

    async def test_case_02_read_text_file(self, isolated_train_dir):
        (isolated_train_dir / "sample.txt").write_text("hello sample content", encoding="utf-8")
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls("read_text_file", {"path": "sample.txt"}, "c1"),
                _ai_final("已读到 sample.txt"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "读 sample.txt"}]})
        tool_msg = result["messages"][2]
        assert '"ok": true' in tool_msg.content
        assert "hello sample content" in tool_msg.content

    async def test_case_03_check_commit_message_valid(self):
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls(
                    "check_commit_message",
                    {"message": "feat: app: add login"},
                    "c1",
                ),
                _ai_final("这条 commit 合规"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "检查 commit"}]})
        tool_msg = result["messages"][2]
        assert '"valid": true' in tool_msg.content

    async def test_case_04_two_step_pipeline(self, isolated_train_dir):
        # 文件内容 "10 20 30",求和 = 60;验证两步工具都被调用
        (isolated_train_dir / "nums.txt").write_text("10 20 30", encoding="utf-8")
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls("read_text_file", {"path": "nums.txt"}, "c1"),
                _ai_with_tool_calls("calculator", {"expression": "10+20+30"}, "c2"),
                _ai_final("和是 60"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "先读文件再算"}]})
        types = [m.type for m in result["messages"]]
        assert types == ["human", "ai", "tool", "ai", "tool", "ai"]
        tool_messages = [m for m in result["messages"] if m.type == "tool"]
        assert len(tool_messages) == 2

    async def test_case_05_calculator_power(self):
        # calculator 不支持 ``**``(``ast.Pow`` 节点不在白名单),
        # 改用十个 2 相乘 = 1024,效果上等价于 2^10
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls(
                    "calculator",
                    {"expression": "2*2*2*2*2*2*2*2*2*2"},
                    "c1",
                ),
                _ai_final("1024"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "算 2 的 10 次方"}]})
        tool_msg = result["messages"][2]
        assert '"value": 1024' in tool_msg.content

    async def test_case_06_check_commit_message_fix(self):
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls(
                    "check_commit_message",
                    {"message": "fix: api: handle 500"},
                    "c1",
                ),
                _ai_final("这条 commit 合规"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "检查 commit"}]})
        tool_msg = result["messages"][2]
        assert '"valid": true' in tool_msg.content


# ---------- 测试 7-10: 无需工具 ----------


class TestNoToolNeeded:
    """4 条: 模型直接给最终回答,Agent 不应调任何工具。"""

    async def test_case_07_greeting(self):
        model = FakeToolCapableChatModel(responses=[_ai_final("你好!有什么可以帮你的吗?")])
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "你好"}]})
        types = [m.type for m in result["messages"]]
        assert types == ["human", "ai"]
        assert "你好" in result["messages"][-1].content

    async def test_case_08_concept_question(self):
        model = FakeToolCapableChatModel(
            responses=[_ai_final("Agent Loop 是模型与工具反复交互直到产出最终回答的过程。")]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "什么是 Agent Loop"}]}
        )
        assert "Agent Loop" in result["messages"][-1].content
        assert all(m.type != "tool" for m in result["messages"])

    async def test_case_09_no_such_capability(self):
        model = FakeToolCapableChatModel(
            responses=[_ai_final("抱歉,我没有联网查天气的工具,建议用其他方式查询。")]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "今天天气怎么样"}]})
        types = [m.type for m in result["messages"]]
        assert types == ["human", "ai"]
        last = result["messages"][-1].content
        assert "抱歉" in last or "无法" in last or "天气" in last

    async def test_case_10_tell_joke(self):
        model = FakeToolCapableChatModel(
            responses=[_ai_final("为什么程序员总穿黑衣服?因为没有 bug 可以 debug。")]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "讲个笑话"}]})
        types = [m.type for m in result["messages"]]
        assert types == ["human", "ai"]
        assert "bug" in result["messages"][-1].content or "debug" in result["messages"][-1].content


# ---------- 测试 11-15: 错误参数 ----------


class TestInvalidArguments:
    """5 条: 模型调工具但参数非法,Agent 收到工具的结构化错误并继续。"""

    async def test_case_11_calculator_unsafe_node(self):
        # ``__import__('os')`` 经 AST 解析为 ``Call`` 节点,不在白名单
        # → 工具返回 ``{"ok": false, "kind": "unsafe_node"}`` 结构化错误
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls(
                    "calculator",
                    {"expression": "__import__('os')"},
                    "c1",
                ),
                _ai_final("已拒绝不安全表达式"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "算这个"}]})
        tool_msg = result["messages"][2]
        assert '"ok": false' in tool_msg.content
        assert "unsafe_node" in tool_msg.content

    async def test_case_12_calculator_non_string(self):
        # ``expression`` 传 ``int`` → LangChain v1 在 ``BaseTool.invoke``
        # 阶段捕获 Pydantic ``ValidationError`` 后,把 ToolMessage ``status``
        # 置为 ``"error"``,content 含 "Input should be a valid string" +
        # "Please fix the error and try again."
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls("calculator", {"expression": 123}, "c1"),
                _ai_final("已拒绝"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "算"}]})
        tool_msg = result["messages"][2]
        assert getattr(tool_msg, "status", None) == "error"
        assert "Input should be a valid string" in tool_msg.content
        assert "Please fix the error and try again" in tool_msg.content

    async def test_case_13_read_text_file_empty_path(self, isolated_train_dir):
        # 空字符串相对路径经 ``_safe_resolve`` 解析为沙箱根目录本身
        # → ``is_file()`` 为 ``False`` → 返回 ``{"kind": "is_dir"}``
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls("read_text_file", {"path": ""}, "c1"),
                _ai_final("已拒绝"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "读"}]})
        tool_msg = result["messages"][2]
        assert '"ok": false' in tool_msg.content
        assert "is_dir" in tool_msg.content

    async def test_case_14_read_text_file_forbidden_path(self, isolated_train_dir):
        # ``../../etc/passwd`` 越过 ``TRAIN_DIR`` → ``forbidden``
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls(
                    "read_text_file",
                    {"path": "../../etc/passwd"},
                    "c1",
                ),
                _ai_final("已拒绝越界访问"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "读"}]})
        tool_msg = result["messages"][2]
        assert '"ok": false' in tool_msg.content
        assert "forbidden" in tool_msg.content

    async def test_case_15_check_commit_message_empty(self):
        # 空字符串 → header 解析失败 → ``valid=False``、``errors`` 非空
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls("check_commit_message", {"message": ""}, "c1"),
                _ai_final("不合规"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "检查"}]})
        tool_msg = result["messages"][2]
        assert '"valid": false' in tool_msg.content
        assert "errors" in tool_msg.content


# ---------- 测试 16-20: 工具失败 ----------


class TestToolFailures:
    """5 条: 工具执行异常或返回失败,Agent 不崩溃、能继续或终止。"""

    async def test_case_16_read_text_file_not_found(self, isolated_train_dir):
        # 文件不存在 → 工具返回 ``{"kind": "not_found"}`` 结构化错误
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls(
                    "read_text_file",
                    {"path": "no_such_file.txt"},
                    "c1",
                ),
                _ai_final("文件不存在"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "读"}]})
        tool_msg = result["messages"][2]
        assert '"ok": false' in tool_msg.content
        assert "not_found" in tool_msg.content

    async def test_case_17_calculator_division_by_zero(self):
        # ``1/0`` → 工具内部 ``except ZeroDivisionError`` → ``{"kind": "domain"}``
        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls("calculator", {"expression": "1/0"}, "c1"),
                _ai_final("数学错误"),
            ]
        )
        agent = build_devassistant_agent(model=model)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "算 1/0"}]})
        tool_msg = result["messages"][2]
        assert '"ok": false' in tool_msg.content
        assert '"domain"' in tool_msg.content
        assert "division" in tool_msg.content or "zero" in tool_msg.content

    async def test_case_18_check_commit_message_raises_exception(self):
        # 工具抛 ``ValueError`` → SafeToolMiddleware 兜底为 ``TOOL_ERROR``。
        # 注意:此处**不**用 ``build_devassistant_agent``(它绑定的是
        # ``devagent.tools.check_commit_message`` 裸函数,无法注入异常),
        # 改用 ``create_agent`` + 自定义 ``@tool`` boom 函数 + SafeToolMiddleware,
        # 与 day13 ``TestSafeToolMiddleware`` 同款。
        from langchain.agents import create_agent
        from langchain.tools import tool

        @tool
        def boom_check(message: str) -> str:
            """Always raises (用于验证 SafeToolMiddleware 兜底)."""
            raise ValueError("synthetic explosion")

        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls("boom_check", {"message": "feat: app: x"}, "c1"),
                _ai_final("已看到错误"),
            ]
        )
        agent = create_agent(model=model, tools=[boom_check], middleware=[SafeToolMiddleware()])
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "检查"}]})
        tool_msg = result["messages"][2]
        assert tool_msg.content.startswith("TOOL_ERROR:")
        assert "ValueError" in tool_msg.content

    async def test_case_19_tool_timeout_simulated(self):
        # 工具抛 ``TimeoutError`` → SafeToolMiddleware 兜底为 ``TOOL_ERROR``
        from langchain.agents import create_agent
        from langchain.tools import tool

        @tool
        def boom_calc(expression: str) -> str:
            """Always times out (用于模拟工具超时)."""
            raise TimeoutError("simulated tool timeout")

        model = FakeToolCapableChatModel(
            responses=[
                _ai_with_tool_calls("boom_calc", {"expression": "1+1"}, "c1"),
                _ai_final("已超时"),
            ]
        )
        agent = create_agent(model=model, tools=[boom_calc], middleware=[SafeToolMiddleware()])
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "算"}]})
        tool_msg = result["messages"][2]
        assert tool_msg.content.startswith("TOOL_ERROR:")
        assert "TimeoutError" in tool_msg.content

    async def test_case_20_tool_persistent_failure_stops_at_recursion_limit(self):
        # 工具持续抛异常 + 模型持续返回 ``tool_call`` → SafeToolMiddleware
        # 每次兜底为 ``TOOL_ERROR``,但模型仍持续调工具 → 超过
        # ``recursion_limit=4`` 后 LangGraph 抛 ``GraphRecursionError`` 终止
        from langchain.agents import create_agent
        from langchain.tools import tool

        @tool
        def boom_calc(expression: str) -> str:
            """Always raises (用于验证持续异常 + recursion_limit 截断)."""
            raise RuntimeError("persistent failure")

        responses = [
            _ai_with_tool_calls("boom_calc", {"expression": "1"}, f"c{i}") for i in range(20)
        ]
        model = FakeToolCapableChatModel(responses=responses)
        agent = create_agent(model=model, tools=[boom_calc], middleware=[SafeToolMiddleware()])
        with pytest.raises(GraphRecursionError):
            await agent.ainvoke(
                {"messages": [{"role": "user", "content": "go"}]},
                config={"recursion_limit": 4},
            )
