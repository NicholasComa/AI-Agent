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

    # 连入站防护一起验（密钥与两个上限都注入子进程）
    uv run python scripts/service_week9_acceptance.py \
        --api-key demo-key --rate-limit 5 --max-body-bytes 512

分组（``--only`` 可选值）：``probe`` / ``rag`` / ``idempotency`` /
``stream`` / ``workflow`` / ``tools`` / ``envelope`` / ``security``。

``security`` 分组放在最后执行：它的 429 用例会打满配额，先跑会把后续分组
全打成 429。413 与 429 断言需要知道服务端上限，因此只在传了
``--max-body-bytes`` / ``--rate-limit`` 时执行，否则记为跳过。

自建子进程之前会检查端口是否空闲。``/health`` 只能证明「这个端口上有服务在
答」，不能证明「答的是刚起的那个进程」——若端口被旧服务或 compose 容器占着，
就绪轮询会从占用者拿到 200，整轮断言便打在**它的**配置上。端口冲突时脚本
直接以退出码 2 终止并指明占用者。

退出码 0 表示全部通过；非 0 表示有失败项或脚本自身出错（2 为前置条件不满足）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from collections import deque
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

API_KEY = ""
"""本次运行的 Bearer 密钥；非空时所有业务请求都带上该令牌。"""

BODY_LIMIT = 0
"""子进程的请求体上限；0 表示沿用服务缺省值（用于 413 用例）。"""

RATE_LIMIT = 0
"""子进程的每分钟配额；0 表示沿用服务缺省值（用于 429 用例）。"""


def _auth_headers() -> dict[str, str]:
    """业务路由需要的认证头；未配置密钥时为空。"""
    return {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}


def _merge_headers(headers: dict[str, str] | None = None) -> dict[str, str] | None:
    """把认证头并入调用方给出的请求头。"""
    merged = _auth_headers()
    if headers:
        merged.update(headers)
    return merged or None


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


class PortInUseError(RuntimeError):
    """目标端口已被别的进程占用，脚本不该继续跑。

    典型场景：compose 的 ``api`` 容器仍发布着 8080。此时新起的子进程会因
    端口冲突退出，而脚本的就绪轮询却能从**那个容器**拿到 ``/health`` 的
    200——于是整轮验收都打在容器的旧配置上，断言全错但看不出原因。
    """


def _port_owner(host: str, port: int) -> str:
    """询问本机谁在监听该端口，用于把冲突原因写进报错信息。

    只做只读查询（``netstat`` 取 PID，``tasklist`` 取进程名），不杀任何进程。
    查不到时返回空串——拿不到名字不影响判定「端口被占」这件事本身。
    """
    target = f":{port} "
    pids: list[str] = []
    try:
        netstat = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in netstat.stdout.splitlines():
        if "LISTENING" not in line or target not in line:
            continue
        parts = line.split()
        if parts and parts[-1].isdigit() and parts[-1] not in pids:
            pids.append(parts[-1])
    if not pids:
        return ""

    names: list[str] = []
    for pid in pids:
        try:
            tasklist = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        first = tasklist.stdout.strip().splitlines()
        if not first:
            continue
        name = first[0].split(",")[0].strip().strip('"')
        if name and name not in names:
            names.append(f"{name}(pid={pid})")
    return ", ".join(names)


class ServerProcess:
    """被脚本托管的 ``serve.py`` 子进程。"""

    def __init__(self, port: int, host: str, extra_env: dict[str, str]) -> None:
        self.port = port
        self.host = host
        self.extra_env = extra_env
        self._process: subprocess.Popen[str] | None = None
        self._recent: deque[str] = deque(maxlen=200)
        self._reader: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def ensure_port_free(self) -> None:
        """确认端口没被占用，否则抛出 :class:`PortInUseError`。

        必须在 ``start()`` 之前调用：起得来服务的前提是端口真的属于它。若只
        等 ``/health`` 返回 200 就放行，占用端口的旧进程（例如 compose 容器）
        会让整轮验收静默地测错对象。
        """
        owner = _port_owner(self.host, self.port)
        if owner:
            raise PortInUseError(
                f"端口 {self.host}:{self.port} 已被占用（{owner}）。"
                f"请先释放：容器占用的执行 `docker compose down`，"
                f"旧脚本进程占用的执行 `taskkill /F /PID <pid>`；"
                f"或换一个空闲端口：`--port {self.port + 1}`。"
            )

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
        self._reader = threading.Thread(target=self._drain_output, daemon=True)
        self._reader.start()

    def _drain_output(self) -> None:
        """持续读取子进程输出并保留最后若干行。

        必须边跑边读：管道缓冲区只有几 KB，写满后服务进程会阻塞在写日志上，
        对外表现为请求无响应。这不是业务逻辑出错，但会让验收结果全是超时。
        """
        if self._process is None or self._process.stdout is None:
            return
        for line in self._process.stdout:
            self._recent.append(line.rstrip("\n"))

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
                # trust_env=False：开发机常配 HTTP(S)_PROXY，httpx 默认会把发往
                # 127.0.0.1 的探测也送去代理，代理回 502 或断连会让就绪判断永远
                # 失败，而服务其实早已起来。
                response = httpx.get(f"{self.base_url}/health", timeout=3.0, trust_env=False)
            except httpx.HTTPError:
                time.sleep(READY_POLL_SECONDS)
                continue
            if response.status_code == 200:
                return True
            time.sleep(READY_POLL_SECONDS)
        return False

    def tail_output(self, lines: int = 30) -> str:
        """取最近若干行日志，便于定位启动失败原因。"""
        if not self._recent:
            return ""
        return "\n".join(list(self._recent)[-lines:])

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
        if self._reader is not None:
            # 进程已退出，读取线程会随即读到 EOF；等一下让日志收全。
            self._reader.join(timeout=5)


def _stream_frames(
    client: httpx.Client, path: str, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """以流式方式请求并收集全部 SSE 数据帧。"""
    frames: list[dict[str, Any]] = []
    with client.stream(
        "POST",
        path,
        json=payload,
        timeout=TIMEOUTS.request,
        headers=_merge_headers(),
    ) as response:
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
    expected = [
        "observability",
        "session_store",
        "config",
        "qdrant",
        "llm",
        "mcp",
        "workflow",
    ]
    if names != expected:
        report.failed("probe", "/ready 依赖顺序", f"got={names}")
    elif ready.status_code == 200:
        report.passed("probe", "/ready", f"status=ready deps={len(names)}")
    else:
        broken = [
            item["name"] for item in body["dependencies"] if item["required"] and not item["ready"]
        ]
        report.failed("probe", "/ready", f"status={ready.status_code} required_not_ready={broken}")


def run_security(client: httpx.Client, report: Report) -> None:
    """入站防护分组：认证、请求体上限与配额。

    413 与 429 需要知道服务端的上限才能断言，因此只在 ``--max-body-bytes`` /
    ``--rate-limit`` 显式给出时执行；认证用例则按服务实际行为判定，未配密钥
    的服务会自动跳过——不能把「本机开发模式」当成失败。
    """
    print("\n== 入站防护 ==")

    # 探针不带令牌也必须作答，否则编排系统会把「依赖坏了」误判成「进程死了」。
    #
    # 判据是白名单而非「非 401 即通过」：后者会让 500 / 502 / 404 也记成 PASS，
    # 而路由被改、探针内抛异常恰恰是这套断言要拦的回归。
    # 两条探针的合法码不同——/ready 在必需依赖未就绪时**设计上**返回 503
    # （见 routes/health.py:60-62），因此它必须同时接受 200 与 503；
    # /health 只判进程存活，状态码恒为 200，故只认 200。
    for path, expected in (("/health", {200}), ("/ready", {200, 503})):
        response = _safe_get(
            client, report, "security", f"探针免认证 {path}", path, timeout=TIMEOUTS.probe
        )
        if response is None:
            continue
        if response.status_code == 401:
            report.failed("security", f"探针免认证 {path}", "探针不应要求认证")
        elif response.status_code in expected:
            report.passed("security", f"探针免认证 {path}", f"status={response.status_code}")
        else:
            report.failed(
                "security",
                f"探针免认证 {path}",
                f"unexpected status={response.status_code}，合法值 {sorted(expected)}",
            )

    # 判据用「不带令牌的请求是否被拒」推断服务是否启用了认证。
    #
    # 请求体刻意**故意非法**（缺必填字段）：这样认证开启时结果必然是 401
    # （认证依赖排在参数校验之前），认证关闭时结果必然是 422，两种情形都
    # 在几毫秒内返回、不触碰模型。
    #
    # 早期版本发的是合法请求体，导致「认证未启用」时这一条会真的跑完整个
    # RAG 链路（本机真实模型约 50 秒），被 30 秒客户端超时打断后记成 FAIL，
    # 报出「未授权拒绝：客户端读超时」这种与实际语义无关的结论。
    anonymous = _safe_post(
        client,
        report,
        "security",
        "未授权拒绝",
        RAG_PATH,
        {},
        timeout=TIMEOUTS.probe,
        headers={"Authorization": ""},
    )
    if anonymous is not None:
        code = anonymous.json().get("error", {}).get("code")
        if anonymous.status_code == 401:
            if code == "unauthorized" and API_KEY:
                report.passed("security", "未授权拒绝", "401 unauthorized")
            elif code == "unauthorized":
                report.failed(
                    "security",
                    "未授权拒绝",
                    "服务已启用认证，请用 --api-key 传入密钥，否则其余分组也会 401",
                )
            else:
                report.failed("security", "未授权拒绝", f"code={code}")
        elif anonymous.status_code == 422 and not API_KEY:
            # 未配密钥 → 认证不启用 → 非法请求体在参数校验阶段被拒。
            # 这说明「没有认证」是配置使然，而不是防护被绕过。
            report.skipped(
                "security",
                "未授权拒绝",
                "服务未启用认证（AGENT_SERVICE_API_KEY 为空）；非法请求体已在参数校验阶段被拒（422）",
            )
        else:
            # 500 / 502 / 429 / 404 / 200 等一律为失败：以前这里记 SKIP，而 SKIP 不进
            # report.failures，会让「防护整体失效」拿到退出码 0。
            # 200 尤其要判失败——非法请求体被放行，说明校验链断了。
            report.failed(
                "security",
                "未授权拒绝",
                f"疑似防护失效：status={anonymous.status_code} code={code}，"
                f"期望 401（认证开启）或 422（认证关闭）",
            )

    if BODY_LIMIT > 0:
        oversized = _safe_post(
            client,
            report,
            "security",
            "超限请求体 -> 413",
            RAG_PATH,
            {"question": "x" * (BODY_LIMIT + 64), "min_score": 0.0},
            timeout=TIMEOUTS.probe,
        )
        if oversized is not None:
            error = oversized.json().get("error", {})
            if oversized.status_code == 413 and error.get("code") == "payload_too_large":
                report.passed("security", "超限请求体 -> 413", f"limit={BODY_LIMIT}")
            else:
                report.failed(
                    "security",
                    "超限请求体 -> 413",
                    f"status={oversized.status_code} code={error.get('code')}",
                )
    else:
        report.skipped("security", "超限请求体 -> 413", "未给 --max-body-bytes，无法确定服务端上限")

    if RATE_LIMIT > 0:
        # 打配额用**故意非法**的小请求体，让前 RATE_LIMIT 次在参数校验阶段
        # 几毫秒返回 422，只有第 RATE_LIMIT+1 次才由限流依赖抛 429。
        #
        # 早期版本发的是合法 RAG 请求：真实 qwen3（CPU）单次约 50 秒，而循环
        # 的客户端超时只有 30 秒，于是第一次请求就报「客户端读超时」，报出的
        # 结论与「配额是否生效」毫无关系。
        #
        # 这里不依赖「认证早于参数校验」那套顺序：限流本身也是路由依赖，与
        # 认证同批注册，同样在请求体解析之前执行，因此非法体照样扣配额。
        blocked = None
        attempts = 0
        for attempt in range(RATE_LIMIT + 2):
            attempts = attempt + 1
            response = _safe_post(
                client,
                report,
                "security",
                "配额耗尽 -> 429",
                RAG_PATH,
                {},
                timeout=TIMEOUTS.probe,
            )
            if response is None:
                blocked = None
                break
            if response.status_code == 429:
                blocked = response
                break
        if blocked is None:
            if report.failures and report.failures[-1].label == "配额耗尽 -> 429":
                pass
            else:
                report.failed(
                    "security",
                    "配额耗尽 -> 429",
                    f"打满 {RATE_LIMIT + 2} 次仍未限流",
                )
        else:
            error = blocked.json().get("error", {})
            # httpx 的 Headers 大小写不敏感，可直接用原始大小写取值。
            retry_after = blocked.headers.get("Retry-After")
            if error.get("code") == "rate_limited" and retry_after:
                report.passed(
                    "security",
                    "配额耗尽 -> 429",
                    f"第 {attempts} 次被限流，Retry-After={retry_after}",
                )
            else:
                report.failed(
                    "security",
                    "配额耗尽 -> 429",
                    f"code={error.get('code')} Retry-After={retry_after}",
                )
    else:
        report.skipped("security", "配额耗尽 -> 429", "未给 --rate-limit，无法确定服务端配额")


def warn_mismatched_target(client: httpx.Client, report: Report, args: argparse.Namespace) -> None:
    """``--no-spawn`` 时探一下被测服务是否真带着本次要用的配置。

    自建子进程能靠注入环境变量保证配置一致；``--no-spawn`` 则只能相信外面
    那个服务是对的。而最常见的错法是连到仍在跑的 compose 容器上——它的
    ``AGENT_SERVICE_API_KEY`` / ``MAX_BODY_BYTES`` / ``RATE_LIMIT_PER_MINUTE``
    都是缺省值，会让认证、413、429 三条断言给出与「防护失效」无关的结论。

    这里只做**只读**探测：用一个缺必填字段的小请求体打业务路由，从响应码
    反推认证是否开着。命中不一致时如实报告，不中止脚本——用户可能就是想
    验当前这个服务。
    """
    if not (API_KEY or BODY_LIMIT):
        return

    try:
        probe = client.post(RAG_PATH, json={}, timeout=TIMEOUTS.probe)
    except httpx.HTTPError as exc:
        report.failed(
            "security", "被测目标与本次配置一致", f"探测失败：{type(exc).__name__}: {exc}"
        )
        return

    if API_KEY and probe.status_code != 401:
        report.failed(
            "security",
            "被测目标与本次配置一致",
            f"传了 --api-key 但 {args.host}:{args.port} 未要求认证"
            f"（status={probe.status_code}）——很可能连到了仍以缺省配置运行的旧服务或容器"
            f"（容器占用端口时先 `docker compose down`）",
        )
        return

    if not API_KEY and probe.status_code == 401:
        report.failed(
            "security",
            "被测目标与本次配置一致",
            f"{args.host}:{args.port} 要求认证但本次未传 --api-key，后续业务分组会全部 401",
        )
        return

    report.passed(
        "security",
        "被测目标与本次配置一致",
        f"--no-spawn 目标认证口径吻合（status={probe.status_code}）",
    )


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
        return client.post(path, json=payload, timeout=timeout, headers=_merge_headers(headers))
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
    headers: dict[str, str] | None = None,
) -> httpx.Response | None:
    try:
        return client.get(path, timeout=timeout, headers=_merge_headers(headers))
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
        help=(
            "只跑指定分组，逗号分隔：probe,rag,idempotency,stream,workflow,tools,envelope,security"
        ),
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
    parser.add_argument(
        "--api-key",
        default="",
        help="业务路由的 Bearer 密钥；同时注入子进程（AGENT_SERVICE_API_KEY）",
    )
    parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=0,
        help="子进程的请求体上限；给出后才执行 413 用例（0 表示不覆盖）",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=0,
        help="子进程的每分钟配额；给出后才执行 429 用例（0 表示不覆盖）",
    )
    args = parser.parse_args(argv)

    groups = [item.strip() for item in args.only.split(",") if item.strip()]
    report = Report()
    server: ServerProcess | None = None

    global API_KEY, BODY_LIMIT, RATE_LIMIT, TIMEOUTS  # noqa: PLW0603 —— 脚本级配置
    TIMEOUTS = Timeouts(request=args.client_timeout)
    API_KEY = args.api_key
    BODY_LIMIT = args.max_body_bytes
    RATE_LIMIT = args.rate_limit

    extra_env: dict[str, str] = {}
    if args.timeout_seconds > 0:
        extra_env["AGENT_SERVICE_REQUEST_TIMEOUT_SECONDS"] = str(args.timeout_seconds)
    if args.api_key:
        extra_env["AGENT_SERVICE_API_KEY"] = args.api_key
    if args.max_body_bytes > 0:
        extra_env["AGENT_SERVICE_MAX_BODY_BYTES"] = str(args.max_body_bytes)
    if args.rate_limit > 0:
        extra_env["AGENT_SERVICE_RATE_LIMIT_PER_MINUTE"] = str(args.rate_limit)

    if not args.no_spawn:
        server = ServerProcess(args.port, args.host, extra_env)
        try:
            server.ensure_port_free()
        except PortInUseError as exc:
            # 端口被占时必须直接退出：否则就绪轮询会从占用者那里拿到 200，
            # 整轮断言都打在别人的配置上，失败信息与真实原因毫无关系。
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
    exit_code = 0
    try:
        # trust_env=False：见 ServerProcess.wait_until_ready 的说明——本机若配了
        # HTTP(S)_PROXY，走代理会把本机请求变成 502。
        with httpx.Client(base_url=base_url, timeout=TIMEOUTS.rest, trust_env=False) as client:
            if server is None:
                warn_mismatched_target(client, report, args)
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
            # 入站防护放最后：429 用例会打满配额，先跑会把后面的分组全打成 429。
            if _selected(groups, "security"):
                run_security(client, report)
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
