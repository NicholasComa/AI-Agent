"""通用 Metadata Filter：把过滤条件编译为 Qdrant ``Filter`` 对象。

支持两种条件：

- ``str`` 值 → 精确匹配（``MatchValue``）；
- ``list[str]`` 值 → 命中任一即满足（``MatchAny``）。

多个键之间为 AND 语义；``source_filter`` 与 ``MetadataConditions``
可同时使用，合并为同一个 ``Filter``。
"""

from __future__ import annotations

from typing import Any

from qdrant_client.http import models as qmodels

MetadataConditions = dict[str, str | list[str]]
"""元数据过滤条件：payload 键 -> 精确值或候选集。"""


def _field_condition(key: str, value: str | list[str]) -> qmodels.FieldCondition:
    """把单个键值条件编译为 FieldCondition。"""
    if isinstance(value, str):
        match: Any = qmodels.MatchValue(value=value)
    else:
        match = qmodels.MatchAny(any=list(value))
    return qmodels.FieldCondition(key=key, match=match)


def build_filter(
    conditions: MetadataConditions | None,
    source_filter: str | None = None,
) -> qmodels.Filter | None:
    """把元数据条件与来源过滤编译成 Qdrant Filter。

    Args:
        conditions: payload 键值条件，``None`` 或空 dict 表示不过滤。
        source_filter: 可选 ``source`` 精确匹配，单独成为一条条件。

    Returns:
        合并后的 :class:`Filter`；无条件时返回 ``None``（全量检索）。
    """
    merged: list[MetadataConditions] = []
    if conditions:
        merged.append(conditions)
    if source_filter is not None:
        merged.append({"source": source_filter})
    if not merged:
        return None
    must = [_field_condition(key, value) for cond in merged for key, value in cond.items()]
    return qmodels.Filter(must=must) if must else None


def source_is(*sources: str) -> MetadataConditions:
    """构造「来源命中任一」的过滤条件。"""
    return {"source": list(sources)}


def file_type_is(*types: str) -> MetadataConditions:
    """构造「文件类型命中任一」的过滤条件。"""
    return {"file_type": list(types)}
