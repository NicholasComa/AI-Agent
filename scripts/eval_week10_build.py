"""生成并自检第十周 Golden Dataset（52 条）。

用法（Git Bash，先 ``export PATH="/c/Users/Xsz/.local/bin:$PATH"``，在项目根执行）::

    # 生成数据集（会覆盖 data/golden/week10_golden.jsonl）
    uv run python scripts/eval_week10_build.py

    # 只做自检与条数统计，不写文件
    uv run python scripts/eval_week10_build.py --check

取材说明与语料实况
------------------

服务侧读的集合由 ``AgentServiceSettings.collection_name`` 决定，键是
``AGENT_SERVICE_COLLECTION_NAME``，默认 **``jwipc_v3``**（51 条载荷）。注意
``.env`` 里的 ``QDRANT_COLLECTION_NAME=rag_chunks`` 属于**另一条链路**：它被
第 5/6 周的脚本（``src/config.py``）读取，服务不读它。两个名字指向不同集合，
取材前必须先确认看的是哪一个——看错集合会得出「语料里没有硬件手册」的错误结论。

``jwipc_v3`` 的构成与计划描述一致，是「硬件手册 + 概念文档 + 宋词/文学」的杂集：

| 来源 | 条数 |
| --- | --- |
| ``VT1000用户手册-V1.0--2025.02.19.pdf`` | 16 |
| ``S102H&S102HT 中英文简易使用指南--Rev1.0--2023.10.10.pdf`` | 10 |
| 六个 ``rag_*.md`` 概念文档 | 11 |
| 诗词与文学文件 | 14 |

因此 22 条知识问答按计划的要求分五组取材，**规格参数**与**跨机型对比**两类都能
从真实索引出题（计划里那条示例「VT1000 的供电电压」对应手册原文的
「输入电压 - DC IN 12V」）：

- ``spec``：单机型规格参数，取自两份硬件手册；
- ``cross_model``：VT1000 与 S102H 的同项对比；
- ``core``：六个概念文档上的问答，复用 ``examples/qa_set.json`` 的人工标注；
- ``known_noise``：诗词类问答。这些文件多为单条片段，且与其它语料语义相距较远，
  召回本就受语料构成影响；按计划要求**不改语料**，单列一组说明；
- ``refusal``：知识库中无对应内容的提问，期望模型明确拒答。

复用 ``examples/qa_set.json`` 的条目会**逐条核对来源是否与人工标注一致**，
不一致直接报错，避免两处标注悄悄分叉。

来源清单由 ``--check`` 一并打印，便于与集合的实际载荷对照。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))  # noqa: E402

from evaluation.dataset import (  # noqa: E402
    SCENARIO_QUOTAS,
    DatasetError,
    assert_dataset,
    describe_counts,
    describe_groups,
    dump_cases,
)

DEFAULT_OUT = PROJECT_ROOT / "data" / "golden" / "week10_golden.jsonl"
EXAMPLES_DIR = PROJECT_ROOT / "examples"

QA_CHECKS_ANSWERABLE = (
    "retrieval_hit@3",
    "citation_source_hit",
    "answer_point_coverage>=0.6",
    "refusal_correct",
)
QA_CHECKS_REFUSAL = ("refusal_correct",)

REQ_CHECKS = (
    "category_match",
    "clarification_expected",
    "functional_coverage>=0.6",
    "schema_valid_rate",
)

TOOL_CHECKS_OK = ("tool_selected", "args_schema_valid", "outcome_match")
TOOL_CHECKS_DENIED = (*TOOL_CHECKS_OK, "deny_enforced")


# ----------------------------------------------------------------------
# 知识问答：从 examples/qa_set.json 择优复用，再补分组与期望要点
# ----------------------------------------------------------------------


VT1000_SOURCE = "VT1000用户手册-V1.0--2025.02.19.pdf"
S102H_SOURCE = "S102H&S102HT 中英文简易使用指南--Rev1.0--2023.10.10.pdf"


@dataclass(frozen=True)
class QaPick:
    """一条知识问答的取材指令。

    Attributes:
        question: 问句。
        group: 分组，见模块文档。
        source: 期望来源文件名。**必须与线上集合里实际存在的来源逐字一致**，
            写错会变成一条注定失败的用例。
        expect_points: 期望要点。写作时的准则取自语料原文的关键词，而不是自己
            归纳的同义说法——要点匹配是子串包含，改写成同义词就永远匹配不上。
        note: 说明。
    """

    question: str
    group: str
    source: str
    expect_points: list[str] = field(default_factory=list)
    note: str = ""


QA_PICKS: tuple[QaPick, ...] = (
    # spec：单机型规格参数（计划要求补的一类）
    QaPick(
        "VT1000 的供电电压是多少？",
        "spec",
        VT1000_SOURCE,
        ["DC IN 12V", "DC Jack"],
        "规格参数-输入电压；计划里的示例行，已核对手册原文",
    ),
    QaPick(
        "VT1000 使用的是什么处理器？",
        "spec",
        VT1000_SOURCE,
        ["Jasper", "N5095"],
        "规格参数-CPU",
    ),
    QaPick(
        "VT1000 提供哪些显示与串口接口？",
        "spec",
        VT1000_SOURCE,
        ["HDMI", "DB9"],
        "规格参数-IO 接口",
    ),
    QaPick(
        "VT1000 支持哪些安装方式？",
        "spec",
        VT1000_SOURCE,
        ["上架式", "壁挂式"],
        "规格参数-安装方式",
    ),
    QaPick(
        "S102H 的电源输入是多少伏？",
        "spec",
        S102H_SOURCE,
        ["19V", "DC IN"],
        "规格参数-输入电压",
    ),
    QaPick(
        "S102H 的内存最大支持多少？",
        "spec",
        S102H_SOURCE,
        ["SO-DIMM", "32GB"],
        "规格参数-内存上限",
    ),
    # cross_model：跨机型对比（计划要求补的一类）
    QaPick(
        "VT1000 与 S102H 的输入电压分别是多少？",
        "cross_model",
        VT1000_SOURCE,
        ["12V", "19V"],
        "跨机型对比-供电；两机型数字必须都出现",
    ),
    QaPick(
        "VT1000 与 S102H 的内存插槽配置有何不同？",
        "cross_model",
        S102H_SOURCE,
        ["1 x SO-DIMM", "2 x SO-DIMM"],
        "跨机型对比-内存插槽数",
    ),
    QaPick(
        "VT1000 与 S102H 在显示接口上有哪些差异？",
        "cross_model",
        S102H_SOURCE,
        ["HDMI", "DP"],
        "跨机型对比-显示接口",
    ),
    # core：概念文档问答，复用 examples/qa_set.json 的人工标注
    QaPick(
        "RAG 的全称是什么？",
        "core",
        "rag_concepts.md",
        ["Retrieval-Augmented Generation", "检索增强生成", "外部知识库"],
        "事实型-术语",
    ),
    QaPick(
        "Overlap 重叠的作用是什么？",
        "core",
        "rag_concepts.md",
        ["片段边界", "切断", "冗余"],
        "事实型-概念",
    ),
    QaPick(
        "Qdrant 的 Point 由哪几部分组成？",
        "core",
        "qdrant_basics.md",
        ["id", "vector", "payload"],
        "事实型-概念",
    ),
    QaPick(
        "RAG 管道的五个阶段是什么？",
        "core",
        "rag_pipeline.md",
        ["导入", "切分", "向量化", "检索", "生成"],
        "事实型-流程",
    ),
    QaPick(
        "Recall@K 指标是如何计算的？",
        "core",
        "rag_evaluation.md",
        ["命中", "占比", "期望来源"],
        "事实型-评测",
    ),
    QaPick(
        "Embedding 模型常见的向量维度有哪些？",
        "core",
        "embedding_models.md",
        ["384", "768", "1024"],
        "事实型-概念",
    ),
    # known_noise：诗词类，单片段文件且与其它语料语义相距远
    QaPick(
        "红豆生南国 出自哪位诗人的哪首诗？",
        "known_noise",
        "poem_tang_相思_王维.md",
        ["王维", "相思"],
        "诗词-唐诗；语料含大量无关文本",
    ),
    QaPick(
        "千里澄江似练，翠峰如簇 出自哪首词？",
        "known_noise",
        "poem_ci_桂枝香_王安石.md",
        ["王安石", "桂枝香"],
        "诗词-宋词",
    ),
    QaPick(
        "我住长江头，君住长江尾 是哪位词人的作品？",
        "known_noise",
        "poem_ci_卜算子_李之仪.md",
        ["李之仪", "卜算子"],
        "诗词-宋词",
    ),
    # refusal：知识库无对应内容，期望拒答
    QaPick(
        "君不见黄河之水天上来 出自李白的哪首诗？",
        "refusal",
        "",
        [],
        "知识库无此诗-期望拒答",
    ),
    QaPick(
        "2024 年巴黎奥运会开幕式的举办日期是？",
        "refusal",
        "",
        [],
        "知识库无对应片段-期望拒答",
    ),
    QaPick(
        "公司食堂中午的营业时间是什么？",
        "refusal",
        "",
        [],
        "知识库无对应片段-期望拒答",
    ),
    QaPick(
        "Windows 12 的正式发布日期是哪天？",
        "refusal",
        "",
        [],
        "知识库无对应片段-期望拒答",
    ),
)


def build_knowledge_qa(examples: Path = EXAMPLES_DIR) -> list[dict]:
    """按 :data:`QA_PICKS` 生成 22 条知识问答。

    复用 ``examples/qa_set.json`` 的条目会核对来源是否一致：那份文件是人工标注
    的 ground truth，如果这里的来源写成了另一个文件，说明取材时看错了集合或
    语料已变，必须在冻结前暴露出来。
    """
    pool_path = examples / "qa_set.json"
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    by_question = {item["question"]: item for item in pool}

    cases: list[dict] = []
    for index, pick in enumerate(QA_PICKS, start=1):
        expect_found = bool(pick.source)
        annotated = by_question.get(pick.question)
        if annotated is not None:
            # 拒答题在 qa_set.json 里没有 expect_source 键，取值是 None；与取材侧
            # 的空串是同一件事，比较前先归一，否则会被当成不一致。
            annotated_source = annotated.get("expect_source") or ""
            if annotated_source != pick.source:
                raise DatasetError(
                    f"{pool_path} 的人工标注与取材不一致：{pick.question}\n"
                    f"  标注={annotated_source!r}，取材={pick.source!r}"
                )
            expect_found = bool(annotated["expect_found"])
        if expect_found and not pick.source:
            raise DatasetError(f"{pick.question} 标为可回答但没有给出来源")
        cases.append(
            {
                "id": f"qa-{index:03d}",
                "scenario": "knowledge_qa",
                "group": pick.group,
                "input": {"query": pick.question, "top_k": 3, "min_score": 0.0},
                "reference": {
                    "expect_found": expect_found,
                    "expect_sources": [pick.source] if expect_found else [],
                    "expect_points": list(pick.expect_points),
                },
                "checks": list(QA_CHECKS_ANSWERABLE if expect_found else QA_CHECKS_REFUSAL),
                "note": pick.note,
            }
        )
    return cases


# ----------------------------------------------------------------------
# 需求分析：5 条基线样本 + 13 条扩写
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ReqSpec:
    """一条需求分析用例的取材指令。"""

    group: str
    text: str
    expect_category: str | None
    expect_needs_clarify: bool
    expect_points: list[str] = field(default_factory=list)
    note: str = ""


REQ_EXTRA: tuple[ReqSpec, ...] = (
    # 正常需求（5 条扩写）
    ReqSpec(
        "normal",
        "开发一个企业内部考勤管理系统，支持员工打卡（GPS 定位）、请假审批流程、"
        "加班申请、月度考勤报表导出 Excel，管理员可以配置考勤规则和节假日。",
        "web",
        False,
        ["打卡", "请假", "加班", "报表"],
        "正常需求-内部管理系统",
    ),
    ReqSpec(
        "normal",
        "做一个移动端记账 App，支持手动记账、账单分类、月度统计图表、预算提醒，"
        "数据本地存储并支持导出 CSV。",
        "mobile",
        False,
        ["记账", "分类", "统计", "导出"],
        "正常需求-移动端",
    ),
    ReqSpec(
        "normal",
        "搭建一个 REST API 服务，提供用户注册登录、JWT 鉴权、文章增删改查、"
        "分页与关键字搜索，并输出 OpenAPI 文档。",
        "api",
        False,
        ["鉴权", "增删改查", "分页", "搜索"],
        "正常需求-接口服务",
    ),
    ReqSpec(
        "normal",
        "做一个运营数据看板，从 MySQL 读取订单数据，按日/周/月展示销售额、"
        "订单量、客单价趋势，支持导出图片。",
        "data",
        False,
        ["订单", "趋势", "导出"],
        "正常需求-数据看板",
    ),
    ReqSpec(
        "normal",
        "开发一个桌面端文件整理工具，按扩展名自动分类到子目录，支持批量重命名、"
        "重复文件检测与操作预览。",
        "desktop",
        False,
        ["分类", "重命名", "重复", "预览"],
        "正常需求-桌面工具",
    ),
    # 含糊需求（3 条扩写）
    ReqSpec("ambiguous", "帮我弄个东西，越快越好。", None, True, [], "含糊需求-无任何有效信息"),
    ReqSpec("ambiguous", "这个能不能做成那种很智能的？", None, True, [], "含糊需求-只有形容词"),
    ReqSpec("ambiguous", "参考一下同行，做个类似的就行了。", None, True, [], "含糊需求-指代缺失"),
    # 缺失信息（2 条扩写）
    ReqSpec(
        "missing_info",
        "给客户做一个管理平台，功能你看着办。",
        None,
        True,
        [],
        "缺失信息-用户与场景缺失",
    ),
    ReqSpec(
        "missing_info",
        "做个小程序，先出个原型。",
        None,
        True,
        [],
        "缺失信息-业务领域缺失",
    ),
    # 无关文本（2 条扩写）
    ReqSpec("irrelevant", "中午吃什么好呢？", "other", True, [], "无关文本-闲聊"),
    ReqSpec("irrelevant", "周末想去爬山，有推荐的路线吗？", "other", True, [], "无关文本-生活话题"),
    # 超长需求（1 条扩写）
    ReqSpec(
        "very_long",
        "需要建设一个覆盖全省的智慧水务监测平台，接入 3200 个管网测点，采样频率 5 分钟一次，"
        "数据保留 5 年。功能包括：实时监测（压力、流量、浊度、余氯四类指标，超阈值自动告警，"
        "告警通过短信与站内消息下发）、历史数据查询（任意时间段曲线对比、导出 Excel）、"
        "管网拓扑图（按片区着色，点击测点查看详情）、爆管分析（基于压力突变定位疑似爆管区段）、"
        "巡检工单（移动端接单、拍照上传、GPS 打卡、完工确认）、报表中心（日报、月报、"
        "水质达标率统计，支持定时推送）。技术上要求前后端分离，后端 Spring Boot 3 + "
        "PostgreSQL + TimescaleDB 时序扩展 + Redis 缓存 + Kafka 承接测点上报，前端 Vue3 + ECharts，"
        "部署在政务云 K8s 集群，需通过等保三级测评，提供 Grafana 监控与 Prometheus 告警，"
        "并要求灰度发布与回滚方案。项目预算 480 万，工期 9 个月，团队 12 人。",
        "web",
        False,
        ["监测", "告警", "查询", "工单", "报表"],
        "超长需求-需在字段约束内归纳",
    ),
)


def build_requirement_analysis(examples: Path = EXAMPLES_DIR) -> list[dict]:
    """基线 5 类 + 扩写 13 条，共 18 条。"""
    base_path = examples / "requirement_samples.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))

    base_specs = (
        ReqSpec(
            "normal",
            base["normal"]["text"],
            "web",
            False,
            ["登录", "商品", "购物车", "订单", "支付"],
            "正常需求-基线样本",
        ),
        ReqSpec("ambiguous", base["ambiguous"]["text"], None, True, [], "含糊需求-基线样本"),
        ReqSpec(
            "missing_info",
            base["missing_info"]["text"],
            None,
            True,
            ["推荐"],
            "缺失信息-基线样本",
        ),
        ReqSpec("irrelevant", base["irrelevant"]["text"], "other", True, [], "无关文本-基线样本"),
        ReqSpec(
            "very_long",
            base["very_long"]["text"],
            "web",
            False,
            ["登录", "商品", "购物车", "支付", "订单"],
            "超长需求-基线样本",
        ),
    )

    cases: list[dict] = []
    for index, spec in enumerate((*base_specs, *REQ_EXTRA), start=1):
        cases.append(
            {
                "id": f"req-{index:03d}",
                "scenario": "requirement_analysis",
                "group": spec.group,
                "input": {"text": spec.text},
                "reference": {
                    "expect_category": spec.expect_category,
                    "expect_needs_clarify": spec.expect_needs_clarify,
                    "expect_points": list(spec.expect_points),
                },
                "checks": list(REQ_CHECKS),
                "note": spec.note,
            }
        )
    return cases


# ----------------------------------------------------------------------
# 工具调用：4 个工具的合规与越权调用，共 12 条
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """一条工具调用用例的取材指令。"""

    tool: str
    arguments: dict
    expect_outcome: str
    expect_deny_kind: str | None = None
    note: str = ""


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("list_files", {"path": ".", "limit": 10}, "ok", note="正常调用-沙箱根"),
    ToolSpec("list_files", {"path": "src", "limit": 5}, "ok", note="正常调用-子目录"),
    ToolSpec("list_files", {"path": "../.."}, "denied", "forbidden", note="越权-路径穿越"),
    ToolSpec("read_file", {"path": "pyproject.toml"}, "ok", note="正常调用-小文件"),
    ToolSpec(
        "read_file",
        {"path": "README.md"},
        "needs_confirmation",
        note="确认门-超过 50KB 读取闸门（README 约 58KB）",
    ),
    ToolSpec(
        "read_file", {"path": "../../etc/passwd"}, "denied", "forbidden", note="越权-路径穿越"
    ),
    ToolSpec("read_file", {"path": "/etc/passwd"}, "denied", "bad_path", note="越权-绝对路径"),
    ToolSpec("git_log", {"path": ".", "limit": 5}, "ok", note="正常调用-读取提交历史"),
    ToolSpec("git_log", {"path": "../"}, "denied", "forbidden", note="越权-沙箱外目录"),
    ToolSpec(
        "check_commit_message",
        {"message": "func: app: Add golden dataset builder"},
        "ok",
        note="业务校验-合规提交信息",
    ),
    ToolSpec(
        "check_commit_message",
        {"message": "update something"},
        "invalid",
        note="业务校验-缺少 type",
    ),
    ToolSpec(
        "check_commit_message",
        {"message": "docs: 更新第十周文档"},
        "invalid",
        note="业务校验-缺少 scope",
    ),
)

_GROUP_OF: dict[str, str] = {
    "ok": "allowed",
    "denied": "denied",
    "needs_confirmation": "gated",
    "invalid": "invalid",
    "failed": "failed",
}
"""结局到报告分组的映射。分组名与结局不必同名，但必须一一对应，否则按组统计会串。"""


def build_tool_call() -> list[dict]:
    """按 :data:`TOOL_SPECS` 生成 12 条工具调用用例。"""
    cases: list[dict] = []
    for index, spec in enumerate(TOOL_SPECS, start=1):
        denied = spec.expect_outcome == "denied"
        cases.append(
            {
                "id": f"tool-{index:03d}",
                "scenario": "tool_call",
                "group": _GROUP_OF[spec.expect_outcome],
                "input": {"tool": spec.tool, "arguments": spec.arguments},
                "reference": {
                    "expect_tool": spec.tool,
                    "expect_outcome": spec.expect_outcome,
                    "expect_deny_kind": spec.expect_deny_kind,
                },
                "checks": list(TOOL_CHECKS_DENIED if denied else TOOL_CHECKS_OK),
                "note": spec.note,
            }
        )
    return cases


def build_all() -> list[dict]:
    """生成完整 52 条。"""
    return [
        *build_knowledge_qa(),
        *build_requirement_analysis(),
        *build_tool_call(),
    ]


def _print_sources(cases: list[dict]) -> None:
    """打印知识问答取材涉及的来源，便于与线上集合载荷对照。"""
    sources = sorted(
        {
            source
            for case in cases
            if case["scenario"] == "knowledge_qa"
            for source in case["reference"]["expect_sources"]
        }
    )
    print(f"知识问答涉及的来源（{len(sources)} 个）：")
    for name in sources:
        print(f"  - {name}")


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="生成并自检第十周 Golden Dataset")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出 JSONL 路径")
    parser.add_argument("--check", action="store_true", help="只自检，不写文件")
    args = parser.parse_args(argv)

    raw = build_all()
    try:
        # 先借临时文件走一遍与落盘完全相同的 Pydantic 校验，避免「生成时是
        # 字典、读回时才爆炸」这种把问题推迟到下一环节的写法。
        cases = validate(raw)
    except DatasetError as exc:
        print(f"[FAIL] 生成结果未通过校验：{exc}")
        return 1

    counts = describe_counts(cases)
    if counts != dict(SCENARIO_QUOTAS):
        print(f"[FAIL] 场景条数与计划不符：实际 {counts}，期望 {dict(SCENARIO_QUOTAS)}")
        return 1
    print(f"[PASS] 生成 {len(cases)} 条：{counts}")

    if not args.check:
        out = dump_cases(cases, args.out)
        print(f"[PASS] 已写入 {out}")

    print("[PASS] 自检通过：id 唯一、场景条数达标、检查项可被指标引擎识别")
    print(f"分组：{describe_groups(cases)}")
    _print_sources(raw)
    return 0


def validate(cases: list[dict]) -> list:
    """把字典写进临时文件后走一遍完整校验，返回 Pydantic 模型列表。

    临时文件在系统临时目录里，用完即删——``--check`` 的语义是「不写文件」，
    这里不能为了复用校验而在仓库里留下残件。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "golden.jsonl"
        path.write_text(
            "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
            encoding="utf-8",
        )
        return assert_dataset(path)


if __name__ == "__main__":
    raise SystemExit(main())
