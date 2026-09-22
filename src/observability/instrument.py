"""埋点包装器：把既有的对话函数、检索器与工具调用包成会产出 span 的版本。

三个工厂都遵循同一套写法：**返回一个与被包对象接口一致的替代品**，而不是
让调用方显式地「进 span、出 span」。理由是这些对象会作为依赖注入到领域层
（生成器、图节点、工具会话），如果埋点要求调用方配合，领域层就得反过来知道
追踪层的存在——那正好是本层要避免的耦合。

包装器全部实现 ``__getattr__`` 透传，避免包装层退化成一个窄接口：被包对象
上那些与埋点无关的方法（``count`` / ``index`` / 属性）必须原样可见，否则
「加了一层埋点」会连带改变功能可用性。

追踪是旁路能力：本模块写入记录的所有动作都不改变被包对象的返回值与异常，
异常一律原样抛出，只在 span 上留痕。
"""

from __future__ import annotations

import logging
from typing import Any

from .models import content_payload, make_usage
from .tracer import Tracer

logger = logging.getLogger(__name__)

QUERY_CONTENT_LIMIT = 2000
"""检索查询落盘时的截断长度；只有开启内容捕获才会真正写到这个长度。"""


class TracedChat:
    """包住异步对话函数，每次调用产出一条 ``generation`` span。

    Attributes:
        model: 写进 span 的模型名；为 ``None`` 时该字段留空。
        attempt: 重试序号。对话函数在重试链里会被反复调用，这个值让「第几次
            才成功」在 trace 上直接可见。
    """

    def __init__(
        self,
        chat_fn: Any,
        tracer: Tracer,
        *,
        model: str | None = None,
        attempt: int = 1,
    ) -> None:
        """初始化。

        Args:
            chat_fn: 异步对话函数，签名 ``(messages) -> str``。
            tracer: 追踪门面。
            model: 模型名，写入 span 的 ``model`` 字段。
            attempt: 首次尝试的序号，供重试链区分同一次请求的多次调用。
        """
        self._chat_fn = chat_fn
        self._tracer = tracer
        self._model = model
        self._attempt = attempt

    async def __call__(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """调用被包函数并记录耗时、模型与用法；异常照常抛出。

        Args:
            messages: 对话消息列表。
            **kwargs: 透传给被包函数的额外参数。

        Returns:
            被包函数的返回值。
        """
        with self._tracer.span(
            "generate",
            "generation",
            message_count=len(messages),
            attempt=self._attempt,
        ) as record:
            if self._model:
                record.model = self._model
            usage = _usage_of(self._chat_fn)
            if usage:
                record.usage = usage
                self._tracer.set_usage(
                    model=self._model,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                )
            try:
                result = await self._chat_fn(messages, **kwargs)
            except BaseException as exc:
                # 异常类型必须落在 attributes 里：error_type 只能放一个类名，
                # 而「哪一步失败」往往需要看调用方的补充信息。
                _mark(exc, attributes={"error": type(exc).__name__}, record=record)
                raise
            preview = result if isinstance(result, str) else str(result)
            record.content = content_payload(
                preview,
                capture=self._tracer.config.capture_content,
                max_chars=self._tracer.config.max_content_chars,
            )
            return result

    def with_attempt(self, attempt: int) -> TracedChat:
        """返回同一个被包对象、但重试序号不同的包装器。

        重试链里每轮都该有独立的 span；复制包装器比让调用方自己拼参数更省事，
        也不会把试次序号泄漏到业务代码里。
        """
        return TracedChat(
            self._chat_fn,
            self._tracer,
            model=self._model,
            attempt=attempt,
        )

    def __getattr__(self, name: str) -> Any:
        """未定义属性透传给被包对象。"""
        return getattr(self._chat_fn, name)


class TracedRetriever:
    """包住检索器，每次 ``retrieve`` 产出一条 ``retriever`` span。

    Attributes:
        min_score: 拒答阈值，写入 span 供离线分析「这次为什么没召回」。
    """

    def __init__(
        self,
        retriever: Any,
        tracer: Tracer,
        *,
        min_score: float | None = None,
    ) -> None:
        """初始化。

        Args:
            retriever: 检索器，需提供 ``retrieve(query, top_k=..., **kwargs)``。
            tracer: 追踪门面。
            min_score: 拒答阈值；仅记录，不参与过滤。
        """
        self._retriever = retriever
        self._tracer = tracer
        self._min_score = min_score

    def retrieve(self, query: str, top_k: int = 3, **kwargs: Any) -> list[Any]:
        """检索并记录命中情况；异常照常抛出。

        Args:
            query: 查询文本。
            top_k: 召回条数。
            **kwargs: 透传给被包检索器的额外参数。

        Returns:
            被包检索器的召回结果。
        """
        with self._tracer.span("retrieve", "retriever", top_k=top_k) as record:
            if self._min_score is not None:
                # min_score 是浮点，走 attributes 清洗后仍是数值。
                record.attributes["min_score"] = self._min_score
            # query 属于敏感键，无法进入 attributes；正文只能走 content 通道，
            # 且默认只落哈希与长度。
            record.content = content_payload(
                query,
                capture=self._tracer.config.capture_content,
                max_chars=min(self._tracer.config.max_content_chars, QUERY_CONTENT_LIMIT),
            )
            record.attributes["query_chars"] = len(query)
            try:
                results = self._retriever.retrieve(query, top_k=top_k, **kwargs)
            except BaseException as exc:
                _mark(exc, attributes={"error": type(exc).__name__}, record=record)
                raise
            _record_hits(record, results)
            return results

    def __getattr__(self, name: str) -> Any:
        """未定义属性透传给被包检索器（``count`` / ``index`` 等）。"""
        return getattr(self._retriever, name)


class TracedTool:
    """包住工具调用，每次调用产出一条 ``tool`` span。

    工具名以 ``tool_<name>`` 命名，与既有冒烟脚本的记录名保持一致：一条 trace
    里同时出现 ``tool_check_commit`` 与 ``tool_read_file`` 时，靠名字前缀就能
    分辨，不必再读 attributes。
    """

    def __init__(self, call: Any, tracer: Tracer, *, tool_name: str = "tool") -> None:
        """初始化。

        Args:
            call: 异步可调用对象，签名 ``(name, arguments) -> result``。
            tracer: 追踪门面。
            tool_name: 缺省工具名；单次调用可被入参覆盖。
        """
        self._call = call
        self._tracer = tracer
        self._tool_name = tool_name

    async def __call__(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """调用工具并记录入参形状与结果状态；异常照常抛出。

        Args:
            name: 工具名。
            arguments: 工具入参。

        Returns:
            被包调用的返回值。
        """
        args = arguments or {}
        with self._tracer.span(
            f"tool_{name}",
            "tool",
            # 只记参数名不记参数值：参数里可能带文件路径或提交信息正文。
            arg_keys=sorted(str(key) for key in args)[:32],
            tool_name=name,
        ) as record:
            try:
                result = await self._call(name, arguments)
            except BaseException as exc:
                _mark(
                    exc,
                    attributes={"error": type(exc).__name__},
                    record=record,
                )
                raise
            record.attributes["denied"] = _is_denied(result)
            record.attributes["is_error"] = _is_error(result)
            return result

    def __getattr__(self, name: str) -> Any:
        """未定义属性透传给被包对象。"""
        return getattr(self._call, name)


def traced_chat(
    chat_fn: Any,
    tracer: Tracer,
    *,
    model: str | None = None,
    attempt: int = 1,
) -> TracedChat:
    """把对话函数包成产出 ``generation`` span 的版本。

    Args:
        chat_fn: 异步对话函数，签名 ``(messages) -> str``。
        tracer: 追踪门面。
        model: 模型名。
        attempt: 首次尝试的序号。

    Returns:
        :class:`TracedChat` 实例，接口与被包函数一致。
    """
    return TracedChat(chat_fn, tracer, model=model, attempt=attempt)


def traced_retriever(
    retriever: Any,
    tracer: Tracer,
    *,
    min_score: float | None = None,
) -> TracedRetriever:
    """把检索器包成产出 ``retriever`` span 的版本。

    Args:
        retriever: 检索器。
        tracer: 追踪门面。
        min_score: 拒答阈值。

    Returns:
        :class:`TracedRetriever` 实例。
    """
    return TracedRetriever(retriever, tracer, min_score=min_score)


def traced_tool(call: Any, tracer: Tracer, *, tool_name: str = "tool") -> TracedTool:
    """把工具调用包成产出 ``tool`` span 的版本。

    Args:
        call: 异步可调用对象，签名 ``(name, arguments) -> result``。
        tracer: 追踪门面。
        tool_name: 缺省工具名。

    Returns:
        :class:`TracedTool` 实例。
    """
    return TracedTool(call, tracer, tool_name=tool_name)


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------


def _record_hits(record: Any, results: Any) -> None:
    """把召回结果的规模与来源写进 span。

    ``sources`` 是敏感键（文件名可能暴露内部资料清单），必须清洗掉；这里只
    记录**去重后的来源个数**，既有诊断价值又不落具体文件名。
    """
    rows = list(results or [])
    record.attributes["hit_count"] = len(rows)
    if not rows:
        record.attributes["top1_score"] = 0.0
        return
    top = rows[0]
    score = getattr(top, "score", None)
    if isinstance(score, (int, float)):
        record.attributes["top1_score"] = float(score)
    sources = [getattr(row, "source", None) for row in rows]
    record.attributes["source_count"] = len({src for src in sources if src})


def _usage_of(target: Any) -> dict[str, int] | None:
    """从被包对象上读取 token 用量；读不到返回 ``None``。

    用量是可选信息，不同实现暴露方式不一致（有的是 ``usage`` 属性，有的是
    ``last_usage``）。这里只做只读探测，任何异常都当作「没有」处理，因为
    「拿不到用量」不该让一次成功的模型调用变成失败。
    """
    for attr in ("usage", "last_usage"):
        try:
            raw = getattr(target, attr, None)
        except Exception:  # noqa: BLE001 —— 属性探测失败等同于没有用量
            continue
        if not isinstance(raw, dict):
            continue
        usage = make_usage(
            input_tokens=raw.get("input_tokens") or raw.get("prompt_tokens"),
            output_tokens=raw.get("output_tokens") or raw.get("completion_tokens"),
        )
        if usage:
            return usage
    return None


def _mark(error: BaseException, *, attributes: dict[str, Any], record: Any) -> None:
    """把异常写进记录；只写不抛，保证原异常继续向上传播。"""
    try:
        record.status = "error"
        record.error_type = type(error).__name__
        record.error_message = str(error)[:200] or None
        record.attributes.update(attributes)
    except Exception as exc:  # noqa: BLE001 —— 留痕失败不得掩盖原异常
        logger.warning("observability.mark_error_failed err=%s", exc)


def _field(result: Any, name: str, default: Any = None) -> Any:
    """从结果对象或字典里取字段；取不到返回缺省值。"""
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _is_error(result: Any) -> bool:
    """判断工具结果是否表示失败。

    兼容三种表达方式：``is_error`` / ``isError``（MCP SDK 用驼峰）与
    ``error``。任何一个为真都算失败。
    """
    for name in ("is_error", "isError"):
        value = _field(result, name)
        if value is not None:
            return bool(value)
    return bool(_field(result, "error"))


def _is_denied(result: Any) -> bool:
    """判断工具结果是否属于「被安全策略拒绝」。

    确认门与白名单拒绝在业务上都是「没执行」，但两者在 trace 上必须与真正的
    执行失败分开，否则「拒绝率」与「失败率」会被混成一个数。
    """
    for name in ("denied", "blocked", "rejected"):
        value = _field(result, name)
        if value is not None:
            return bool(value)
    code = _field(result, "error_code") or _field(result, "code")
    return isinstance(code, str) and "DENIED" in code.upper()


__all__ = [
    "QUERY_CONTENT_LIMIT",
    "TracedChat",
    "TracedRetriever",
    "TracedTool",
    "traced_chat",
    "traced_retriever",
    "traced_tool",
]
