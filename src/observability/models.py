"""追踪数据模型与纯函数工具。

本模块只放数据结构和无副作用的纯函数，不碰文件、网络与事件循环，因此
可以被追踪门面（:mod:`observability.tracer`）与各后端自由复用，也能在
测试里单独验证。

两类值的边界
------------

追踪记录里有两类内容，处理方式刻意分开：

``attributes``
    声明为「元数据」：节点名、条数、分数、状态码这类短标签。受键名黑名单
    与长度上限双重约束，超限一律**丢弃**而不是截断。

``content``
    声明为「正文」：提示词、回答、检索到的片段原文。恒定走哈希通道，
    只有显式开启内容捕获时才会带上截断后的原文。

**为什么把「元数据」和「正文」拆成两个字段，而不是靠长度阈值自动判断**：
节点名（``plan``）与来源文件名（``a.md``）都是短字符串，任何「超过 N 字符
即视为正文」的隐式规则都必然漏掉其中一个。判断权交给调用方之后，
「未开启内容捕获时不出现任何原文」就是**结构上**成立的——``content``
字段此时恒为 ``None``——而不是靠约定维持。
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

SPAN_KINDS: tuple[str, ...] = ("trace", "span", "generation", "retriever", "tool", "chain")
"""允许的 span 类型。

``trace`` 是一条 trace 的根；``generation`` 对应一次模型调用（带模型名与
token 用量）；``retriever`` 与 ``tool`` 分别对应检索和工具调用；``chain``
对应组合逻辑（工作流整体）。其余未分类区间用 ``span``。
"""

DEFAULT_KIND = "span"
"""无法识别 kind 时的兜底取值。"""

SPAN_STATUS: tuple[str, ...] = ("ok", "error")
"""span 的结束状态。"""

MAX_ATTRIBUTE_KEY_CHARS = 64
"""属性键名长度上限。"""

MAX_ERROR_MESSAGE_CHARS = 160
"""异常摘要的长度上限；带上完整堆栈会淹没有效信息。"""

HASH_LENGTH = 12
"""正文哈希保留的十六进制位数。

12 位（48 bit）足以区分同一语料库内的片段，又不至于让 JSONL 行变长。
哈希只用于「同一条正文在不同 trace 里是否一致」的比对，不作为安全边界。
"""

SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "answer",
        "body",
        "chunk",
        "content",
        "doc",
        "document",
        "file",
        "filename",
        "input",
        "message",
        "messages",
        "output",
        "path",
        "prompt",
        "query",
        "quote",
        "response",
        "source",
        "sources",
        "text",
    }
)
"""禁止出现在 ``attributes`` 里的键名。

这些位置在业务代码里放的是正文或来源文件名，一旦允许写入属性，就等于绕过
了 ``content`` 字段的哈希通道。命中即丢弃并记 debug 日志。
"""


def utc_now() -> str:
    """当前 UTC 时间的 ISO8601 字符串（微秒精度，``Z`` 结尾）。

    比日志里的秒级格式（``%Y-%m-%dT%H:%M:%S``）精细，因为 span 的耗时常在
    毫秒量级，秒级时间戳无法用来核对节点先后顺序。
    """
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_span_id() -> str:
    """生成 16 位十六进制 span 标识（与 W3C span-id 长度一致）。"""
    return uuid.uuid4().hex[:16]


def new_trace_id(seed: str | None = None) -> str:
    """生成 32 位十六进制 trace 标识（与 W3C trace-id 长度一致）。

    Args:
        seed: 给定则返回该值的 SHA-256 前 32 位，使同一 seed 每次都得到同一个
            trace 标识，便于把「同一次请求」在本地 JSONL 与远端之间对齐、
            也让测试不必依赖随机数。为 ``None`` 时返回随机标识。

    Returns:
        32 位小写十六进制字符串。
    """
    if seed is None:
        return uuid.uuid4().hex
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def single_line(text: str, *, limit: int = MAX_ERROR_MESSAGE_CHARS) -> str:
    """把多行文本压成单行并截断。

    异常消息里常带换行与缩进，直接进 JSONL 会让一行记录跨多行、破坏逐行
    解析；截断则避免把整个上游响应体塞进记录。
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _keep_scalar(value: Any, *, max_label_chars: int) -> tuple[bool, Any]:
    """判断单个属性值是否可以保留。

    Returns:
        ``(是否保留, 规范化后的值)``。超出长度上限的字符串**整体丢弃**而不是
        截断——一个几千字符的值几乎必然是正文误入，保留它的前 64 个字符仍然
        是泄露正文。列表按同一口径处理：任一元素超限则整个值丢弃。
    """
    if value is None or isinstance(value, bool | int):
        return True, value
    if isinstance(value, float):
        # NaN / Infinity 无法序列化成合法 JSON，只能在入口挡掉。
        return (True, value) if math.isfinite(value) else (False, None)
    if isinstance(value, str):
        return (True, value) if len(value) <= max_label_chars else (False, None)
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        if all(len(item) <= max_label_chars for item in value):
            return True, list(value)
        return False, None
    return False, None


def sanitize_attributes(values: Mapping[str, Any], *, max_label_chars: int) -> dict[str, Any]:
    """清洗属性字典：只保留合规的标量，任何异常情况都不向外抛。

    规则：键名必须是非空字符串且不超过 :data:`MAX_ATTRIBUTE_KEY_CHARS`，且
    小写形式不在 :data:`SENSITIVE_KEYS` 中；值只能是标量或全字符串列表。

    **刻意不做嵌套结构的序列化兜底**：把 ``dict`` / 自定义对象 ``json.dumps``
    之后塞进属性，等于给「正文悄悄漏出去」开了一条后门——调用方以为自己在传
    元数据，实际把整段提示词写进了追踪记录。无法处理的值一律丢弃。

    Args:
        values: 原始属性字典。
        max_label_chars: 单个字符串值的长度上限。

    Returns:
        清洗后的新字典；被丢弃的键会记 debug 日志，便于排查「为什么指标里
        少了一个字段」。
    """
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key or len(key) > MAX_ATTRIBUTE_KEY_CHARS:
            logger.debug("observability.attribute_dropped reason=bad_key key=%r", key)
            continue
        if key.lower() in SENSITIVE_KEYS:
            logger.debug("observability.attribute_dropped reason=sensitive_key key=%s", key)
            continue
        keep, normalized = _keep_scalar(value, max_label_chars=max_label_chars)
        if not keep:
            logger.debug(
                "observability.attribute_dropped reason=unsupported_value key=%s type=%s",
                key,
                type(value).__name__,
            )
            continue
        cleaned[key] = normalized
    return cleaned


def content_payload(value: str, *, capture: bool, max_chars: int) -> dict[str, Any]:
    """正文的唯一入口：恒定产出哈希与长度，只有开启捕获时才附带原文。

    Args:
        value: 正文内容。
        capture: 是否记录原文。为 ``False`` 时不返回 ``text`` 键，因此调用方
            无从拿到原文，也就无从写进记录。
        max_chars: 原文截断长度，仅 ``capture=True`` 时生效。

    Returns:
        含 ``sha256`` 与 ``chars`` 的字典；``capture=True`` 时另有 ``text``
        与 ``truncated``。哈希始终基于**完整**正文计算，不受截断影响。
    """
    payload: dict[str, Any] = {
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:HASH_LENGTH],
        "chars": len(value),
    }
    if capture:
        payload["text"] = value[:max_chars]
        payload["truncated"] = len(value) > max_chars
    return payload


def make_usage(
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, int] | None:
    """组装 token 用量字典；两个计数都缺失时返回 ``None``。

    键名沿用 OpenAI 兼容接口的 ``input_tokens`` / ``output_tokens``，并补一个
    ``total_tokens``。负数按 0 处理：上游偶发返回负数时，让它显示成 0 比让
    成本统计出现负值更容易发现问题。
    """
    if input_tokens is None and output_tokens is None:
        return None
    prompt = max(int(input_tokens or 0), 0)
    completion = max(int(output_tokens or 0), 0)
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": prompt + completion,
    }


@dataclass
class SpanRecord:
    """一条 span 记录，同时是本地 JSONL 的行格式与远端上报的载荷来源。

    刻意**不冻结**：调用方拿到 :meth:`Tracer.span` 产出的对象后要往里写元数据
    （``record.attributes["top_k"] = 3``），而收尾未结束的 span 时需要改状态位。
    冻结的 dataclass 只能靠 :func:`dataclasses.replace` 造新对象，此时调用方
    手里的旧对象会与新对象脱节。可变对象 + 投递前 :meth:`snapshot` 的组合，
    是这里最不容易出现「两处状态不一致」的做法。

    Attributes:
        trace_id: 所属 trace 的 32 位标识。
        span_id: 本 span 的 16 位标识。
        parent_id: 父 span 标识；根 span 为 ``None``。
        name: 可读名称，如 ``retrieve`` / ``generate``。
        kind: 取值见 :data:`SPAN_KINDS`。
        started_at: 进入时间，ISO8601 UTC。
        ended_at: 结束时间；``None`` 表示尚未结束。
        duration_ms: 耗时毫秒，由单调时钟差值计算。
        status: ``ok`` 或 ``error``。
        error_type: 异常类名。
        error_message: 单行异常摘要，已截断。
        model: 模型名，仅 ``generation`` 有值。
        usage: token 用量，见 :func:`make_usage`。
        cost_usd: 估算成本，查不到单价时为 ``None``。
        request_id: 复用请求中间件注入的请求标识。
        attributes: 已清洗的元数据。
        content: 正文哈希与可选原文；未开启捕获时为 ``None``。
        unfinished: 是否属于「进入后没走完就退出」的 span。
    """

    trace_id: str
    span_id: str
    parent_id: str | None = None
    name: str = ""
    kind: str = DEFAULT_KIND
    started_at: str = ""
    ended_at: str | None = None
    duration_ms: float | None = None
    status: str = "ok"
    error_type: str | None = None
    error_message: str | None = None
    model: str | None = None
    usage: dict[str, int] | None = None
    cost_usd: float | None = None
    request_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    content: dict[str, Any] | None = None
    unfinished: bool = False

    def to_payload(self) -> dict[str, Any]:
        """导出为字典。

        **所有字段恒定出现**，缺失的用 ``None`` 占位，而不是省略键。固定
        schema 让下游统计脚本不必到处写 ``payload.get(...)``，也让「字段漏写」
        在比对时立刻暴露。
        """
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "model": self.model,
            "usage": self.usage,
            "cost_usd": self.cost_usd,
            "request_id": self.request_id,
            "attributes": self.attributes,
            "content": self.content,
            "unfinished": self.unfinished,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> SpanRecord | None:
        """从字典还原记录；结构不符时返回 ``None`` 而不抛异常。

        用于读回历史 JSONL：文件可能是旧版本代码写的、也可能被手工改过，
        逐行解析时遇到坏数据应跳过而不是中断整个读取。
        """
        if not isinstance(payload, dict):
            return None
        trace_id = payload.get("trace_id")
        span_id = payload.get("span_id")
        if not isinstance(trace_id, str) or not isinstance(span_id, str):
            return None
        kind = payload.get("kind")
        status = payload.get("status")
        attributes = payload.get("attributes")
        usage = payload.get("usage")
        content = payload.get("content")
        return cls(
            trace_id=trace_id,
            span_id=span_id,
            parent_id=payload.get("parent_id"),
            name=payload.get("name") or "",
            kind=kind if kind in SPAN_KINDS else DEFAULT_KIND,
            started_at=payload.get("started_at") or "",
            ended_at=payload.get("ended_at"),
            duration_ms=payload.get("duration_ms"),
            status=status if status in SPAN_STATUS else "ok",
            error_type=payload.get("error_type"),
            error_message=payload.get("error_message"),
            model=payload.get("model"),
            usage=usage if isinstance(usage, dict) else None,
            cost_usd=payload.get("cost_usd"),
            request_id=payload.get("request_id"),
            attributes=attributes if isinstance(attributes, dict) else {},
            content=content if isinstance(content, dict) else None,
            unfinished=bool(payload.get("unfinished")),
        )

    def snapshot(self) -> SpanRecord:
        """深拷贝一份交给后端。

        投递之后调用方仍可能继续往 ``attributes`` 里写（例如 span 退出后又补
        了一个字段），共享同一个字典会让「已经上报的数据」被就地改写。拷贝
        的成本相对一次落盘 IO 可以忽略。
        """
        return copy.deepcopy(self)


__all__ = [
    "DEFAULT_KIND",
    "HASH_LENGTH",
    "MAX_ATTRIBUTE_KEY_CHARS",
    "MAX_ERROR_MESSAGE_CHARS",
    "SENSITIVE_KEYS",
    "SPAN_KINDS",
    "SPAN_STATUS",
    "SpanRecord",
    "content_payload",
    "make_usage",
    "new_span_id",
    "new_trace_id",
    "sanitize_attributes",
    "single_line",
    "utc_now",
]
