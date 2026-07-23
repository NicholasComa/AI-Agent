"""Tests for :mod:`src.schemas` + :mod:`src.prompts` (Day 4 structured output).

All tests use ``httpx.MockTransport`` so the suite never hits the network. Two
groups of tests:

1. **5x stability test** - call the LLM 5 times on the same input and verify
   that the structure of the returned Pydantic model is stable across runs
   (same field names, type-correct, sane confidence band).

2. **Five-sample tests** - one parametrized test per sample (normal /
   ambiguous / missing_info / irrelevant / very_long). Each uses a handler
   that returns a plausible RequirementAnalysis for that input and checks
   schema acceptance plus per-sample sanity bands.

Two boundary tests cover ``extra="forbid"`` (extra field) and the
``confidence`` range constraint.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from llm_client import LlmClient
from prompts import build_messages
from schemas import RequirementAnalysis

# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_PATH = REPO_ROOT / "examples" / "requirement_samples.json"


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> LlmClient:
    """Build a ``LlmClient`` whose HTTP layer is fully mocked."""
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return LlmClient(
        base_url="https://api.example.com/v1",
        model="qwen3",
        api_key="test-key",
        timeout_seconds=1.0,
        client=http_client,
    )


def _wrap_payload(payload: dict[str, Any], *, model: str = "qwen3") -> dict[str, Any]:
    """Wrap a JSON-serialisable ``payload`` as a chat-completion response body."""
    return {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 50, "total_tokens": 60},
    }


def _ok_handler_with_payload(
    payload: dict[str, Any],
) -> Callable[[httpx.Request], httpx.Response]:
    """200 OK handler returning a fixed chat-completion payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_wrap_payload(payload))

    return handler


def _cycling_handler(
    variants: list[dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Handler that cycles through ``variants[0], variants[1], ...`` on each call."""

    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = variants[state["i"] % len(variants)]
        state["i"] += 1
        return httpx.Response(200, json=_wrap_payload(payload, model=f"qwen3-r{state['i']}"))

    return handler


def _load_samples() -> dict[str, dict[str, Any]]:
    with SAMPLES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Test data: 5 plausible variants for the same "normal" input
# ---------------------------------------------------------------------------

NORMAL_VARIANTS: list[dict[str, Any]] = [
    {
        "title": "B2C 电商网站",
        "category": "web",
        "functional_points": ["用户登录", "商品浏览", "购物车", "订单管理", "在线支付"],
        "risks": ["支付安全合规", "高并发性能瓶颈", "第三方支付接口稳定性"],
        "clarification_questions": ["需要支持哪些支付方式？", "并发量级预期是多少？"],
        "confidence": 0.9,
    },
    {
        "title": "电商网站系统",
        "category": "web",
        "functional_points": [
            "手机/微信登录",
            "商品浏览与搜索",
            "购物车管理",
            "订单管理",
            "支付集成",
        ],
        "risks": ["支付安全合规", "高并发", "搜索性能"],
        "clarification_questions": ["支持哪些支付渠道？"],
        "confidence": 0.88,
    },
    {
        "title": "B2C 电商平台",
        "category": "web",
        "functional_points": ["用户登录", "商品展示", "购物车", "支付", "后台管理"],
        "risks": ["支付合规风险", "性能与扩展性", "数据安全"],
        "clarification_questions": ["支付方式？", "预期用户量？"],
        "confidence": 0.92,
    },
    {
        "title": "中小型电商网站",
        "category": "web",
        "functional_points": [
            "登录注册",
            "商品搜索浏览",
            "购物车",
            "订单管理",
            "支付与对账",
            "后台管理",
        ],
        "risks": ["支付牌照与合规", "高并发处理", "数据备份与灾备"],
        "clarification_questions": ["是否需要会员等级？"],
        "confidence": 0.87,
    },
    {
        "title": "零售商电商网站",
        "category": "web",
        "functional_points": ["用户登录", "商品浏览", "购物车", "订单与支付", "后台商品/订单管理"],
        "risks": ["支付通道稳定性", "并发与限流", "数据安全合规"],
        "clarification_questions": ["支持哪些支付方式？", "需要小程序端吗？"],
        "confidence": 0.85,
    },
]


# Per-sample "plausible" answers (used by the 5 parametrized sample tests)
SAMPLE_ANSWERS: dict[str, dict[str, Any]] = {
    "normal": NORMAL_VARIANTS[0],
    "ambiguous": {
        "title": "未明确的产品需求",
        "category": "other",
        "functional_points": [],
        "risks": ["需求范围不清", "资源投入难以估算", "目标用户未定义"],
        "clarification_questions": [
            "您想做什么类型的产品？",
            "目标用户是谁？",
            "期望的核心功能是什么？",
            "预算和时间线？",
        ],
        "confidence": 0.15,
    },
    "missing_info": {
        "title": "推荐系统需求",
        "category": "ai",
        "functional_points": ["个性化推荐", "用户行为收集"],
        "risks": ["冷启动问题", "推荐效果评估困难", "数据隐私合规"],
        "clarification_questions": [
            "推荐什么类型的商品或内容？",
            "目标用户群体是谁？",
            "有哪些可用的用户行为数据？",
            "推荐效果的评估指标？",
        ],
        "confidence": 0.4,
    },
    "irrelevant": {
        "title": "非产品需求输入",
        "category": "other",
        "functional_points": [],
        "risks": [],
        "clarification_questions": ["请提供产品需求描述"],
        "confidence": 0.05,
    },
    "very_long": {
        "title": "B2C 电商平台（综合）",
        "category": "web",
        "functional_points": [
            "用户登录与会员体系",
            "商品多级分类与详情",
            "购物车与多种支付集成",
            "后台订单与商品管理",
            "营销活动与数据看板",
        ],
        "risks": [
            "等保 2.0 三级与个保法合规",
            "峰值 2000 QPS 高并发架构",
            "灾备 RPO 15min / RTO 1h 多活部署",
            "K8s + 微服务运维复杂度",
        ],
        "clarification_questions": [
            "是否已有支付牌照？",
            "客户端优先级：先做小程序还是 App？",
            "是否需要对接第三方 ERP / WMS？",
        ],
        "confidence": 0.85,
    },
}


# ---------------------------------------------------------------------------
# 5x stability test
# ---------------------------------------------------------------------------


async def test_stability_5_runs_same_input(capsys: pytest.CaptureFixture[str]) -> None:
    """Run the same input 5 times; structure must be stable across all runs.

    Mock handler cycles through 5 plausible-but-different RequirementAnalysis
    JSONs, mimicking real LLM non-determinism. The test asserts:
    - All 5 parses succeed (Pydantic validation passes)
    - All 5 return the same category ("web")
    - All titles have plausible length (2-80)
    - All confidences in sane band [0.5, 0.95]
    - All have at least 3 functional points (the input clearly demands it)
    """
    client = _make_client(_cycling_handler(NORMAL_VARIANTS))
    customer_input = "做一个电商网站，登录+商品浏览+购物车+支付"
    messages = build_messages(customer_input)

    results: list[RequirementAnalysis] = []
    async with client:
        for _ in range(5):
            resp = await client.chat(
                messages,
                extra_body={
                    "response_format": {"type": "json_object"},
                    "temperature": 0.0,
                },
            )
            parsed = RequirementAnalysis.model_validate_json(resp.text)
            results.append(parsed)

    # All 5 parses succeeded
    assert len(results) == 5
    # All in same category (structural stability)
    assert all(r.category == "web" for r in results), [r.category for r in results]
    # All titles have plausible length
    assert all(2 <= len(r.title) <= 80 for r in results), [r.title for r in results]
    # All confidences in sane band
    confidences = [r.confidence for r in results]
    assert all(0.5 <= c <= 0.95 for c in confidences), confidences
    # All have at least 3 functional points (the input clearly demands it)
    assert all(len(r.functional_points) >= 3 for r in results)

    # Print 5-run report (visible with ``pytest -s``)
    with capsys.disabled():
        print("\n=== 5x stability report (same input, 5 runs) ===")
        for i, r in enumerate(results, 1):
            print(
                f"  run {i}: title={r.title!r} | category={r.category} | "
                f"fp={len(r.functional_points)} | risks={len(r.risks)} | "
                f"cq={len(r.clarification_questions)} | conf={r.confidence}"
            )


# ---------------------------------------------------------------------------
# Per-sample tests (5 samples)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sample_id",
    ["normal", "ambiguous", "missing_info", "irrelevant", "very_long"],
)
async def test_sample_accepted_by_schema(sample_id: str) -> None:
    """For each of 5 sample inputs, the LLM response validates as RequirementAnalysis.

    Each sample uses a mock handler returning a plausible RequirementAnalysis
    tailored to that input. The test verifies:
    - 6 fields all present and type-correct
    - confidence in [0, 1]
    - title length 2-80
    - functional_points count within [0, 6]
    - risks count within [0, 4]
    - clarification_questions count within [0, 5]
    - per-sample sanity: ambiguous/irrelevant low conf, normal/very_long high
    """
    samples = _load_samples()
    sample = samples[sample_id]
    payload = SAMPLE_ANSWERS[sample_id]
    client = _make_client(_ok_handler_with_payload(payload))

    async with client:
        resp = await client.chat(
            build_messages(sample["text"]),
            extra_body={
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
            },
        )

    parsed = RequirementAnalysis.model_validate_json(resp.text)

    # Universal field checks
    assert 2 <= len(parsed.title) <= 80, parsed.title
    assert 0.0 <= parsed.confidence <= 1.0
    assert 0 <= len(parsed.functional_points) <= 6
    assert 0 <= len(parsed.risks) <= 4
    assert 0 <= len(parsed.clarification_questions) <= 5

    # Per-sample sanity bands
    if sample_id in ("ambiguous", "irrelevant"):
        assert parsed.confidence <= 0.3, (
            f"{sample_id} should have low confidence, got {parsed.confidence}"
        )
    elif sample_id in ("normal", "very_long"):
        assert parsed.confidence >= 0.7, (
            f"{sample_id} should have high confidence, got {parsed.confidence}"
        )
    elif sample_id == "missing_info":
        assert 0.2 <= parsed.confidence <= 0.7, (
            f"missing_info should be in middle band, got {parsed.confidence}"
        )

    # Per-sample category sanity
    if sample_id in ("ambiguous", "irrelevant"):
        assert parsed.category == "other"
    elif sample_id == "missing_info":
        assert parsed.category == "ai"
    elif sample_id in ("normal", "very_long"):
        assert parsed.category == "web"


# ---------------------------------------------------------------------------
# Boundary tests
# ---------------------------------------------------------------------------


async def test_extra_field_rejected() -> None:
    """LLM returning an extra field (hallucination) must raise ValidationError."""
    bad_payload = dict(SAMPLE_ANSWERS["normal"], hallucination="foo")
    client = _make_client(_ok_handler_with_payload(bad_payload))

    async with client:
        resp = await client.chat(
            build_messages("test"),
            extra_body={
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
            },
        )

    with pytest.raises(ValidationError):
        RequirementAnalysis.model_validate_json(resp.text)


async def test_invalid_confidence_rejected() -> None:
    """LLM returning confidence > 1.0 must raise ValidationError."""
    bad_payload = dict(SAMPLE_ANSWERS["normal"], confidence=1.5)
    client = _make_client(_ok_handler_with_payload(bad_payload))

    async with client:
        resp = await client.chat(
            build_messages("test"),
            extra_body={
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
            },
        )

    with pytest.raises(ValidationError):
        RequirementAnalysis.model_validate_json(resp.text)
