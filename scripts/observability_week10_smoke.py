"""可观测性冒烟：按 ``.env`` 真实配置跑一条完整 trace，并核对记录落点。

用法::

    # 只跑本地后端，读回 JSONL 验证落盘格式
    OBS_BACKEND=local uv run python scripts/observability_week10_smoke.py

    # 按 .env 配置跑（默认 langfuse），并轮询公开 API 确认实例真的收到
    uv run python scripts/observability_week10_smoke.py --verify-wait 60

    # 固定种子后缀，复现上一次那几条 trace
    uv run python scripts/observability_week10_smoke.py --seed-suffix demo

覆盖下面这些点：

- 配置解析结果：后端选择、端点、凭据是否齐全、是否发生降级及原因；
- 嵌套 span 的父子链、耗时与用量折算；
- 异常路径：span 与根 span 都标错误，且异常照常向上抛；
- 提前退出留下的未结束 span 被收尾成错误并标记 ``unfinished``；
- 元数据清洗：敏感键不进入记录；
- 落点核对：本地后端读回 JSONL 行；远端后端轮询公开 API，核对 8 条记录的名称、
  类型、错误级别与 trace 分组。

脚本开头调用 ``load_dotenv`` 把仓库根 ``.env`` 读进环境变量。这一步对远端
后端是必需的：远端 SDK 自己从环境变量取端点与凭据，不读本项目的配置对象，
少了它 SDK 会回落到官方默认的云端端点。

关于「只上传元数据」
--------------------

远端核对走的是 v4 的 ``/api/public/v2/observations``。该端点不返回正文，观测表
本身也没有 input/output 列，因此这里**不能**用它证明「没上传正文」——它证明的
是记录结构与分级正确。要实证正文边界，直接查原始事件表更可靠：

.. code-block:: sql

    SELECT count() FROM default.events_full
    WHERE position(status_message, '<敏感串>') > 0;

``events_full`` 存的是落库前的原始事件，命中数为 0 才算没泄露。注意要同时搜一个
**预期存在**的串作为对照（例如异常消息会进 ``status_message``），否则无法排除
「搜索本身失效」。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for path in (PROJECT_ROOT / "src", PROJECT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from observability import ObservabilityConfig, describe  # noqa: E402

_FAILED = 0

# 故意带一个敏感键：sanitize_attributes 应当把它丢掉
SENSITIVE_KEY = "prompt"
SENSITIVE_VALUE = "这段提示词不应出现在任何记录里"

# 场景应当产出的 8 条记录：名称到远端 observation 类型的映射。
# 名称与类型都要对上，只数条数无法发现类型映射写错。
EXPECTED_OBSERVATIONS = {
    "smoke.request": "SPAN",
    "retrieve": "RETRIEVER",
    "generate": "GENERATION",
    "tool_check_commit": "TOOL",
    "smoke.error": "SPAN",
    "broken.tool": "TOOL",
    "smoke.unfinished": "SPAN",
    "dropped.step": "CHAIN",
}

# 这三条应当以 ERROR 级别落库：体内抛异常的两条，以及被 flush 收尾的那条
ERROR_OBSERVATIONS = frozenset({"smoke.error", "broken.tool", "smoke.unfinished"})

# 三条 trace 各自的预期记录数，用来核对分组没串。
# 键是 ``_run_scenario`` 里给 ``trace()`` / ``start_trace()`` 的 seed 前缀，不是
# observation 名：正常链的 seed 是 ``smoke-ok-<后缀>``，而它的根 span 名为
# ``smoke.request``。界面上只看得到后者，不存在名为 ``smoke-ok`` 的条目。
EXPECTED_TRACE_SIZES = {
    "smoke-ok": 4,
    "smoke-error": 2,
    "smoke-unfinished": 2,
}


def _ok(label: str, detail: str = "") -> None:
    """记录一条通过项。"""
    print(f"  [OK]   {label}" + (f"  {detail}" if detail else ""))


def _fail(label: str, detail: str = "") -> None:
    """记录一条失败项。"""
    global _FAILED
    _FAILED += 1
    print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


def _run_scenario(tracer: Any, suffix: str) -> dict[str, Any]:
    """跑三条路径并返回关键标识。

    三条路径分别是正常完成、体内抛异常、以及根 span 不收尾——后者用来验证
    ``flush`` 会把遗留 span 收尾成错误状态，而不是静默丢弃。

    ``suffix`` 拼进种子，让每次运行落在各自独立的 trace 上。种子决定 trace
    标识，若不加后缀，重复运行会把新记录并进上一次那几条 trace，远端按标识
    过滤出来的条数就会翻倍，计数核对随之失效。
    """
    # 1. 正常路径：一条 trace 里嵌三种 span
    with tracer.trace("smoke.request", seed=f"smoke-ok-{suffix}", session_id="smoke") as root:
        with tracer.span(
            "retrieve",
            kind="retriever",
            top_k=3,
            strategy="vector",
            **{SENSITIVE_KEY: SENSITIVE_VALUE},
        ):
            tracer.set_attributes(top1_score=0.87)
        with tracer.span("generate", kind="generation"):
            tracer.set_usage(model="qwen3:1.7b", input_tokens=120, output_tokens=48)
        with tracer.span("tool_check_commit", kind="tool"):
            pass

    # 2. 异常路径：异常必须继续向上抛，记录同时标成错误
    error_trace_id = None
    try:
        with tracer.trace("smoke.error", seed=f"smoke-error-{suffix}") as failing:
            error_trace_id = failing.trace_id
            with tracer.span("broken.tool", kind="tool"):
                msg = "smoke 故意抛出的异常"
                raise RuntimeError(msg)
    except RuntimeError:
        pass

    # 3. 未收尾路径：只开根 span，不调 end_trace
    tracer.start_trace("smoke.unfinished", seed=f"smoke-unfinished-{suffix}")
    with tracer.span("dropped.step", kind="chain"):
        pass

    return {
        "ok_trace_id": root.trace_id,
        "error_trace_id": error_trace_id or "",
        "unfinished_trace_id": tracer.current_trace_id() or "",
    }


def _read_jsonl(trace_dir: Path) -> list[dict[str, Any]]:
    """读回落盘记录；文件不存在或行损坏都只跳过。"""
    records: list[dict[str, Any]] = []
    for path in sorted(trace_dir.glob("traces-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _verify_local(config: ObservabilityConfig) -> bool:
    """核对本地 JSONL：三种 span 都在，敏感键被丢弃。"""
    records = _read_jsonl(config.trace_dir)
    if not records:
        _fail("本地落盘", f"{config.trace_dir} 下没有记录")
        return False

    names = {record["name"] for record in records}
    expect = {"smoke.request", "retrieve", "generate", "tool_check_commit", "broken.tool"}
    missing = expect - names
    if missing:
        _fail("本地落盘", f"缺少记录 {sorted(missing)}")
        return False
    _ok("本地落盘", f"{len(records)} 条记录，覆盖 {len(expect)} 个 span")

    leaked = [
        record["name"] for record in records if SENSITIVE_KEY in (record.get("attributes") or {})
    ]
    if leaked:
        _fail("敏感键清洗", f"这些 span 仍带 {SENSITIVE_KEY}：{leaked}")
    else:
        _ok("敏感键清洗", f"{SENSITIVE_KEY} 未进入任何记录")

    unfinished = [record["name"] for record in records if record.get("unfinished")]
    if "smoke.unfinished" in unfinished:
        _ok("未收尾 span", f"被收尾并标记：{unfinished}")
    else:
        _fail("未收尾 span", f"未找到 unfinished 标记，实际为 {unfinished}")
    return True


def _fetch_health(config: ObservabilityConfig) -> httpx.Response:
    """查健康端点。

    ``trust_env=False`` 是必需的：httpx 默认会读 ``http_proxy`` 环境变量，把
    指向 ``127.0.0.1`` 的请求交给代理，结果拿到空响应而不是真实状态码。
    """
    url = f"{config.langfuse_base_url.rstrip('/')}/api/public/health"
    with httpx.Client(trust_env=False, timeout=10.0) as client:
        return client.get(url)


def _fetch_observations(config: ObservabilityConfig, limit: int = 50) -> list[dict[str, Any]]:
    """取回最近一批 observation。

    v4 的部署运行在 ``events_only`` 模式下，旧的 ``/api/public/traces`` 端点已
    经不存在（返回 404），读取能力改由 ``/api/public/v2/observations`` 提供。
    端点选错会表现为「入库失败」，实际数据已经进库。
    """
    url = f"{config.langfuse_base_url.rstrip('/')}/api/public/v2/observations"
    with httpx.Client(trust_env=False, timeout=15.0) as client:
        response = client.get(
            url,
            params={"limit": limit},
            auth=(config.langfuse_public_key, config.langfuse_secret_key),
        )
    response.raise_for_status()
    payload = response.json()
    return payload.get("data", []) if isinstance(payload, dict) else []


def _verify_remote(config: ObservabilityConfig, trace_ids: dict[str, str], wait: float) -> bool:
    """轮询公开 API，核对记录的名称、类型、错误级别与 trace 分组。"""
    known = {trace_id for trace_id in trace_ids.values() if trace_id}
    if not known:
        _fail("远端核对", "没有可核对的 trace 标识")
        return False

    deadline = time.monotonic() + wait
    rows: list[dict[str, Any]] = []
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            fetched = _fetch_observations(config)
        except (httpx.HTTPError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(3)
            continue
        rows = [row for row in fetched if str(row.get("traceId")) in known]
        if set(EXPECTED_OBSERVATIONS) <= {str(row.get("name")) for row in rows}:
            break
        time.sleep(3)

    if not rows:
        _fail("远端入库", f"{wait:.0f} 秒内未取到记录，最后异常 {last_error}")
        return False

    found = {str(row.get("name")): row for row in rows}
    missing = set(EXPECTED_OBSERVATIONS) - set(found)
    if missing:
        _fail("远端入库", f"{wait:.0f} 秒内仍缺少记录：{sorted(missing)}")
        return False
    _ok("远端入库", f"{len(EXPECTED_OBSERVATIONS)} 条记录全部可取回")

    wrong_type = {
        name: found[name].get("type")
        for name, expect in EXPECTED_OBSERVATIONS.items()
        if found[name].get("type") != expect
    }
    if wrong_type:
        _fail("类型映射", f"与预期不符：{wrong_type}")
    else:
        _ok("类型映射", "SPAN / RETRIEVER / GENERATION / TOOL / CHAIN 均正确")

    not_error = {
        name: found[name].get("level")
        for name in ERROR_OBSERVATIONS
        if found[name].get("level") != "ERROR"
    }
    if not_error:
        _fail("错误分级", f"这些记录不是 ERROR：{not_error}")
    else:
        _ok("错误分级", f"{sorted(ERROR_OBSERVATIONS)} 均为 ERROR")

    grouped: dict[str, int] = {}
    for row in rows:
        key = str(row.get("traceId"))
        grouped[key] = grouped.get(key, 0) + 1
    if sorted(grouped.values()) != sorted(EXPECTED_TRACE_SIZES.values()):
        _fail(
            "trace 分组",
            f"各组记录数 {sorted(grouped.values())}，预期 {sorted(EXPECTED_TRACE_SIZES.values())}",
        )
    else:
        _ok("trace 分组", "三条 trace 分别为 4 / 2 / 2 条记录")

    # 该端点不返回正文；仅在字段出现时判定，避免把「字段缺省」误判成泄露
    leaked = {
        str(row.get("name")): key
        for row in rows
        for key in ("input", "output")
        if row.get(key) not in (None, "", [], {})
    }
    if leaked:
        _fail("正文边界", f"观测记录携带了正文字段：{leaked}")
    else:
        _ok("正文边界", "观测记录不含正文（该端点本身也不返回正文）")
    return True


async def _main() -> int:
    parser = argparse.ArgumentParser(description="可观测性冒烟")
    parser.add_argument(
        "--verify-wait",
        type=float,
        default=45.0,
        help="远端后端下等待记录入库的最长秒数；0 表示跳过入库核对",
    )
    parser.add_argument(
        "--seed-suffix",
        default="",
        help="固定本次运行的种子后缀，用于复现同一条 trace；缺省取当前时间戳",
    )
    args = parser.parse_args()

    suffix = args.seed_suffix or str(int(time.time()))

    # 必须在读配置之前执行：远端 SDK 自己从环境变量取端点与凭据
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

    config = ObservabilityConfig.from_env()
    summary = describe(config)

    print("== 配置 ==")
    for key, value in summary.items():
        print(f"  {key:<28} {value}")

    if not config.enabled:
        _fail("总开关", "OBS_ENABLED 为假，全部追踪 API 退化为空操作")
        return 1
    _ok("总开关", "已启用")

    from observability import build_tracer

    tracer = build_tracer(config)
    print("== 后端 ==")
    _ok("后端选择", f"{tracer.backend_name}")
    if tracer.degraded_reason:
        _ok("降级原因", tracer.degraded_reason)

    if tracer.backend_name == "langfuse":
        try:
            health = _fetch_health(config)
        except httpx.HTTPError as exc:
            _fail("实例健康检查", f"{type(exc).__name__}: {exc}")
            return 1
        if health.status_code == 200:
            _ok("实例健康检查", f"{health.json().get('version', '?')} @ {config.langfuse_base_url}")
        else:
            _fail("实例健康检查", f"HTTP {health.status_code} @ {config.langfuse_base_url}")
            return 1

    print("== 追踪 ==")
    _ok("本次种子后缀", suffix)
    trace_ids = _run_scenario(tracer, suffix)
    for label, trace_id in trace_ids.items():
        if trace_id:
            _ok(label, trace_id[:12] + "…")

    tracer.flush()
    await tracer.aclose()
    _ok("收尾", "flush 与 aclose 均未抛异常")

    print("== 落点核对 ==")
    if tracer.backend_name == "langfuse":
        if args.verify_wait > 0:
            _verify_remote(config, trace_ids, args.verify_wait)
        else:
            _ok("远端核对", "已按要求跳过")
    else:
        _verify_local(config)

    print()
    if _FAILED:
        print(f"冒烟失败：{_FAILED} 项未通过")
        return 1
    print("冒烟全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
