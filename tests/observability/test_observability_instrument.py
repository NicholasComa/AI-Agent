"""``instrument`` 三个包装工厂的用例。

核心口径：包装器**不改变被包对象的行为**（返回值、异常、属性可见性都一致），
只在其上追加记录。因此每个工厂至少覆盖三件事——正常路径的记录内容、异常路径的
留痕与重抛、以及属性透传。
"""

from __future__ import annotations

import pytest
from observability_fakes import LONG_TEXT, SOURCE_NAME

from observability import traced_chat, traced_retriever, traced_tool

# ----------------------------------------------------------------------
# traced_chat
# ----------------------------------------------------------------------


async def test_traced_chat_records_model_and_message_count(tracer, recorded):
    """一次正常调用产出一条 ``generation`` span，带模型名与消息条数。"""

    async def chat(messages):
        return "回答"

    result = await traced_chat(chat, tracer, model="qwen3")([{"role": "user"}] * 3)

    assert result == "回答"
    span = recorded.by_name("generate")
    assert span.kind == "generation"
    assert span.model == "qwen3"
    assert span.attributes["message_count"] == 3


async def test_traced_chat_returns_value_unchanged(tracer):
    """返回值原样透出，不被埋点改写。"""

    async def chat(messages):
        return "原样返回"

    assert await traced_chat(chat, tracer)([]) == "原样返回"


async def test_traced_chat_marks_error_and_reraises(tracer, recorded):
    """异常时标 error、把异常类型写进 attributes，且异常原样抛出。"""

    async def chat(messages):
        msg = "上游 502"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="上游 502"):
        await traced_chat(chat, tracer)([])

    span = recorded.by_name("generate")
    assert span.status == "error"
    assert span.error_type == "RuntimeError"
    assert span.attributes["error"] == "RuntimeError"


async def test_traced_chat_does_not_capture_text_by_default(tracer, recorded):
    """未开启内容捕获时，回答正文不落盘，只留哈希与长度。"""

    async def chat(messages):
        return LONG_TEXT

    await traced_chat(chat, tracer)([])

    span = recorded.by_name("generate")
    assert span.content is not None
    assert span.content["chars"] == len(LONG_TEXT)
    assert "text" not in span.content
    assert LONG_TEXT not in recorded.payload()


async def test_traced_chat_captures_truncated_text_when_enabled(tmp_path, recorded):
    """开启内容捕获并按上限截断。"""
    from observability_fakes import make_tracer

    tracer = make_tracer(tmp_path, backend=recorded, capture_content=True, max_content_chars=10)

    async def chat(messages):
        return LONG_TEXT

    await traced_chat(chat, tracer)([])

    span = recorded.by_name("generate")
    assert span.content["text"] == LONG_TEXT[:10]
    assert span.content["truncated"] is True


async def test_traced_chat_records_usage_when_available(tmp_path, recorded):
    """被包对象暴露用量时写入 span 与成本字段。"""
    from observability_fakes import make_tracer

    tracer = make_tracer(tmp_path, backend=recorded)

    class ChatWithUsage:
        usage = {"input_tokens": 12, "output_tokens": 7}

        async def __call__(self, messages):
            return "ok"

    await traced_chat(ChatWithUsage(), tracer)([])

    span = recorded.by_name("generate")
    assert span.usage == {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}


async def test_traced_chat_with_attempt_marks_retry_index(tracer, recorded):
    """重试链里每一轮都能标自己的试次序号。"""
    from observability.instrument import TracedChat

    async def chat(messages):
        return "ok"

    wrapper = TracedChat(chat, tracer)
    await wrapper.with_attempt(3)([])

    assert recorded.by_name("generate").attributes["attempt"] == 3


async def test_traced_chat_passes_through_attributes(tracer):
    """未定义属性透传给被包对象，包装层不做窄接口。"""

    async def chat(messages):
        return "ok"

    chat.custom_flag = "kept"  # type: ignore[attr-defined]
    assert traced_chat(chat, tracer).custom_flag == "kept"  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# traced_retriever
# ----------------------------------------------------------------------


class _Row:
    """最小召回结果，仅提供埋点会读的字段。"""

    def __init__(self, score: float, source: str) -> None:
        self.score = score
        self.source = source
        self.chunk_id = "c"
        self.text = "t"


def _retriever(rows):
    """构造返回固定结果的替身检索器。"""

    class Retriever:
        def retrieve(self, query, top_k=3, **kwargs):
            return list(rows)

        def count(self):
            return 42

    return Retriever()


def test_traced_retriever_records_hits_and_scores(tracer, recorded):
    """记录召回条数、Top1 分数与来源去重个数。"""
    retriever = traced_retriever(
        _retriever([_Row(0.9, "a.md"), _Row(0.7, "a.md"), _Row(0.5, "b.md")]),
        tracer,
        min_score=0.3,
    )

    result = retriever.retrieve("查询", top_k=3)

    assert len(result) == 3
    span = recorded.by_name("retrieve")
    assert span.kind == "retriever"
    assert span.attributes["hit_count"] == 3
    assert span.attributes["top1_score"] == 0.9
    assert span.attributes["source_count"] == 2
    assert span.attributes["min_score"] == 0.3


def test_traced_retriever_records_empty_result(tracer, recorded):
    """空召回时 Top1 分数记 0，而不是留空。"""
    traced_retriever(_retriever([]), tracer).retrieve("查询")

    span = recorded.by_name("retrieve")
    assert span.attributes["hit_count"] == 0
    assert span.attributes["top1_score"] == 0.0


def test_traced_retriever_hashes_query_without_capturing(tracer, recorded):
    """查询正文不落盘：只留哈希与长度，且不出现内部来源文件名。"""
    query = "VT1000 支持几个网口？"
    traced_retriever(_retriever([_Row(0.9, SOURCE_NAME)]), tracer).retrieve(query)

    span = recorded.by_name("retrieve")
    assert span.content["chars"] == len(query)
    assert "text" not in span.content
    payload = recorded.payload()
    assert query not in payload
    assert SOURCE_NAME not in payload


def test_traced_retriever_records_query_length_label(tracer, recorded):
    """长度以外不额外记录查询内容，长度本身便于排查空查询。"""
    traced_retriever(_retriever([]), tracer).retrieve("")

    assert recorded.by_name("retrieve").attributes["query_chars"] == 0


def test_traced_retriever_marks_error_and_reraises(tracer, recorded):
    """检索失败时标错误并重抛，供上层降级。"""

    class Broken:
        def retrieve(self, query, top_k=3, **kwargs):
            msg = "collection 未就绪"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="collection 未就绪"):
        traced_retriever(Broken(), tracer).retrieve("查询")

    span = recorded.by_name("retrieve")
    assert span.status == "error"
    assert span.attributes["error"] == "RuntimeError"


def test_traced_retriever_passes_through_attributes(tracer):
    """``count`` 等非埋点方法原样可见。"""
    assert traced_retriever(_retriever([]), tracer).count() == 42


# ----------------------------------------------------------------------
# traced_tool
# ----------------------------------------------------------------------


async def test_traced_tool_records_name_and_argument_keys(tracer, recorded):
    """记录工具名与参数**键**，不记录参数值。"""

    async def call(name, arguments):
        return {"ok": True}

    result = await traced_tool(call, tracer)("check_commit", {"repo": "r", "token": "secret-value"})

    assert result == {"ok": True}
    span = recorded.by_name("tool_check_commit")
    assert span.kind == "tool"
    assert span.attributes["tool_name"] == "check_commit"
    assert span.attributes["arg_keys"] == ["repo", "token"]
    assert "secret-value" not in recorded.payload()


async def test_traced_tool_marks_error_result(tracer, recorded):
    """工具以 ``isError`` 表达失败时记 ``is_error=true``，但不算异常。"""

    async def call(name, arguments):
        return {"isError": True}

    await traced_tool(call, tracer)("read_file", {})

    span = recorded.by_name("tool_read_file")
    assert span.attributes["is_error"] is True
    assert span.status == "ok"


async def test_traced_tool_detects_denied_result(tracer, recorded):
    """被安全策略拒绝与执行失败要分开记录。"""

    async def call(name, arguments):
        return {"denied": True, "error_code": "PATH_DENIED"}

    await traced_tool(call, tracer)("read_file", {})

    span = recorded.by_name("tool_read_file")
    assert span.attributes["denied"] is True


async def test_traced_tool_detects_snake_case_error(tracer, recorded):
    """兼容 ``is_error`` 与 ``isError`` 两种命名。"""

    async def call(name, arguments):
        return {"is_error": True}

    await traced_tool(call, tracer)("list_dir", {})

    assert recorded.by_name("tool_list_dir").attributes["is_error"] is True


async def test_traced_tool_marks_error_and_reraises(tracer, recorded):
    """工具调用抛异常时标错误并重抛。"""

    async def call(name, arguments):
        msg = "会话已关闭"
        raise ConnectionError(msg)

    with pytest.raises(ConnectionError, match="会话已关闭"):
        await traced_tool(call, tracer)("check_commit", {})

    span = recorded.by_name("tool_check_commit")
    assert span.status == "error"
    assert span.attributes["error"] == "ConnectionError"


async def test_traced_tool_handles_missing_arguments(tracer, recorded):
    """参数缺省为 ``None`` 时不报错。

    空列表会被清洗规则整体丢弃（空列表不携带信息），因此这里断言的是「调用
    成功且不产生 ``arg_keys`` 键」，而不是「记下空列表」。
    """

    async def call(name, arguments):
        assert arguments is None
        return {}

    await traced_tool(call, tracer)("list_tools")

    assert "arg_keys" not in recorded.by_name("tool_list_tools").attributes


async def test_traced_tool_passes_through_attributes(tracer):
    """未定义属性透传。"""

    async def call(name, arguments):
        return {}

    call.session_id = "s-1"  # type: ignore[attr-defined]
    assert traced_tool(call, tracer).session_id == "s-1"  # type: ignore[attr-defined]


async def test_wrappers_nest_into_one_parent_chain(tracer, recorded):
    """三个包装器嵌套时形成一条父子链，而不是各自成根。"""

    async def chat(messages):
        return "ok"

    def retrieve(query, top_k=3, **kwargs):
        return []

    async def call(name, arguments):
        return {}

    with tracer.trace("root"):
        traced_retriever(_retriever([]), tracer).retrieve("q")
        await traced_chat(chat, tracer)([])
        await traced_tool(call, tracer)("t", {})

    root = recorded.by_name("root")
    children = {"retrieve", "generate", "tool_t"}
    parents = {span.parent_id for span in recorded.records if span.name in children}
    assert parents == {root.span_id}
