"""检索增强生成：召回 → 组装提示词 → LLM 生成 → 引用校验 → 拒答。

在 :mod:`rag.knowledge_rag`（导入 + 召回）之上补上生成链路，使知识库
能够产出**带引用、可追溯、可拒答**的答案：

- **召回侧拒答**：Top1 余弦相似度低于 ``min_score``（默认 0.3）时直接返回
  :data:`NO_ANSWER_TEXT`，**不调用 LLM**（省成本、从源头防幻觉）；
- **LLM 侧拒答**：即使分数达标，提示词也要求模型在资料不足时输出
  ``has_answer=false``；
- **引用校验**：LLM 返回的 ``citations[].chunk_id`` 必须落在本次召回结果
  集合内，非法引用被丢弃；合法引用全部丢失时降级为拒答，保证
  「答案可追到原文」不被打折。

LLM 调用通过 :data:`ChatFn` 注入（异步、输入 messages 输出文本），与具体
客户端解耦；离线测试注入假实现即可跑通，真实链路由调用方把
:class:`rag.embeddings` / ``src.llm_client.LlmClient`` 适配成 :data:`ChatFn`。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .knowledge_rag import JwipcKnowledgeRAG
from .retriever import RetrievalResult

logger = logging.getLogger(__name__)

NO_ANSWER_TEXT = "资料中未找到"
"""无依据时的标准拒答文案（Roadmap 要求原文）。"""

DEFAULT_MIN_SCORE = 0.3
"""Top1 余弦相似度低于该值的查询直接拒答，不调用 LLM。"""

RejectReason = Literal[
    "no_hit",  # 召回结果为空
    "low_score",  # Top1 分数低于阈值
    "llm_no_answer",  # LLM 判定资料不足
    "invalid_citations",  # LLM 给出的引用全部无效
    "parse_error",  # LLM 输出无法解析 / 不满足 Schema
    "llm_error",  # LLM 调用失败（异常）
]

# LLM 调用契约：输入 messages，返回模型输出文本（awaitable）。
type ChatFn = Callable[[list[dict[str, str]]], Awaitable[str]]


class Citation(BaseModel):
    """一条答案引用：指向召回结果中的某个片段。"""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., description="资料文件名（召回结果的 source 字段）")
    chunk_id: str = Field(..., description="片段 ID，必须属于本次召回结果")
    quote: str = Field(..., description="引用原文片段，便于人工追溯到原文")


class RagAnswer(BaseModel):
    """一次检索增强生成的结果：答案 + 引用；或拒答说明。"""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(..., description="回答文本；拒答时为固定文案")
    has_answer: bool = Field(..., description="是否有依据的答案；false 表示拒答")
    citations: list[Citation] = Field(default_factory=list, description="答案引用依据")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="回答可信度 0.0-1.0")
    rejected_reason: RejectReason | None = Field(
        default=None, description="拒答原因；有答案时为 None"
    )
    raw_llm_text: str | None = Field(
        default=None, description="LLM 原始输出（调试用；仅在降级分支保留）"
    )


SYSTEM_PROMPT: str = """\
你是企业知识库问答助手。你只能依据下面提供的「参考资料」回答问题，禁止编造资料中没有的内容。

## 严格要求
- **只输出合法 JSON 对象**，禁止任何额外文本（不要 markdown 代码块、不要解释、不要前后缀）。
- 严格匹配以下 4 个字段：answer / has_answer / citations / confidence，字段名严格一致，不要新增字段。
- 如果参考资料不足以回答问题，has_answer 必须为 false，answer 留空字符串。
- citations 只能引用参考资料中的条目，每条包含 source / chunk_id / quote（quote 必须是该条目的原文片段）。

## 字段定义
1. **answer**: 对问题的完整回答，2-2000 字符，能独立成句。
2. **has_answer**: 布尔值，参考资料是否足以回答问题；不足以时为 false。
3. **citations**: 数组，引用依据；每条 {source, chunk_id, quote}，最多 3 条。
4. **confidence**: 0.0-1.0 的浮点数；资料越充分越高，资料不足应主动降低。

## 示例

### 示例 1（有依据）
资料：[1] source="qdrant_intro.md" chunk_id="qdrant_intro.md#0" 内容="Qdrant 是一个开源向量数据库，支持 Payload 过滤。"
问题：Qdrant 是什么？
输出：{"answer":"Qdrant 是一个开源向量数据库。","has_answer":true,"citations":[{"source":"qdrant_intro.md","chunk_id":"qdrant_intro.md#0","quote":"Qdrant 是一个开源向量数据库，支持 Payload 过滤。"}],"confidence":0.9}

### 示例 2（无依据）
资料：[1] source="qdrant_intro.md" chunk_id="qdrant_intro.md#0" 内容="Qdrant 是一个开源向量数据库。"
问题：公司下周三的团建安排是什么？
输出：{"answer":"","has_answer":false,"citations":[],"confidence":0.05}

## 当前任务
请基于下方「参考资料」回答「问题」，输出符合上述 schema 的 JSON。
"""


def build_rag_messages(
    query: str,
    contexts: list[RetrievalResult],
    *,
    system_prompt: str = SYSTEM_PROMPT,
) -> list[dict[str, str]]:
    """把问题与召回片段组装成 LLM 消息。

    Args:
        query: 用户问题。
        contexts: 召回结果（按相关度降序）。
        system_prompt: 覆盖默认系统提示词（主要用于测试）。

    Returns:
        可直接喂给 ``ChatFn`` 的 ``[system, user]`` 消息列表。
    """
    numbered = "\n".join(
        f"[{i + 1}] source={ctx.source} chunk_id={ctx.chunk_id} 内容={ctx.text}"
        for i, ctx in enumerate(contexts)
    )
    user = f"## 参考资料\n{numbered}\n\n## 问题\n{query}"
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user},
    ]


def _parse_llm_json(text: str) -> dict:
    """宽容解析 LLM 输出为 JSON 对象。

    优先按裸 JSON 解析；失败时剥离 markdown 代码块（`````json```` ... ）
    后再试一次，兼容模型偶发加壳的情况。
    """
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.DOTALL)
        if not match:
            raise
        obj = json.loads(match.group(1).strip())
    if not isinstance(obj, dict):
        msg = f"LLM output must be a JSON object, got {type(obj).__name__}"
        raise json.JSONDecodeError(msg, text, 0)
    return obj


class RagGenerator:
    """检索增强生成器：把知识库召回与 LLM 生成串成一条可拒答的问答链路。

    Args:
        rag: 已导入知识库的 :class:`JwipcKnowledgeRAG`（提供 ``retrieve``）。
        chat: LLM 调用契约，输入 messages 返回模型文本（异步）。
        top_k: 每次召回片段数（``<= 0`` 抛 ``ValueError``）。
        min_score: Top1 相似度阈值，低于即拒答（须在 0.0-1.0 内）。
        system_prompt: 覆盖默认系统提示词。
        no_answer_text: 拒答文案。
    """

    def __init__(
        self,
        rag: JwipcKnowledgeRAG,
        chat: ChatFn,
        *,
        top_k: int = 3,
        min_score: float = DEFAULT_MIN_SCORE,
        system_prompt: str = SYSTEM_PROMPT,
        no_answer_text: str = NO_ANSWER_TEXT,
    ) -> None:
        if top_k <= 0:
            msg = f"top_k must be > 0, got {top_k}"
            raise ValueError(msg)
        if not 0.0 <= min_score <= 1.0:
            msg = f"min_score must be in 0.0-1.0, got {min_score}"
            raise ValueError(msg)
        if not callable(chat):
            msg = f"chat must be a callable returning an awaitable str, got {type(chat).__name__}"
            raise TypeError(msg)
        self._rag = rag
        self._chat = chat
        self._top_k = top_k
        self._min_score = min_score
        self._system_prompt = system_prompt
        self._no_answer_text = no_answer_text

    @property
    def top_k(self) -> int:
        return self._top_k

    @property
    def min_score(self) -> float:
        return self._min_score

    async def answer(self, query: str) -> RagAnswer:
        """对 ``query`` 执行检索增强生成，返回带引用答案或拒答结果。

        流程：召回 → 空结果/低分直接拒答 → 组装消息 → 调 LLM → 解析 →
        引用白名单校验 → 有依据返回答案，否则降级拒答。
        """
        results = self._rag.retrieve(query, top_k=self._top_k)
        if not results:
            return self._reject("no_hit")
        if results[0].score < self._min_score:
            return self._reject("low_score")

        messages = build_rag_messages(query, results, system_prompt=self._system_prompt)
        try:
            text = await self._chat(messages)
        except Exception as exc:  # noqa: BLE001 - 任何调用失败都降级为拒答
            logger.warning("rag.answer llm_error=%s", exc)
            return self._reject("llm_error")

        try:
            parsed = RagAnswer.model_validate(_parse_llm_json(text))
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("rag.answer parse_error=%s", exc)
            return self._reject("parse_error", raw=text)

        valid_ids = {r.chunk_id for r in results}
        valid = [c for c in parsed.citations if c.chunk_id in valid_ids]
        if not parsed.has_answer:
            return self._reject("llm_no_answer", raw=text, citations=valid)
        if not valid:
            return self._reject("invalid_citations", raw=text)
        return RagAnswer(
            answer=parsed.answer,
            has_answer=True,
            citations=valid,
            confidence=parsed.confidence,
            raw_llm_text=text,
        )

    def _reject(
        self,
        reason: RejectReason,
        *,
        raw: str | None = None,
        citations: list[Citation] | None = None,
    ) -> RagAnswer:
        return RagAnswer(
            answer=self._no_answer_text,
            has_answer=False,
            citations=citations or [],
            confidence=0.0,
            rejected_reason=reason,
            raw_llm_text=raw,
        )
