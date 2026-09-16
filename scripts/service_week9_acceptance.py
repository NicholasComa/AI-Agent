"""第 9 周服务验收：一条命令跑完全部在线接口测试。

与 ``service_week9_smoke.py`` 的分工：冒烟脚本用 ``ASGITransport`` 直打 app
对象，不占端口、不需外部依赖；本脚本走**真实 HTTP**，会拉起 ``serve.py``
子进程、等就绪、按序打接口、最后收掉进程，用于验收「服务真能跑起来」这件事。

用法::

    # 全自动：脚本自己起服务、测完自己关
    uv run python scripts/service_week9_acceptance.py

    # 服务已经开着，不要重复起
    uv run python scripts/service_week9_acceptance.py --no-spawn --port 8080

    # 只跑某一组
    uv run python scripts/service_week9_acceptance.py --only rag

分组（``--only`` 可选值）：``probe`` / ``rag`` / ``idempotency`` /
``stream`` / ``workflow`` / ``tools`` / ``envelope``。

退出码 0 表示全部通过；非 0 表示有失败项或脚本自身出错。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for path in (PROJECT_ROOT / "src", PROJECT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import httpx  # noqa: E402

RAG_PATH = "/v1/rag/answer"
START_PATH = "/v1/workflow/requirement-analysis"
TOOLS_PATH = "/v1/tools"

QUESTION = "Qdrant 是什么数据库？"
NORMAL_REQUIREMENT = "开发一个电商网站，包含商品浏览、购物车与支付功能。"
AMBIGUOUS_REQUIREMENT = "帮我做个东西"
CLARIFY_ANSWER = "仅 Web 版，不做移动端"

STARTUP_TIMEOUT_SECONDS = 60.0
READY_POLL_SECONDS = 2.0
"""HTTP 客户端超时。

真实 qwen3（CPU）单节点实测约 79 秒，7 个节点的工作流可达数分钟；这两个值
是「客户端愿意等多久」，与服务端的 ``AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS``
是两回事。用 ``--client-timeout`` 覆盖，机器慢就调大。
"""
CLIENT_TIMEOUT_DEFAULT = 900.0
"""客户端单请求默认超时（秒）。"""
CLIENT_TIMEOUT_PROBE = 30.0
"""探针与短请求超时：这些接口不调模型，不该等。"""

STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"


@dataclass
class Timeouts:
    """本次运行的超时配置。"""

    request: float = CLIENT_TIMEOUT_DEFAULT
    probe: float = CLIENT_TIMEOUT_PROBE

    @property
    def rest(self) -> float:
        """``httpx.Client`` 级默认超时，覆盖面比逐次传参更稳。"""
        return self.request


TIMEOUTS = Timeouts()
"""本次运行的超时配置，由 :func:`main` 按命令行参数写入。"""


@dataclass
class Result:
    """一条断言的结论。"""

    group: str
    label: str
    status: str
    detail: str = ""


@dataclass
class Report:
    """验收结果汇总。"""

    results: list[Result] = field(default_factory=list)

    def passed(self, group: str, label: str, detail: str = "") -> None:
        self.results.append(Result(group, label, "PASS", detail))

    def failed(self, group: str, label: str, detail: str) -> None:
        self.results.append(Result(group, label, "FAIL", detail))

    def skipped(self, group: str, label: str, detail: str) -> None:
        self.results.append(Result(group, label, STATUS_SKIPPED, detail))

    @property
    def failures(self) -> list[Result]:
        return [item for item in self.results if item.status == "FAIL"]

    @property
    def skips(self) -> list[Result]:
        return [item for item in self.results if item.status == STATUS_SKIPPED]

    def dump(self) -> None:
        width = max((len(item.label) for item in self.results), default=10)
        current = ""
        for item in self.results:
            if item.group != current:
                current = item.group
                print(f"\n== {current} ==")
            mark = {"PASS": "[PASS]", "FAIL": "[FAIL]", "skipped": "[SKIP]"}[item.status]
            line = f"{mark} {item.label:<{width}}"
            if item.detail:
                line += f"  {item.detail}"
            print(line)


class ServerProcess:
    """被脚本托管的 ``serve.py`` 子进程。"""

    def __init__(self, port: int, host: str, extra_env: dict[str, str]) -> None:
        self.port = port
        self.host = host
        self.extra_env = extra_env
        self._process: subprocess.Popen[str] | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        import os

        env = dict(os.environ)
        env.update(self.extra_env)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "serve.py"),
            "--app",
            "src.agent_service.app:app",
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        print(f"[INFO] 启动服务：{' '.join(command)}")
        self._process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def wait_until_ready(self) -> bool:
        """轮询 ``/health``，直到进程能应答或超时。

        ``/ready`` 会在依赖不全时返回 503，不适合当启动信号，所以这里探
        ``/health``——它只答进程存活。返回 ``False`` 表示进程已退出或超时。
        """
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                return False
            try:
                response = httpx.get(f"{self.base_url}/health", timeout=3.0)
            except httpx.HTTPError:
                time.sleep(READY_POLL_SECONDS)
                continue
            if response.status_code == 200:
                return True
            time.sleep(READY_POLL_SECONDS)
        return False

    def tail_output(self, lines: int = 30) -> str:
        """进程退出时把日志倒出来，便于定位启动失败原因。"""
        if self._process is None or self._process.stdout is None:
            return ""
        if self._process.poll() is None:
            return ""
        captured = self._process.stdout.read()
        return "\n".join(captured.splitlines()[-lines:])

    def stop(self) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        print("[INFO] 停止服务")
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)


def _parse_sse(lines: list[str]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for line in lines:
        if line.startswith("data: "):
            frames.append(json.loads(line[len("data: ") :]))
    return frames


def _stream_frames(
    client: httpx.Client, path: str, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """以流式方式请求并收集全部 SSE 数据帧。"""
    frames: list[dict[str, Any]] = []
    with client.stream("POST", path, json=payload, timeout=TIMEOUTS.request) as response:
        if response.status_code != 200:
            return []
        for line in response.iter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: ") :]))
    return frames


def run_probe(client: httpx.Client, report: Report) -> None:
    print("\n== 探针 ==")
    health = _safe_get(client, report, "probe", "/health", "/health", timeout=TIMEOUTS.probe)
    if health is not None:
        if health.status_code != 200:
            report.failed("probe", "/health", f"status={health.status_code}")
        else:
            body = health.json()
            report.passed("probe", "/health", f"status={body['status']} backend={body['backend']}")

    ready = _safe_get(client, report, "probe", "/ready", "/ready", timeout=TIMEOUTS.probe)
    if ready is None:
        return
    body = ready.json()
    names = [item["name"] for item in body["dependencies"]]
    expected = ["session_store", "config", "qdrant", "llm", "mcp", "workflow"]
    if names != expected:
        report.failed("probe", "/ready 依赖顺序", f"got={names}")
    elif ready.status_code == 200:
        report.passed("probe", "/ready", f"status=ready deps={len(names)}")
    else:
        broken = [
            item["name"] for item in body["dependencies"] if item["required"] and not item["ready"]
        ]
        report.failed("probe", "/ready", f"status={ready.status_code} required_not_ready={broken}")


def _qdrant_ready(client: httpx.Client) -> bool:
    """Qdrant 未起时 RAG 必然 503，据此决定是跳过还是真测。

    ``/ready`` 在任一必需依赖未就绪时整体返回 503，但响应体里的
    ``dependencies`` 逐项写明了每一层状态，所以这里只看数组、不看总状态码。
    """
    try:
        ready = client.get("/ready", timeout=TIMEOUTS.probe)
    except httpx.HTTPError:
        return False
    try:
        dependencies = ready.json()["dependencies"]
    except (ValueError, KeyError):
        return False
    for item in dependencies:
        if item["name"] == "qdrant":
            return bool(item["ready"])
    return False


def _safe_post(
    client: httpx.Client,
    report: Report,
    group: str,
    label: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> httpx.Response | None:
    """发一次 POST，把网络层异常转成 FAIL 而不是中断整个脚本。

    真实模型下单次请求可能远超客户端超时，或服务端正处于 ``asyncio.timeout``
    收尾阶段；这类失败应当记为一条失败断言，让后面的分组继续跑完。
    """
    try:
        return client.post(path, json=payload, timeout=timeout, headers=headers)
    except httpx.TimeoutException:
        report.failed(group, label, f"客户端读超时（{timeout:.0f}s）")
        return None
    except httpx.HTTPError as exc:
        report.failed(group, label, f"请求失败：{type(exc).__name__}: {exc}")
        return None


def _safe_get(
    client: httpx.Client,
    report: Report,
    group: str,
    label: str,
    path: str,
    *,
    timeout: float,
) -> httpx.Response | None:
    try:
        return client.get(path, timeout=timeout)
    except httpx.HTTPError as exc:
        report.failed(group, label, f"请求失败：{type(exc).__name__}: {exc}")
        return None


def run_rag(client: httpx.Client, report: Report, *, heavy: bool) -> None:
    print("\n== RAG 问答 ==")
    if not _qdrant_ready(client):
        report.skipped("rag", "RAG 问答", "qdrant 未就绪，先 docker start qdrant_server")
        return

    payload = {"question": QUESTION, "min_score": 0.0}
    response = _safe_post(
        client, report, "rag", "带引用回答", RAG_PATH, payload, timeout=TIMEOUTS.request
    )
    if response is None:
        return
    body = response.json()
    if response.status_code == 200 and body.get("has_answer") and body.get("citations"):
        report.passed("rag", "带引用回答", f"citations={len(body['citations'])}")
    else:
        report.failed("rag", "带引用回答", f"status={response.status_code} body={body}")

    if heavy:
        miss = _safe_post(
            client,
            report,
            "rag",
            "低分拒答",
            RAG_PATH,
            {"question": "今天天气怎么样？"},
            timeout=TIMEOUTS.request,
        )
        if miss is not None:
            miss_body = miss.json()
            if miss.status_code == 200 and miss_body.get("has_answer") is False:
                report.passed("rag", "低分拒答", f"reason={miss_body.get('rejected_reason')}")
            else:
                report.failed("rag", "低分拒答", f"status={miss.status_code} body={miss_body}")
    else:
        report.skipped("rag", "低分拒答", "--no-heavy 已跳过（非拒答路径会真实调用模型）")

    bad = _safe_post(
        client, report, "rag", "非法入参", RAG_PATH, {"question": ""}, timeout=TIMEOUTS.probe
    )
    if bad is not None:
        if bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_argument":
            report.passed("rag", "非法入参", "422 invalid_argument")
        else:
            report.failed("rag", "非法入参", f"status={bad.status_code} body={bad.json()}")


def run_stream(client: httpx.Client, report: Report) -> None:
    print("\n== SSE 流式 ==")
    if not _qdrant_ready(client):
        report.skipped("stream", "RAG 流式帧序", "qdrant 未就绪")
        return
    try:
        frames = _stream_frames(
            client, RAG_PATH, {"question": QUESTION, "min_score": 0.0, "stream": True}
        )
    except httpx.HTTPError as exc:
        report.failed("stream", "RAG 流式帧序", f"请求失败：{type(exc).__name__}: {exc}")
        return
    kinds = [frame["type"] for frame in frames]
    if kinds and kinds[0] == "delta" and kinds[-1] == STATUS_DONE and "citations" in kinds:
        report.passed("stream", "RAG 流式帧序", " -> ".join(kinds))
    else:
        report.failed("stream", "RAG 流式帧序", str(kinds))


def run_workflow(client: httpx.Client, report: Report, *, heavy: bool) -> None:
    """工作流分组。

    Args:
        heavy: 真实模型下 7 个节点总耗时可达数分钟，失败信息里会提示超时口径。
    """
    print("\n== 需求分析工作流 ==")
    payload = {"requirement_text": NORMAL_REQUIREMENT}
    response = _safe_post(
        client,
        report,
        "workflow",
        "完整需求",
        START_PATH,
        payload,
        timeout=TIMEOUTS.request,
    )
    thread_id: str | None = None
    if response is not None:
        body = response.json()
        status = body.get("status")
        if response.status_code == 200 and status in {"completed", "awaiting_clarification"}:
            trace = ">".join(body.get("trace") or [])
            report.passed("workflow", "完整需求", f"status={status} trace={trace}")
        elif response.status_code == 504:
            report.failed(
                "workflow",
                "完整需求",
                "504 超时；真实 qwen3 单节点约 79s，请调大 "
                "AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS（--timeout-seconds 可直接传）",
            )
        else:
            report.failed("workflow", "完整需求", f"status={response.status_code} body={body}")

    ambiguous = _safe_post(
        client,
        report,
        "workflow",
        "歧义需求挂起",
        START_PATH,
        {"requirement_text": AMBIGUOUS_REQUIREMENT},
        timeout=TIMEOUTS.request,
    )
    if ambiguous is not None:
        ambiguous_body = ambiguous.json()
        if ambiguous_body.get("status") == "awaiting_clarification" and ambiguous_body.get(
            "clarification_questions"
        ):
            report.passed(
                "workflow",
                "歧义需求挂起",
                f"questions={len(ambiguous_body['clarification_questions'])}",
            )
            thread_id = ambiguous_body.get("thread_id")
        elif ambiguous.status_code == 504:
            report.failed("workflow", "歧义需求挂起", "504 超时")
        else:
            report.failed("workflow", "歧义需求挂起", f"status={ambiguous_body.get('status')}")

    if not thread_id:
        report.skipped("workflow", "resume 续跑", "未拿到 thread_id，续跑无法进行")
        return
    if not heavy:
        report.skipped("workflow", "resume 续跑", "--no-heavy 已跳过长链路")
        return

    resumed = _safe_post(
        client,
        report,
        "workflow",
        "resume 续跑",
        f"/v1/workflow/{thread_id}/resume",
        {"answers": [CLARIFY_ANSWER]},
        timeout=TIMEOUTS.request,
    )
    if resumed is not None:
        resumed_body = resumed.json()
        if resumed.status_code == 200 and resumed_body.get("status") == "completed":
            trace = ">".join(resumed_body.get("trace") or [])
            report.passed("workflow", "resume 续跑", f"trace={trace}")
        elif resumed.status_code == 504:
            report.failed(
                "workflow",
                "resume 续跑",
                "504 超时；已知问题，超时口径需按路由分级（工作流约 600s）",
            )
        else:
            report.failed(
                "workflow", "resume 续跑", f"status={resumed.status_code} body={resumed_body}"
            )

    finished = _safe_post(
        client,
        report,
        "workflow",
        "无挂起点续跑",
        f"/v1/workflow/{thread_id}/resume",
        {"answers": [CLARIFY_ANSWER]},
        timeout=TIMEOUTS.request,
    )
    if finished is not None:
        if finished.status_code == 422 and finished.json()["error"]["code"] == "invalid_argument":
            report.passed("workflow", "无挂起点续跑", "422 invalid_argument")
        else:
            report.failed("workflow", "无挂起点续跑", f"status={finished.status_code}")


def run_tools(client: httpx.Client, report: Report) -> None:
    print("\n== 工具接口 ==")
    response = _safe_get(
        client, report, "tools", "GET /v1/tools", TOOLS_PATH, timeout=TIMEOUTS.probe
    )
    if response is None:
        return
    body = response.json()
    if response.status_code == 200:
        names = [item["name"] for item in body["tools"]]
        report.passed("tools", "GET /v1/tools", f"tools={len(names)}")
    elif response.status_code == 503 and body["error"]["code"] == "dependency_unavailable":
        report.passed("tools", "GET /v1/tools", f"503 未启用 MCP（{body['error']['detail']}）")
    else:
        report.failed("tools", "GET /v1/tools", f"status={response.status_code} body={body}")


def run_envelope(client: httpx.Client, report: Report) -> None:
    print("\n== 统一错误信封 ==")
    response = _safe_get(
        client, report, "envelope", "404 信封", "/no-such-path", timeout=TIMEOUTS.probe
    )
    if response is not None:
        body = response.json()
        error = body.get("error", {})
        if response.status_code == 404 and error.get("request_id") and error.get("code"):
            report.passed("envelope", "404 信封", f"code={error['code']}")
        else:
            report.failed("envelope", "404 信封", f"status={response.status_code} body={body}")

    if not _qdrant_ready(client):
        report.skipped("envelope", "幂等回放", "qdrant 未就绪")
        return
    headers = {"Idempotency-Key": "acceptance-1"}
    payload = {"question": QUESTION, "min_score": 0.0}
    first = _safe_post(
        client,
        report,
        "envelope",
        "幂等回放",
        RAG_PATH,
        payload,
        timeout=TIMEOUTS.request,
        headers=headers,
    )
    if first is None:
        return
    if first.status_code != 200:
        report.failed("envelope", "幂等回放", f"首次请求 status={first.status_code}")
        return
    replay = _safe_post(
        client,
        report,
        "envelope",
        "幂等回放",
        RAG_PATH,
        payload,
        timeout=TIMEOUTS.request,
        headers=headers,
    )
    if replay is None:
        return
    if replay.headers.get("Idempotency-Replayed") == "true" and replay.json() == first.json():
        report.passed("envelope", "幂等回放", "Idempotency-Replayed=true")
    else:
        report.failed(
            "envelope", "幂等回放", f"header={replay.headers.get('Idempotency-Replayed')}"
        )


def _selected(groups: list[str], name: str) -> bool:
    return not groups or name in groups


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第 9 周服务验收（真实 HTTP）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8080, help="绑定端口（默认 8080）")
    parser.add_argument("--no-spawn", action="store_true", help="服务已开着，不要重复启动")
    parser.add_argument(
        "--only",
        default="",
        help="只跑指定分组，逗号分隔：probe,rag,idempotency,stream,workflow,tools,envelope",
    )
    parser.add_argument(
        "--no-heavy",
        action="store_true",
        help="跳过 resume 这类长链路用例（真实模型下单次可达数分钟）",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=0.0,
        help="覆盖服务端请求超时（通过环境变量传给子进程，0 表示不覆盖）",
    )
    parser.add_argument(
        "--client-timeout",
        type=float,
        default=CLIENT_TIMEOUT_DEFAULT,
        help=f"客户端单请求超时秒数（默认 {CLIENT_TIMEOUT_DEFAULT:.0f}）",
    )
    args = parser.parse_args(argv)

    groups = [item.strip() for item in args.only.split(",") if item.strip()]
    report = Report()
    server: ServerProcess | None = None

    global TIMEOUTS  # noqa: PLW0603 —— 脚本级配置，供各分组函数读取
    TIMEOUTS = Timeouts(request=args.client_timeout)

    extra_env: dict[str, str] = {}
    if args.timeout_seconds > 0:
        extra_env["AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS"] = str(args.timeout_seconds)

    if not args.no_spawn:
        server = ServerProcess(args.port, args.host, extra_env)
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
    exit_code = 0
    try:
        with httpx.Client(base_url=base_url, timeout=TIMEOUTS.rest) as client:
            if _selected(groups, "probe"):
                run_probe(client, report)
            if _selected(groups, "rag"):
                run_rag(client, report, heavy=not args.no_heavy)
            if _selected(groups, "idempotency") or _selected(groups, "envelope"):
                run_envelope(client, report)
            if _selected(groups, "stream"):
                run_stream(client, report)
            if _selected(groups, "workflow"):
                run_workflow(client, report, heavy=not args.no_heavy)
            if _selected(groups, "tools"):
                run_tools(client, report)
    finally:
        if server is not None:
            server.stop()

    report.dump()
    print()
    if report.failures:
        print(f"验收未通过：{len(report.failures)} 项失败，{len(report.skips)} 项跳过")
        exit_code = 1
    elif report.skips:
        print(
            f"验收通过：{len(report.results) - len(report.skips)} 项通过，{len(report.skips)} 项跳过"
        )
    else:
        print(f"验收全部通过：{len(report.results)} 项")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
