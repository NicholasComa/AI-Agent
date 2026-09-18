"""第 9 周工程测试：并发压力与超时行为。

与另外两个脚本的分工：

* ``service_week9_smoke.py`` —— 用 ``ASGITransport`` 直打 app 对象，验接口契约；
* ``service_week9_acceptance.py`` —— 真实 HTTP 跑全部分组，验「服务能跑起来」；
* **本脚本** —— 只回答两个工程问题：**并发下会不会崩**、**超时会不会被正确
  识别并计数**。

两类测试的驱动方式刻意不同，因为它们要证明的事情不同：

- **并发**必须走真实 HTTP 与真实事件循环，才能让并发闸门、限流器、连接池都
  参与进来；用 ASGITransport 会把并发压成顺序，测不出任何东西。
- **超时**必须能精确控制模型耗时到「刚好超过服务端超时」。真实模型无法做到
  这一点（CPU 推理耗时波动大，且会真的等上几十秒），因此这一组在进程内用
  注入的慢 ``ChatFn`` 构造，慢的秒数完全可控。

用法::

    # 全部（默认 20 与 50 两级并发 + 超时组）
    uv run python scripts/service_week9_load.py

    # 服务已开着，复用不重启
    uv run python scripts/service_week9_load.py --no-spawn --port 8080

    # 只跑一级并发；自定义请求数
    uv run python scripts/service_week9_load.py --levels 20 --requests 60

    # 真实模型下并发数要压低，否则必然排队超时
    uv run python scripts/service_week9_load.py --levels 5 --requests 10 \
        --client-timeout 600

    # 只跑超时组（不需要 Qdrant，也不需要起服务）
    uv run python scripts/service_week9_load.py --only timeout

退出码 0 表示全部断言通过；1 表示有失败项；2 表示脚本自身出错（服务没起来）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = PROJECT_ROOT / "tests"
for path in (
    PROJECT_ROOT / "src",
    PROJECT_ROOT,
    # 测试替身与夹具放在这两个目录，超时组需要复用它们。
    _TESTS_DIR,
    _TESTS_DIR / "agent_service",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import httpx  # noqa: E402
from service_week9_acceptance import (  # noqa: E402
    QUESTION,
    RAG_PATH,
    PortInUseError,
    Report,
    ServerProcess,
)

DEFAULT_LEVELS = (20, 50)
"""并发档位。路线要求验 20 与 50 两级。"""

DEFAULT_REQUESTS = 40
"""每级总请求数。为并发数的 2 倍，保证「排队—放行—再排队」多轮发生。"""

DEFAULT_MAX_CONCURRENCY = 8
"""被测服务的默认并发闸门容量（``AGENT_SERVICE_MAX_CONCURRENCY``）。

与 :class:`agent_service.settings.AgentServiceSettings` 的默认值保持一致，用于判断
429 是设计内的排队保护还是配置缺陷。服务端改过该键时须用 ``--max-concurrency`` 同步。
"""

STARTUP_TIMEOUT_SECONDS = 60.0
READY_POLL_SECONDS = 2.0

SERVER_TIMEOUT_SECONDS = 2.0
"""超时组注入的服务端请求超时。故意取得很短，让测试几秒内出结论。"""

SLOW_CHAT_SECONDS = 5.0
"""超时组假模型的单次耗时，需明显大于 :data:`SERVER_TIMEOUT_SECONDS`。"""


# ---------------------------------------------------------------------------
# 断言与统计
# ---------------------------------------------------------------------------


def _percentile(values: list[float], percentile: float) -> float:
    """线性插值分位数；样本为空时返回 0.0。

    与 ``metrics.py`` 里的桶估算不同，这里保留全量样本，因此可以给出精确值——
    并发测试的样本量可控（百级），不需要为此牺牲精度。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * max(0.0, min(percentile, 1.0))
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@dataclass
class Outcome:
    """一次并发压测的原始观测。"""

    status_codes: list[int] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    headers_seen: set[str] = field(default_factory=set)

    @property
    def duration_seconds(self) -> float:
        return max(self.finished_at - self.started_at, 1e-9)

    @property
    def throughput(self) -> float:
        """吞吐（请求/秒）。"""
        return len(self.status_codes) / self.duration_seconds

    def count(self, code: int) -> int:
        return sum(1 for item in self.status_codes if item == code)

    @property
    def server_errors(self) -> list[int]:
        """5xx 状态码列表。并发压测下出现任何一个都算失败。"""
        return [code for code in self.status_codes if code >= 500]


def _fire_one(
    client: httpx.Client,
    *,
    timeout: float,
    min_score: float,
) -> tuple[int | None, float, str]:
    """打一次 RAG 请求，返回 ``(状态码, 耗时毫秒, 错误说明)``。

    客户端异常不向上抛：并发场景下单个请求失败必须被记录成数据点，否则一个
    连接重置就会把整轮压测打断。

    Args:
        client: 已建连的客户端。
        timeout: 单请求超时。
        min_score: 传给接口的相似度阈值。设为 ``1.0`` 可强制走「低分拒答」路径，
            使压测只经过检索与闸门、不调用模型——CPU 推理下单次约 79 秒，
            真实模型连打几十次会让压测失去可操作性。
    """
    started = time.perf_counter()
    try:
        response = client.post(
            RAG_PATH,
            json={"question": QUESTION, "min_score": min_score},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return None, elapsed, f"{type(exc).__name__}: {exc}"
    elapsed = (time.perf_counter() - started) * 1000
    return response.status_code, elapsed, ""


def run_level(
    base_url: str,
    *,
    level: int,
    requests: int,
    client_timeout: float,
    min_score: float,
) -> Outcome:
    """以 ``level`` 并发打完 ``requests`` 个请求。

    使用线程池而不是 ``asyncio.gather``：``httpx.Client`` 的同步接口在线程里
    会各自持有一条连接，能真实压到服务端的并发闸门；同一条同步连接串行发包
    则完全测不到并发行为。
    """
    outcome = Outcome(started_at=time.perf_counter())
    # 单请求超时给足余量：要测的是服务端闸门如何排队，不是客户端等不及。
    per_request_timeout = max(client_timeout, 60.0)

    def worker() -> None:
        # 每个线程一条独立连接，避免共享连接池把并发串行化。
        # trust_env=False 是必须的：开发机上常配 HTTP_PROXY / HTTPS_PROXY，
        # httpx 默认会读它们，把发往 127.0.0.1 的请求也送去代理，代理不认识
        # 本机端口便回 502 或直接断连，表现为压测大面积失败而服务其实无恙。
        with httpx.Client(
            base_url=base_url,
            timeout=per_request_timeout,
            trust_env=False,
        ) as client:
            status, elapsed, error = _fire_one(
                client, timeout=per_request_timeout, min_score=min_score
            )
            with lock:
                outcome.latencies_ms.append(elapsed)
                if status is not None:
                    outcome.status_codes.append(status)
                if error:
                    outcome.errors.append(error)

    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=level) as pool:
        futures = [pool.submit(worker) for _ in range(requests)]
        for future in futures:
            future.result()
    outcome.finished_at = time.perf_counter()
    return outcome


def report_level(
    outcome: Outcome,
    *,
    level: int,
    report: Report,
    max_concurrency: int,
) -> None:
    """把一轮并发结果落成断言与数字。

    四条断言分开记录而不是合并成一条：网络失败、5xx、限流比例、性能数字各自
    可能独立出问题，合并后只能看到「失败」，无法定位是哪一类。

    Args:
        outcome: 本轮采集结果。
        level: 并发档位。
        report: 结果收集器。
        max_concurrency: 被测服务的并发闸门容量。用它区分「设计内的排队保护」与
            「容量异常」，而不是一刀切地允许或禁止 429。
    """
    group = "load"

    if len(outcome.errors) > 0:
        report.failed(
            group,
            f"{level} 并发 · 全部收到响应",
            f"{len(outcome.errors)}/{len(outcome.status_codes) + len(outcome.errors)} "
            f"个请求在网络层失败，示例：{outcome.errors[0][:120]}",
        )
    else:
        report.passed(group, f"{level} 并发 · 全部收到响应", f"{len(outcome.status_codes)} 个")

    if outcome.server_errors:
        report.failed(
            group,
            f"{level} 并发 · 无 5xx",
            f"出现 5xx 状态码：{sorted(set(outcome.server_errors))}",
        )
    else:
        report.passed(group, f"{level} 并发 · 无 5xx", "逐个请求均非 5xx")

    codes: dict[int, int] = {}
    for code in outcome.status_codes:
        codes[code] = codes.get(code, 0) + 1
    distribution = " ".join(f"{code}x{count}" for code, count in sorted(codes.items()))
    total = len(outcome.status_codes)
    throttled = codes.get(429, 0)

    # 并发不超过闸门容量时不允许出现 429：那说明限流口径与服务配置不一致，是缺陷。
    # 超过容量时 429 是设计内的排队保护，但比例仍要看清——全部被限流意味着这一档
    # 根本没测到服务能力，数字没有解释力。
    if level <= max_concurrency:
        if throttled:
            report.failed(
                group,
                f"{level} 并发 · 限流只出现在超配额场景",
                f"并发 {level} ≤ 闸门 {max_concurrency} 却出现 {throttled} 个 429",
            )
        else:
            report.passed(
                group,
                f"{level} 并发 · 限流只出现在超配额场景",
                f"未出现 429（闸门={max_concurrency}）",
            )
    elif throttled == total:
        report.failed(
            group,
            f"{level} 并发 · 限流只出现在超配额场景",
            f"全部 {total} 个请求被限流，本档未观测到任何有效吞吐",
        )
    else:
        report.passed(
            group,
            f"{level} 并发 · 限流只出现在超配额场景",
            f"429x{throttled}/{total} 属闸门={max_concurrency} 的排队保护，"
            f"仍拿到 {total - throttled} 个 200",
        )

    report.passed(
        group,
        f"{level} 并发 · 吞吐与延迟",
        f"吞吐={outcome.throughput:.1f} req/s "
        f"p50={_percentile(outcome.latencies_ms, 0.5):.0f}ms "
        f"p95={_percentile(outcome.latencies_ms, 0.95):.0f}ms "
        f"max={max(outcome.latencies_ms, default=0.0):.0f}ms | {distribution}",
    )


# ---------------------------------------------------------------------------
# 超时组：进程内注入慢模型
# ---------------------------------------------------------------------------


class SlowChat:
    """慢对话函数替身：每次调用睡 ``delay_seconds`` 秒。"""

    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.calls = 0

    async def __call__(self, messages: list[dict[str, str]], /) -> str:
        self.calls += 1
        await asyncio.sleep(self.delay_seconds)
        return json.dumps(
            {
                "answer": "不应被返回的答案",
                "has_answer": True,
                "citations": [],
                "confidence": 0.5,
            },
            ensure_ascii=False,
        )


async def _timeout_case(tmp_dir: Path) -> tuple[Report, dict[str, Any]]:
    """在进程内跑超时用例，返回报告与指标快照。"""
    from service_fakes import build_deps, build_rag

    from agent_service import AgentServiceSettings, create_agent_service_app

    report = Report()
    label = f"慢模型 -> {int(SERVER_TIMEOUT_SECONDS)}s 超时"

    root = Path(tmp_dir)
    root.mkdir(parents=True, exist_ok=True)
    rag = build_rag(root)
    slow = SlowChat(SLOW_CHAT_SECONDS)
    settings = AgentServiceSettings(
        session_dir=root / "sessions",
        request_timeout_seconds=SERVER_TIMEOUT_SECONDS,
        # 闸门排队时间给足，确保失败原因只能是「总时限」而不是「排队超时」。
        queue_timeout_seconds=30.0,
    )
    deps = build_deps(root, rag=rag, chat=slow, settings=settings)
    app = create_agent_service_app(deps=deps, version="0.1.0")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://timeout-test",
            timeout=60.0,
            trust_env=False,
        ) as client,
    ):
        response = await client.post(RAG_PATH, json={"question": QUESTION, "min_score": 0.0})
        content_type = response.headers.get("content-type", "")
        body = response.json() if content_type.startswith("application/json") else {}
        error = body.get("error", {})
        if response.status_code == 504 and error.get("code") == "timeout":
            report.passed(label, "504 timeout", f"detail={error.get('detail')}")
        else:
            report.failed(
                label,
                "504 timeout",
                f"status={response.status_code} code={error.get('code')} body={body}",
            )

        # 超时必须以「依赖仍在、只是本次请求被中断」收场，因此探针应保持 200。
        ready = await client.get("/ready")
        if ready.status_code == 200:
            report.passed(label, "超时后服务存活", f"/ready={ready.status_code}")
        else:
            report.failed(
                label, "超时后服务存活", f"/ready={ready.status_code} body={ready.json()}"
            )

        summary = (await client.get("/metrics-summary")).json()
        timeout_count = summary["errors_by_code"].get("timeout", 0)
        if timeout_count >= 1:
            report.passed(label, "超时计入 errors_by_code", f"timeout={timeout_count}")
        else:
            report.failed(
                label, "超时计入 errors_by_code", f"errors_by_code={summary['errors_by_code']}"
            )

        if summary["requests_by_status"].get("504", 0) >= 1:
            report.passed(
                label,
                "504 计入状态码分布",
                f"requests_by_status={summary['requests_by_status']}",
            )
        else:
            report.failed(
                label,
                "504 计入状态码分布",
                f"requests_by_status={summary['requests_by_status']}",
            )

        # 超时发生在模型调用中，说明闸门确实把本次请求放行过——这一条用来证明
        # 「504 不是因为并发闸门排队」。
        if slow.calls >= 1:
            report.passed(label, "超时前已进入模型调用", f"chat 调用次数={slow.calls}")
        else:
            report.failed(label, "超时前已进入模型调用", "假模型从未被调用，失败原因存疑")

    return report, summary


def run_timeout(report: Report, *, workdir: Path) -> dict[str, Any]:
    """同步入口：跑超时组。"""
    print("\n== 超时 ==")
    timeout_report, summary = asyncio.run(_timeout_case(workdir))
    report.results.extend(timeout_report.results)
    return summary


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第 9 周工程测试：并发与超时")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8080, help="绑定端口（默认 8080）")
    parser.add_argument("--no-spawn", action="store_true", help="服务已开着，不要重复启动")
    parser.add_argument(
        "--levels",
        default=",".join(str(item) for item in DEFAULT_LEVELS),
        help=f"并发档位，逗号分隔（默认 {','.join(str(i) for i in DEFAULT_LEVELS)}）",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=DEFAULT_REQUESTS,
        help=f"每级总请求数（默认 {DEFAULT_REQUESTS}）",
    )
    parser.add_argument(
        "--only",
        default="",
        help="只跑指定组，逗号分隔：load,timeout",
    )
    parser.add_argument(
        "--client-timeout",
        type=float,
        default=120.0,
        help="每级压测里单请求的超时秒数（默认 120）",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.0,
        help=(
            "传给 /v1/rag/answer 的相似度阈值。默认 0.0 走完整链路（会调用模型）；"
            "设为 1.0 可强制低分拒答，只压检索与并发闸门，避免 CPU 推理拖垮压测"
        ),
    )
    parser.add_argument(
        "--no-heavy",
        action="store_true",
        help="跳过 50 并发档，只跑较小的一级（真实模型下建议配合 --levels 5）",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=DEFAULT_MAX_CONCURRENCY,
        help=(
            "被测服务的并发闸门容量（AGENT_SERVICE_MAX_CONCURRENCY）。"
            f"用于判断 429 是设计内的排队保护还是配置缺陷（默认 {DEFAULT_MAX_CONCURRENCY}）"
        ),
    )
    args = parser.parse_args(argv)

    groups = [item.strip() for item in args.only.split(",") if item.strip()]

    def selected(name: str) -> bool:
        return not groups or name in groups

    levels = [int(item) for item in args.levels.split(",") if item.strip()]
    if args.no_heavy and len(levels) > 1:
        levels = levels[:1]

    report = Report()
    server: ServerProcess | None = None
    exit_code = 0

    try:
        if selected("load"):
            server = None
            if not args.no_spawn:
                server = ServerProcess(args.port, args.host, {"PYTHONIOENCODING": "utf-8"})
                try:
                    server.ensure_port_free()
                except PortInUseError as exc:
                    # 端口被占时必须直接退出：否则就绪轮询会从占用者（旧服务或
                    # compose 容器）拿到 /health 200，整轮压测便打在它的配置上，
                    # 并发闸门与配额都不是本次打算测的那套。
                    print(f"[FAIL] {exc}")
                    return 2
                server.start()
                if not server.wait_until_ready():
                    print("[FAIL] 服务未能在限时内就绪")
                    tail = server.tail_output()
                    if tail:
                        print("--- 服务日志尾部 ---")
                        print(tail)
                    server.stop()
                    return 2
                print("[INFO] 服务已就绪")
            base_url = server.base_url if server else f"http://{args.host}:{args.port}"

            print("\n== 并发 ==")
            print(
                f"[INFO] 档位={levels} 每级请求数={args.requests} "
                f"min_score={args.min_score} 闸门={args.max_concurrency}"
            )
            for level in levels:
                outcome = run_level(
                    base_url,
                    level=level,
                    requests=args.requests,
                    client_timeout=args.client_timeout,
                    min_score=args.min_score,
                )
                report_level(
                    outcome, level=level, report=report, max_concurrency=args.max_concurrency
                )

        if selected("timeout"):
            run_timeout(report, workdir=PROJECT_ROOT / "data" / "week9_load_tmp")

    except KeyboardInterrupt:
        print("\n[INFO] 已中断")
        exit_code = 2
    finally:
        if server is not None:
            server.stop()

    report.dump()
    print()
    if report.failures:
        print(f"工程测试未通过：{len(report.failures)} 项失败，{len(report.skips)} 项跳过")
        return 1
    print(f"工程测试通过：{len(report.results)} 项断言全部通过")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
