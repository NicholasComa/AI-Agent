"""数据集模型与自检测试。"""

from __future__ import annotations

import pytest
from evaluation_fakes import DATASET_PATH, qa_case, req_case, tool_case, write_jsonl

from evaluation.dataset import (
    KNOWN_TOOLS,
    SCENARIO_QUOTAS,
    SCENARIOS,
    TOTAL_MIN,
    DatasetError,
    assert_dataset,
    describe_counts,
    describe_groups,
    dump_cases,
    load_cases,
)

# ----------------------------------------------------------------------
# 冻结的那份数据集
# ----------------------------------------------------------------------


def test_frozen_dataset_loads_and_meets_quotas():
    """交付的数据集必须能加载、条数达标且 id 唯一。"""
    cases = assert_dataset(DATASET_PATH)

    assert len(cases) >= TOTAL_MIN
    counts = describe_counts(cases)
    for scenario, quota in SCENARIO_QUOTAS.items():
        assert counts[scenario] >= quota, f"{scenario} 条数不足：{counts[scenario]} < {quota}"


def test_frozen_dataset_has_only_known_scenarios():
    """场景字段只能是登记的那三个。"""
    cases = load_cases(DATASET_PATH)

    assert {case.scenario for case in cases} == set(SCENARIOS)


def test_frozen_dataset_groups_are_reported():
    """分组统计包含语料构成相关的那几组。"""
    groups = describe_groups(load_cases(DATASET_PATH))

    assert groups["knowledge_qa/known_noise"] > 0
    assert groups["knowledge_qa/refusal"] > 0
    assert groups["tool_call/denied"] > 0


def test_frozen_dataset_tools_are_all_registered():
    """工具用例只能引用真实注册的工具。"""
    cases = load_cases(DATASET_PATH)
    used = {case.input.tool for case in cases if case.scenario == "tool_call"}

    assert used <= set(KNOWN_TOOLS)


def test_every_case_carries_checks():
    """每条用例都必须至少有一个检查项，否则它在报告里永远「通过」。"""
    cases = load_cases(DATASET_PATH)

    assert all(case.checks for case in cases)


# ----------------------------------------------------------------------
# 校验规则
# ----------------------------------------------------------------------


def test_unknown_field_is_rejected(tmp_path):
    """字段名写错必须报错，而不是被静默忽略。"""
    row = qa_case()
    row["reference"]["expect_source"] = "typo.md"  # 正确的是 expect_sources

    path = write_jsonl(tmp_path / "d.jsonl", [row])

    with pytest.raises(DatasetError, match="校验失败"):
        load_cases(path)


def test_expect_found_true_requires_sources(tmp_path):
    """自相矛盾的标注被拦住。"""
    row = qa_case()
    row["reference"]["expect_sources"] = []
    path = write_jsonl(tmp_path / "d.jsonl", [row])

    with pytest.raises(DatasetError):
        load_cases(path)


def test_refusal_case_must_not_declare_sources(tmp_path):
    """拒答题给了期望来源同样是自相矛盾。"""
    row = qa_case()
    row["reference"]["expect_found"] = False
    path = write_jsonl(tmp_path / "d.jsonl", [row])

    with pytest.raises(DatasetError):
        load_cases(path)


def test_unknown_tool_is_rejected(tmp_path):
    """工具名不在清单里直接报错。"""
    row = tool_case()
    row["input"]["tool"] = "rm_rf"
    path = write_jsonl(tmp_path / "d.jsonl", [row])

    with pytest.raises(DatasetError):
        load_cases(path)


def test_denied_case_requires_deny_kind(tmp_path):
    """期望被拒却不写拒绝类别，报告里就无法说明「被什么拒了」。"""
    row = tool_case()
    row["reference"].pop("expect_deny_kind")
    path = write_jsonl(tmp_path / "d.jsonl", [row])

    with pytest.raises(DatasetError):
        load_cases(path)


def test_ok_case_must_not_declare_deny_kind(tmp_path):
    """正常返回的用例不该带拒绝类别。"""
    row = tool_case()
    row["reference"]["expect_outcome"] = "ok"
    path = write_jsonl(tmp_path / "d.jsonl", [row])

    with pytest.raises(DatasetError):
        load_cases(path)


def test_scenario_discriminates_input_shape(tmp_path):
    """按 scenario 判别：给需求用例塞知识问答的输入必须失败。"""
    row = req_case()
    row["input"] = {"query": "错场景的输入"}
    path = write_jsonl(tmp_path / "d.jsonl", [row])

    with pytest.raises(DatasetError):
        load_cases(path)


def test_missing_file_raises(tmp_path):
    """文件不存在时报明确错误。"""
    with pytest.raises(DatasetError, match="不存在"):
        load_cases(tmp_path / "nope.jsonl")


def test_empty_file_raises(tmp_path):
    """空文件不是合法数据集。"""
    path = tmp_path / "d.jsonl"
    path.write_text("\n\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="为空"):
        load_cases(path)


def test_bad_json_line_reports_line_number(tmp_path):
    """坏行报错要带行号——标注是手写的，没有行号就只能肉眼数。

    首行必须是一条**能通过校验**的用例，否则会在第 1 行先因缺 ``scenario``
    失败，第 2 行的 JSON 解析错误根本走不到，断言就测不到行号。
    """
    path = write_jsonl(tmp_path / "d.jsonl", [qa_case()])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")

    with pytest.raises(DatasetError, match=":2 "):
        load_cases(path)


# ----------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------


def _minimal_dataset(tmp_path, **overrides):
    """构造一份刚刚达标的数据集，供自检用例做扰动。"""
    rows = []
    for scenario, quota in SCENARIO_QUOTAS.items():
        template = {
            "knowledge_qa": qa_case,
            "requirement_analysis": req_case,
            "tool_call": tool_case,
        }[scenario]
        for index in range(
            overrides.get("gap", 0) if scenario == overrides.get("gap_in") else quota
        ):
            row = template()
            row["id"] = f"{scenario}-{index:03d}"
            if scenario == "knowledge_qa":
                row["checks"] = ["retrieval_hit@3"]
            elif scenario == "requirement_analysis":
                row["checks"] = ["category_match"]
            else:
                row["checks"] = ["outcome_match"]
            rows.append(row)
    return write_jsonl(tmp_path / "min.jsonl", rows)


def test_assert_dataset_accepts_minimal_dataset(tmp_path):
    """刚刚达标的数据集自检通过。"""
    assert len(assert_dataset(_minimal_dataset(tmp_path))) == sum(SCENARIO_QUOTAS.values())


def test_assert_dataset_rejects_duplicate_ids(tmp_path):
    """id 重复会被抓出来，并指出冲突的两行。"""
    path = _minimal_dataset(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[0]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="id 重复"):
        assert_dataset(path)


def test_assert_dataset_rejects_short_scenario(tmp_path):
    """某个场景条数不足会被抓出来。"""
    path = _minimal_dataset(tmp_path, gap_in="tool_call", gap=3)

    with pytest.raises(DatasetError, match="tool_call"):
        assert_dataset(path)


def test_assert_dataset_rejects_unparsable_check(tmp_path):
    """检查项语法不可解析时自检失败。"""
    path = _minimal_dataset(tmp_path)
    rows = path.read_text(encoding="utf-8").splitlines()
    rows[0] = rows[0].replace('"retrieval_hit@3"', '"retrieval_hit@k"')
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="无法解析"):
        assert_dataset(path)


def test_assert_dataset_rejects_foreign_check(tmp_path):
    """检查项不属于该场景时自检失败——否则报告里会出现一个永远 skip 的列。"""
    path = _minimal_dataset(tmp_path)
    rows = path.read_text(encoding="utf-8").splitlines()
    rows[0] = rows[0].replace('"retrieval_hit@3"', '"deny_enforced"')
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="不属于该场景"):
        assert_dataset(path)


# ----------------------------------------------------------------------
# 落盘
# ----------------------------------------------------------------------


def test_dump_then_load_round_trips(tmp_path):
    """写盘再读回，对象等价。"""
    original = load_cases(DATASET_PATH)
    path = dump_cases(original, tmp_path / "out.jsonl")

    reloaded = load_cases(path)

    assert [case.id for case in reloaded] == [case.id for case in original]
    assert reloaded[0].model_dump() == original[0].model_dump()
