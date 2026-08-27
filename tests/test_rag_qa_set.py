"""``examples/qa_set.json`` 数据完整性校验（纯 pytest，不联网）。

第 6 周通过标准要求 30 条问答集且含 10 条明确无答案问题，本测试把
「数据形状」固化为断言：

- 共 30 条；``expect_found=false`` 恰好 10 条；
- 每条 ``question`` 非空且不重复；
- ``expect_found=true`` 必须带 ``expect_source``，且该来源确实存在于
  ``data/raw``（防止标注指向不存在的语料）。

答案质量（可溯源 / 拒答）由 ``test_rag_generator.py`` 与真实链路自检覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
QA_SET_PATH = REPO_ROOT / "examples" / "qa_set.json"
RAW_DIR = REPO_ROOT / "data" / "raw"

TOTAL = 30
UNFOUND = 10


def _load_qa_set() -> list[dict]:
    data = json.loads(QA_SET_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, list), "qa_set.json 顶层必须是数组"
    return data


def test_qa_set_total_count() -> None:
    assert len(_load_qa_set()) == TOTAL


def test_qa_set_unanswerable_count() -> None:
    items = _load_qa_set()
    unfound = [d for d in items if d.get("expect_found") is False]
    assert len(unfound) == UNFOUND


def test_qa_set_answerable_count() -> None:
    items = _load_qa_set()
    found = [d for d in items if d.get("expect_found") is True]
    assert len(found) == TOTAL - UNFOUND


def test_qa_set_questions_valid_and_unique() -> None:
    items = _load_qa_set()
    questions = [d["question"] for d in items]
    assert all(isinstance(q, str) and q.strip() for q in questions)
    assert len(questions) == len(set(questions))


def test_qa_set_found_items_have_source() -> None:
    items = _load_qa_set()
    for d in items:
        if d.get("expect_found") is True:
            assert isinstance(d.get("expect_source"), str)
            assert d["expect_source"].strip()


def test_qa_set_sources_exist_in_raw() -> None:
    """``expect_source`` 必须对应 ``data/raw`` 中真实存在的文件名。"""
    raw_names = {p.name for p in RAW_DIR.iterdir() if p.is_file()}
    items = _load_qa_set()
    for d in items:
        if d.get("expect_found") is True:
            assert d["expect_source"] in raw_names, (
                f"expect_source={d['expect_source']!r} 不在 data/raw 中"
            )
