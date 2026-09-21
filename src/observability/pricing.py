"""模型单价表与成本估算。

**为什么要有这张表，即使当前跑的是本地模型**：成本对比是本周评测报告的
维度之一。本地推理的边际成本接近 0，若直接把成本写成 ``0``，等将来换成
云端模型时，历史数据与新增数据之间就失去了可比性。因此统一按「token 数 ×
单价」折算：本地模型单价记为 0，口径与云端保持一致。

表内数值需要按官方定价页核对后再用于对外报表（见模块末 ``TODO``）。
"""

from __future__ import annotations

from collections.abc import Mapping

PRICES_USD_PER_1K_TOKENS: dict[str, tuple[float, float]] = {
    "qwen3": (0.0, 0.0),
    "qwen3:1.7b": (0.0, 0.0),
    "qwen3:8b": (0.0, 0.0),
    "qwen2.5:3b": (0.0, 0.0),
}
"""模型名 → ``(输入单价, 输出单价)``，单位：美元 / 千 token。

本地模型一律记 0；表里没有的模型返回 ``None`` 而不是猜一个值。
键按「精确匹配 → 前缀匹配」的顺序查找，因此 ``qwen3:8b`` 既能命中自己的
条目，也能在只有 ``qwen3`` 时才回退到前缀。
"""


def resolve_price(model: str | None) -> tuple[float, float] | None:
    """查找模型单价。

    Args:
        model: 模型名，允许带 ``:tag`` 后缀。

    Returns:
        ``(输入单价, 输出单价)``；模型为空或不在表内（含前缀回退失败）时返回
        ``None``。
    """
    if not model:
        return None
    if model in PRICES_USD_PER_1K_TOKENS:
        return PRICES_USD_PER_1K_TOKENS[model]
    for name, price in PRICES_USD_PER_1K_TOKENS.items():
        if model.startswith(name):
            return price
    return None


def estimate_cost_usd(model: str | None, usage: Mapping[str, int] | None) -> float | None:
    """按 token 用量估算成本。

    Args:
        model: 模型名。
        usage: 含 ``input_tokens`` / ``output_tokens`` 的用量字典。

    Returns:
        美元成本；模型不在单价表内、或用量缺失时返回 ``None``。**不返回 0 作为
        兜底**——「免费」和「不知道单价」是两件事，混在一起会让对比表里的
        成本列失去意义。
    """
    if not usage:
        return None
    price = resolve_price(model)
    if price is None:
        return None
    input_price, output_price = price
    prompt_tokens = max(int(usage.get("input_tokens", 0)), 0)
    completion_tokens = max(int(usage.get("output_tokens", 0)), 0)
    cost = (prompt_tokens * input_price + completion_tokens * output_price) / 1000.0
    return round(cost, 8)


# TODO: 填入云端模型（如 deepseek / openai）的官方单价后，成本列才可跨模型比较。
__all__ = ["PRICES_USD_PER_1K_TOKENS", "estimate_cost_usd", "resolve_price"]
