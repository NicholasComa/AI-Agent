"""Day 13 —— DevAssistantAgent（研发助手代理）的两个 Agent 中间件（Middleware，挂载在 Agent 运行过程上的拦截器）。

本模块与 :mod:`src.middleware`（Day 8 的 FastAPI（一个 Python Web 框架）中间件）**无关**；
这里是 LangChain v1（一个用于构建大模型应用的框架）的「函数：create_agent（创建代理）」所用的
「类：AgentMiddleware（代理中间件，LangChain 提供的、可挂载到 Agent 运行钩子点的基类）」子类，
作用于 Agent Loop（代理循环，模型与工具反复交互直到产出最终回答的过程）内部的钩子点（Hook，运行时可插入自定义逻辑的位置）：

* 方法：before_model（模型调用前钩子）—— 模型生成回答之前触发（可修改 state 状态，即 Agent 内部保存的全部消息与变量的容器）
* 方法：after_model（模型调用后钩子）—— 模型生成回答之后触发（可修改 state 状态）
* 方法：wrap_tool_call（包裹工具调用钩子）—— 在执行某个工具（Tool，Agent 可调用的一段功能）前后触发（可修改 request 请求，即本次要调用的工具名与参数；也可重试或吞掉异常）

设计目标
--------

* 类：TraceMiddleware（追踪中间件）—— 在三个钩子里把「模型为何调工具（assistant 消息中的 reason 文本，即模型决定调用工具时的说明）」「工具名 + 参数（args，调用工具时传入的参数字典）」
「工具结果（result，工具执行后的返回值）」「最终回答（final answer，模型不再调用工具、直接给用户的回答）」写入结构化 trace（追踪记录，append-only 只追加、不可修改历史的事件列表，
类型为 ``list[dict]``）。便于事后排查 + 测试断言（assertion，测试中判断结果是否符合预期的语句）。
* 类：SafeToolMiddleware（安全工具中间件）—— 在方法：wrap_tool_call 内部用 try/except（尝试 / 捕获异常的语法结构）包裹，工具抛异常（Exception，程序运行中的错误）时返回「类型：ToolMessage（工具消息，工具执行后回传给模型的标准消息对象）」
且**不**冒泡（bubble up，异常不被捕获而向上传播），配合 recursion_limit（递归上限，限制 Agent 循环最大步数、防止无限循环的参数）防止无限循环（终止条件 C + D 的兜底）。

为什么 SafeToolMiddleware 不直接由 TraceMiddleware 兼任
-------------------------------------------------------

责任分离（Separation of Concerns，把不同职责拆到不同模块上的设计原则）：TraceMiddleware 只读不写，需要 raise（抛出，主动中断当前流程并把错误交上层处理）让上层错误处理看见；
SafeToolMiddleware 只吞（swallow，捕获异常后不再向上传播）不抛，负责把异常转成可读字符串，让模型可以“看见失败”并决定下一步。两层叠加 = 既能记日志（log，运行记录），又能不崩溃。
"""

from __future__ import annotations

import json
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, ToolMessage

# ---------------------------------------------------------------------------
# 工具函数：把任意返回值序列化为 ToolMessage 友好的字符串
# ---------------------------------------------------------------------------


def _to_text(value: Any, *, max_len: int = 4000) -> str:
    """把工具结果（常见 dict 字典 / list 列表 / 字符串）转成可放进 ToolMessage.content 的 str 字符串。

    * str 字符串 → 原样返回（只截断过长内容，避免撑爆模型上下文 context，即模型一次能处理的文本量）
    * dict 字典 / list 列表 → 用 ``json.dumps(..., ensure_ascii=False)`` 序列化（转成 JSON 文本）
    * 其它 → 用 ``repr(value)`` 取可读表示
    """
    if isinstance(value, str):
        return value if len(value) <= max_len else value[:max_len] + "…(truncated 已截断)"
    if isinstance(value, (dict, list, tuple, int, float, bool)) or value is None:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(value)
        return text if len(text) <= max_len else text[:max_len] + "…(truncated 已截断)"
    return repr(value)[:max_len]


def _messages(state: Any) -> list[BaseMessage]:
    """统一 state 状态的取法 —— 兼容 dict 字典与 BaseMessage（消息基类）两种 AgentState（代理状态，保存全部消息的容器）实现。"""
    if isinstance(state, dict):
        return list(state.get("messages", []))
    return list(getattr(state, "messages", []))


# ---------------------------------------------------------------------------
# 类：TraceRecorder（追踪记录器，append-only 只追加的事件收集器）
# ---------------------------------------------------------------------------


class TraceRecorder:
    """Append-only（只追加、不可修改历史）的 trace 追踪记录器，供 TraceMiddleware 与测试共享。

    每条事件是一个 dict 字典，至少含 ``kind`` 字段（事件种类标签）。可用方法：of_type（按种类过滤）按 kind 过滤（便于测试断言）。

    Attributes:
        events: 事件列表（按追加顺序排列）。
    """

    __slots__ = ("events",)

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, *, kind: str, **fields: Any) -> None:
        """追加一条事件。``kind`` 必填（事件种类），其余字段按需传入。"""
        evt: dict[str, Any] = {"kind": kind, **fields}
        self.events.append(evt)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        """按 kind 事件种类过滤事件，返回匹配的事件列表。"""
        return [e for e in self.events if e.get("kind") == kind]

    def kinds(self) -> list[str]:
        """返回按顺序的事件种类列表（便于快速断言调用流，即验证钩子触发的先后顺序）。"""
        return [e.get("kind", "") for e in self.events]

    def __len__(self) -> int:
        return len(self.events)


# ---------------------------------------------------------------------------
# 类：TraceMiddleware（追踪中间件，记录 Agent Loop 关键节点）
# ---------------------------------------------------------------------------


class TraceMiddleware(AgentMiddleware):
    """在 Agent Loop（代理循环）的三个钩子里写 trace 追踪记录；不修改 state 状态，不吞（swallow）异常。

    事件类型（kind 字段取值）
    --------

    * ``before_model`` —— ``step``（第几次进入模型，自增计数器）+ ``message_count``（当前消息条数）
    * ``after_model``  —— ``step`` + ``tool_calls``（工具调用列表，每项含 name 工具名 + args 参数 + id 标识）+ ``has_final``（是否产生 final answer 最终回答）+ ``content_preview``（回答内容预览）
    * ``tool_call``    —— ``step`` + ``name`` 工具名 + ``args`` 参数 + ``ok`` 是否成功 + ``result_preview`` 结果预览 或 ``error`` 错误信息

    使用（usage）
    ----

    >>> rec = TraceRecorder()
    >>> agent = create_agent(model=..., middleware=[TraceMiddleware(rec)])
    >>> agent.invoke(...)
    >>> assert any(e["kind"] == "tool_call" for e in rec.events)  # 至少有一次工具调用事件

    测试隔离（isolation，避免相互干扰）
    --------

    每个测试应**自己 new 一个**（新建一个）类：TraceRecorder（追踪记录器），避免共享导致断言串扰（cross-talk，一个测试的事件混入另一个测试）。
    """

    def __init__(self, recorder: TraceRecorder | None = None) -> None:
        super().__init__()
        # 必须用 ``is not None`` 判空，而**不能**用 ``recorder or TraceRecorder()`` ——
        # 空的 TraceRecorder 因为 ``events == []``（空列表）在 bool 语境（布尔语境，即被当作 True/False 判断时）
        # 下为 False，会被后者误当成“未提供”而重新 new 一个新的实例，导致外部传入的 recorder 收不到事件（回归坑）。
        self.recorder: TraceRecorder = recorder if recorder is not None else TraceRecorder()
        self._step: int = 0

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """模型调用前钩子：递增 step 步数计数器，记录当前消息条数（便于诊断异常膨胀，即消息无限增长）。

        LangChain v1 运行时走 async（异步，不阻塞的并发执行模式），所以必须实现 ``a*`` 异步版本（方法名以 a 开头）；
        同步的 ``before_model`` 不会被调用 —— 这是一个常见的坑。
        """
        self._step += 1
        msgs = _messages(state)
        self.recorder.record(
            kind="before_model",
            step=self._step,
            message_count=len(msgs),
        )
        return None

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """模型调用后钩子：从最新的 AIMessage（AI 消息，模型产出的消息）中提取 tool_calls 工具调用 与最终回答预览。"""
        msgs = _messages(state)
        last_ai: BaseMessage | None = None
        for m in reversed(msgs):
            if getattr(m, "type", None) == "ai":
                last_ai = m
                break
        if last_ai is None:
            return None
        tool_calls = list(getattr(last_ai, "tool_calls", []) or [])
        self.recorder.record(
            kind="after_model",
            step=self._step,
            tool_calls=[
                {"name": tc.get("name"), "args": tc.get("args"), "id": tc.get("id")}
                for tc in tool_calls
            ],
            has_final=len(tool_calls) == 0,
            content_preview=_to_text(getattr(last_ai, "content", ""), max_len=200),
        )
        return None

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """包裹工具执行钩子：成功 → 记录 result_preview 结果预览；失败 → 记录 error 错误信息后 ``raise`` 抛出（不吞）。

        不吞（swallow）异常 —— 兜底由类：SafeToolMiddleware（安全工具中间件）负责（责任分离）。
        LangChain v1 运行时走 async 异步路径，所以这里是 ``awrap_tool_call``（异步版包裹工具调用）。
        """
        tc = request.tool_call
        name = tc.get("name")
        args = tc.get("args")
        try:
            result = await handler(request)
            # result 通常是类型：ToolMessage（工具消息）；也有可能是 Command（指令，LangChain 中用于向图返回状态更新的特殊对象）[Any]
            if isinstance(result, ToolMessage):
                preview = _to_text(result.content)
            else:
                preview = _to_text(getattr(result, "content", result))
            self.recorder.record(
                kind="tool_call",
                step=self._step,
                name=name,
                args=args,
                ok=True,
                result_preview=preview,
            )
            return result
        except Exception as exc:  # noqa: BLE001 —— 故意宽捕获（捕获所有异常类型），只是为了记录，随后重新抛出
            self.recorder.record(
                kind="tool_call",
                step=self._step,
                name=name,
                args=args,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise


# ---------------------------------------------------------------------------
# 类：SafeToolMiddleware（安全工具中间件，工具失败兜底）
# ---------------------------------------------------------------------------


class SafeToolMiddleware(AgentMiddleware):
    """工具抛异常（Exception，程序运行错误）时转成类型：ToolMessage（工具消息），内容形如 ``ToolMessage(content="TOOL_ERROR: <kind>: <msg>")``，
    **不**冒泡（bubble up，异常向上传播）—— 模型会“看见”失败并决定下一步（重试 / 改路径 / 终止）。

    配合函数：build_devassistant_agent（构造助手代理）中的 ``recursion_limit``（递归上限）防止异常循环（终止条件 C + D）。

    字符串格式说明
    --------------

    ``"TOOL_ERROR: <ExcType 异常类型>: <exc message 异常信息>"`` —— 模型据此知道失败原因；
    与 Day 12 工具自身的 ``{"ok": False, "error": ..., "kind": ...}`` 信封（envelope，把结果包成统一结构的返回格式）不同，
    这里用于**工具自身抛异常**（信封化失败是工具主动返回的结构化结果，不需要中间件介入）。
    """

    async def awrap_tool_call(self, request: Any, handler: Any) -> ToolMessage:
        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001 —— 故意宽捕获（捕获所有异常），转成 ToolMessage 而非向上抛
            tc = request.tool_call
            safe = f"TOOL_ERROR: {type(exc).__name__}: {exc}"
            return ToolMessage(content=safe, tool_call_id=tc.get("id"))


__all__ = [
    "TraceMiddleware",
    "SafeToolMiddleware",
    "TraceRecorder",
]
