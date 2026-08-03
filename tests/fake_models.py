"""测试用的假模型 / 假 LLM 工具。

Day 12 引入
----------

* :class:`FakeToolCapableChatModel` — LangChain v1 测试替身,实现
  :meth:`bind_tools`,支持按序返回 ``str`` / ``AIMessage``(含 ``tool_calls``)。

为什么不在 conftest.py 里
-------------------------

``tests/`` 不是一个 Python 包,``from tests.conftest import ...`` 在没有
``__init__.py`` 时会失败。conftest.py 适合放 fixture,不适合放被 ``import``
的类。把这类「被测试引用的测试替身」放到独立模块更稳。
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool


class FakeToolCapableChatModel(FakeMessagesListChatModel):
    """支持 ``bind_tools`` 的可序列响应假模型。

    以 :class:`FakeMessagesListChatModel` 为基类,``responses`` 接受
    ``list[BaseMessage]``(包括带 ``tool_calls`` 的 ``AIMessage``)。
    与父类不同:**不在末尾循环** —— 调用次数耗尽时抛 :class:`RuntimeError`,
    避免「第三次拿到 tool_call 响应,但 tool_message 已存在」把 agent loop
    推进到一个奇怪的「artificial tool messages」分支。

    本类只额外实现 :meth:`bind_tools` 以满足 :func:`langchain.agents.create_agent`
    对模型层的探测。

    Note:
        **不**接受字符串输入 —— 需要字符串测试请用内置
        :class:`FakeListChatModel`。这种区分是有意为之,避免「假装调工具」
        的测试用例不小心把字符串塞进 ``responses``(那会被 Pydantic 静默
        接受,但不会触发预期的工具调用链路)。
    """

    def _generate(  # type: ignore[override]
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.i >= len(self.responses):
            msg = (
                f"FakeToolCapableChatModel exhausted (called {self.i} times, "
                f"have {len(self.responses)} responses); add more responses "
                f"or check the test for unintended extra model calls."
            )
            raise RuntimeError(msg)
        response = self.responses[self.i]
        self.i += 1
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(  # type: ignore[no-untyped-def, override]
        self,
        tools: list[BaseTool | dict[str, Any] | type | Any],
        **kwargs: Any,
    ) -> FakeToolCapableChatModel:
        """记录已绑定的工具名后返回 self(共享 ``i`` 计数器)。

        不要返回 clone —— :func:`langchain.agents.create_agent` 内部可能
        多次调用 ``bind_tools``/直接 invoke,如果每次都得到新 clone,
        每个 clone 都从 ``i=0`` 开始,响应序列被"重置",模型永远拿不到
        resp[1](final answer)。
        """
        self._bound_tool_names = [  # type: ignore[attr-defined]
            getattr(t, "name", str(t)) for t in tools
        ]
        return self


__all__ = ["FakeListChatModel", "FakeToolCapableChatModel"]
