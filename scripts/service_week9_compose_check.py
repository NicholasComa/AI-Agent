"""第 9 周工程测试：容器重启与数据持久化。

本脚本只做两件事，都是在 **Docker Compose 环境**下才能验证的：

1. **重启**：``docker compose restart api`` 之后 ``/ready`` 恢复 200，且会话文件
   与幂等缓存仍可读——证明状态落在卷上而不是进程内存里。
2. **持久化**：``docker compose down`` 紧接着 ``up -d``（**不删卷**）之后，
   ``/v1/rag/answer`` 仍能召回，且 ``/ready`` 报出的 ``points_count`` 与重启前
   完全一致——证明向量数据存活在命名卷上。

这两个断言是「容器化是否真的做对了」的核心：镜像重建、进程重启都是日常操作，
数据丢一次就意味着知识库要重新灌，代价极高。

与另外两个脚本的分工：``service_week9_load.py`` 用宿主机进程压并发，
``service_week9_acceptance.py`` 用宿主机进程验接口；本脚本只驱动 compose。

前置条件（脚本会逐项检查，不满足则直接退出并说明原因）::

    1. Docker Desktop 已启动（docker compose version 可用）
    2. 宿主机 6333 端口空闲——本机常有一个名为 qdrant_server 的旧容器占着它
    3. 宿主机模型服务在跑（容器经 host.docker.internal 访问它）

用法::

    # 全流程：重启 + 持久化（约 3-6 分钟，含两次容器重建）
    uv run python scripts/service_week9_compose_check.py

    # 只验重启
    uv run python scripts/service_week9_compose_check.py --only restart

    # 容器已在跑，跳过 up 直接测
    uv run python scripts/service_week9_compose_check.py --skip-up

    # 只做前置检查，不动容器
    uv run python scripts/service_week9_compose_check.py --check-only

退出码 0 表示全部断言通过；1 表示有失败项；2 表示前置条件不满足。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for path in (PROJECT_ROOT / "src", PROJECT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import httpx  # noqa: E402
from service_week9_acceptance import QUESTION, RAG_PATH, Report  # noqa: E402

from agent_service.guards import IDEMPOTENCY_HEADER, REPLAY_HEADER  # noqa: E402

COMPOSE_FILE = PROJECT_ROOT / "compose.yaml"
API_SERVICE = "api"

READY_TIMEOUT_SECONDS = 180.0
"""等待 ``/ready`` 返回 200 的上限。

首次 ``up`` 需要等 Qdrant 健康检查通过 + API 启动并连上模型服务，冷启动可能
超过一分钟，因此给足余量。
"""

READY_POLL_SECONDS = 3.0

COMPOSE_UP_TIMEOUT = 600

_LAST_READY_HINT = ""
"""最近一次 ``wait_until_ready`` 超时时留下的诊断文字。

模块级而非返回值，是为了不把「诊断信息」塞进「就绪响应」的返回类型里污染调用
方签名——调用方只在超时分支读它。
"""


# ---------------------------------------------------------------------------
# docker compose 封装
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """一次外部命令的结果。"""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def brief(self, limit: int = 200) -> str:
        text = (self.stdout or self.stderr).strip().replace("\n", " ")
        return text[:limit]


def _run(argv: list[str], *, timeout: int = 120) -> CommandResult:
    """执行命令并捕获输出；可执行文件缺失时返回 127 而不是抛异常。"""
    try:
        completed = subprocess.run(  # noqa: S603 —— 参数由本脚本构造，无外部输入
            argv,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(argv, 127, "", f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(argv, 124, "", f"timeout after {timeout}s")
    return CommandResult(argv, completed.returncode, completed.stdout, completed.stderr)


def docker_available() -> tuple[bool, str]:
    """判断 docker 与 compose 是否可用。"""
    if shutil.which("docker") is None:
        return False, "找不到 docker 命令，请先启动 Docker Desktop"
    result = _run(["docker", "compose", "version"], timeout=60)
    if not result.ok:
        return False, f"docker compose 不可用：{result.brief()}"
    return True, result.stdout.strip().splitlines()[0] if result.stdout.strip() else "ok"


def host_port_in_use(port: int = 6333) -> tuple[bool, str]:
    """检查宿主机端口是否被**外部**容器占用，并指出占用者。

    本机常驻一个名为 ``qdrant_server`` 的容器占着 6333（本地非容器工作流用它），
    compose 的 qdrant 也发布同一个端口，因此 ``up`` 前必须让它先停。

    这里刻意排除 compose 栈自己的服务：``--skip-up`` 场景下栈已经在跑，把 compose
    的 ``qdrant`` 一起停掉会直接打断被测服务的依赖，症状是「写入幂等缓存 500、
    重启后 /ready 一直 503」——看起来像服务坏了，其实是测试脚本自己拔了电源。
    """
    names = _external_container_names(port)
    if names:
        return True, ", ".join(names)
    return False, ""


def _external_container_names(port: int) -> list[str]:
    """列出占用端口、且不属于本 compose 项目的容器名。"""
    project = PROJECT_ROOT.name
    result = _run(
        [
            "docker",
            "ps",
            "--filter",
            f"publish={port}",
            "--format",
            '{{.Names}}\t{{.Label "com.docker.compose.project"}}',
        ]
    )
    names: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, _, label = line.partition("\t")
        name = name.strip()
        # compose 生成的容器名形如 <project>-<service>-<index>，label 更可靠；
        # 两者都判一次，避免 label 缺失时漏判。
        if label.strip() == project or name.startswith(f"{project}-"):
            continue
        names.append(name)
    return names


def stop_conflicting(port: int = 6333) -> list[str]:
    """停掉占用目标端口的**外部**容器，返回被停的容器名。

    只停容器不删容器也不动数据，测试结束后可原样 ``docker start`` 恢复。
    同样跳过 compose 自己的服务，理由见 :func:`host_port_in_use`。
    """
    names = _external_container_names(port)
    for name in names:
        _run(["docker", "stop", name], timeout=120)
    return names


def compose_up(*, build: bool = False) -> CommandResult:
    """启动 compose 栈（后台）。"""
    argv = ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"]
    if build:
        argv.append("--build")
    return _run(argv, timeout=COMPOSE_UP_TIMEOUT)


def compose_stop(*, remove: bool) -> CommandResult:
    """``down``（不删卷）或 ``restart api``。

    ``down`` 一律不加 ``-v``：删卷就测不出持久化了，这是本脚本最容易写错的
    一处，因此把语义写进参数名而不是靠调用方记得。
    """
    if remove:
        return _run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "down"],
            timeout=180,
        )
    return _run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "restart", API_SERVICE],
        timeout=300,
    )


def compose_ps() -> dict[str, str]:
    """取各服务的当前状态。"""
    result = _run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "ps",
            "--format",
            "{{.Service}}={{.State}}",
        ]
    )
    states: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            service, state = line.split("=", 1)
            states[service.strip()] = state.strip()
    return states


# ---------------------------------------------------------------------------
# HTTP 探针
# ---------------------------------------------------------------------------


def _client(port: int, timeout: float = 30.0) -> httpx.Client:
    """构造直连本机端口的客户端。

    ``trust_env=False`` 必不可少：开发机常配 HTTP(S)_PROXY，httpx 默认会把发往
    127.0.0.1 的请求也交给代理，代理不认识本机端口便回 502，使全部断言假失败。
    """
    return httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        timeout=timeout,
        trust_env=False,
    )


def wait_until_ready(port: int, *, timeout: float = READY_TIMEOUT_SECONDS) -> httpx.Response | None:
    """轮询 ``/ready`` 直到 200，返回最后一次响应；超时返回 ``None``。

    超时不代表进程没起来——也可能是起来了但某个必需依赖没连上。因此这里把最后
    一次响应的依赖明细留下来，供调用方写进失败结论：只报「超时」等于把排障工作
    原封不动丢回给人。

    Args:
        port: 宿主机映射端口。
        timeout: 等待上限（秒）。冷启动含 Qdrant 健康检查与模型连接，给足余量。

    Returns:
        最后一次 200 响应；超时返回 ``None``（明细可经 :attr:`_LAST_READY_HINT` 读取）。
    """
    global _LAST_READY_HINT
    _LAST_READY_HINT = ""
    deadline = time.monotonic() + timeout
    last: httpx.Response | None = None
    with _client(port, timeout=10.0) as client:
        while time.monotonic() < deadline:
            try:
                last = client.get("/ready")
            except httpx.HTTPError as exc:
                _LAST_READY_HINT = f"{type(exc).__name__}: {exc}"
                time.sleep(READY_POLL_SECONDS)
                continue
            if last.status_code == 200:
                return last
            _LAST_READY_HINT = f"status={last.status_code} {_not_ready_names(last)}"
            time.sleep(READY_POLL_SECONDS)
    return None


def _not_ready_names(response: httpx.Response) -> str:
    """列出 ``/ready`` 响应里尚未就绪的必需依赖，供失败结论使用。"""
    try:
        failed = [
            item["name"]
            for item in response.json()["dependencies"]
            if not item.get("ready") and item.get("required")
        ]
    except (ValueError, KeyError, TypeError):
        return "无法解析依赖明细"
    return f"未就绪={failed}" if failed else "所有必需依赖就绪但状态码非 200"


def points_count_of(response: httpx.Response) -> int | None:
    """从 ``/ready`` 响应里取出 qdrant 的 ``points_count``。"""
    try:
        for item in response.json()["dependencies"]:
            if item["name"] != "qdrant":
                continue
            detail = item.get("detail", "")
            marker = "points_count="
            if marker not in detail:
                return None
            return int(detail.split(marker, 1)[1].split()[0])
    except (ValueError, KeyError, TypeError):
        return None
    return None


def session_store_detail(response: httpx.Response) -> str:
    """取 ``session_store`` 的明细文字，用于比对重启前后是否指向同一个目录。"""
    try:
        for item in response.json()["dependencies"]:
            if item["name"] == "session_store":
                return str(item.get("detail", ""))
    except (ValueError, KeyError, TypeError):
        return ""
    return ""


def rag_recall(port: int, *, timeout: float = 300.0) -> tuple[int, dict[str, Any]]:
    """打一次 RAG 问答，返回 ``(状态码, 响应体)``。"""
    with _client(port, timeout=timeout) as client:
        response = client.post(
            RAG_PATH,
            json={"question": QUESTION, "min_score": 0.0},
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body


def _error_hint(response: httpx.Response) -> str:
    """从错误响应里取出 ``error.code``，取不到时退回截断后的原始文本。"""
    try:
        error = response.json().get("error") or {}
        code = error.get("code")
        if code:
            return f"code={code}"
    except (ValueError, AttributeError):
        pass
    return response.text[:120]


def idempotency_roundtrip(port: int, key: str) -> tuple[bool, str]:
    """写一次幂等缓存再回放，验证缓存跨重启仍可读。

    两处容易写错的地方，都会让这条用例以「写入失败」的形式假报错：

    1. **首次写入必须给足超时**。真实 Qdrant + Ollama 一次生成本机是数十秒量级，
       沿用探针的几秒超时会把正常请求判成失败；
    2. **回放要复用同一个键**。跨重启验证的关键是「重启后旧键仍能命中」，因此
       键由调用方持有并在两组用例间共享，不能在函数内重新生成。

    另外，首次写入未返回 200 时直接报出它的状态码与错误码——否则缓存里根本没
    条目，回放必然不命中，而结论里只会看到一个孤立的 ``replayed=None``。

    Args:
        port: 宿主机映射端口。
        key: 幂等键；重启前后必须一致，否则测的是两次互不相关的请求。

    Returns:
        ``(是否命中缓存, 可读结论)``。
    """
    payload = {"question": QUESTION, "min_score": 0.0}
    headers = {IDEMPOTENCY_HEADER: key}
    with _client(port, timeout=300.0) as client:
        try:
            first = client.post(RAG_PATH, json=payload, headers=headers)
            replayed = client.post(RAG_PATH, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}: {exc}"
    if first.status_code != 200:
        return False, f"首次写入失败 status={first.status_code} {_error_hint(first)}"
    flag = replayed.headers.get(REPLAY_HEADER)
    return flag == "true", f"status={replayed.status_code} replayed={flag}"


# ---------------------------------------------------------------------------
# 两组用例
# ---------------------------------------------------------------------------


def run_restart(report: Report, port: int) -> tuple[httpx.Response | None, int | None]:
    """重启组：restart api 后 /ready 恢复 200，状态仍可读。

    顺序上有一处刻意的安排：**先写幂等缓存，再取重启前的快照与做对比**。写缓存
    会打一次真实问答（本机是数十秒量级），若把它夹在「取快照」与「重启」之间，
    重启组的耗时会被算进「重启后恢复」的等待窗口，导致恢复判定假超时。
    """
    group = "restart"
    print("\n== 重启 ==")

    # 幂等键在两组用例间共享：重启后必须用同一个键命中，才证明条目从卷上恢复。
    replay_key = f"compose-check-persist-{int(time.time())}"

    # 先把缓存写进去（含一次真实生成），再开始计时与取快照。
    wrote, detail = idempotency_roundtrip(port, replay_key)
    if wrote:
        report.passed(group, "重启前写入幂等缓存", detail)
    else:
        report.failed(group, "重启前写入幂等缓存", detail)

    before = wait_until_ready(port)
    if before is None:
        report.failed(group, "重启前 /ready", "容器就绪超时，无法进行重启对比")
        return None, None
    count_before = points_count_of(before)
    store_before = session_store_detail(before)
    report.passed(
        group, "重启前 /ready", f"points_count={count_before} session_store={store_before}"
    )

    result = compose_stop(remove=False)
    if result.ok:
        report.passed(group, f"docker compose restart {API_SERVICE}", result.brief())
    else:
        report.failed(group, f"docker compose restart {API_SERVICE}", result.brief())
        return None, count_before

    after = wait_until_ready(port)
    if after is None:
        report.failed(
            group,
            "重启后 /ready 恢复 200",
            f"超时未恢复（{_LAST_READY_HINT}），见 docker compose logs api",
        )
        return None, count_before

    report.passed(group, "重启后 /ready 恢复 200", "dependencies 完整")
    store_after = session_store_detail(after)
    if store_after == store_before:
        report.passed(group, "会话目录未变", store_after)
    else:
        report.failed(group, "会话目录未变", f"before={store_before} after={store_after}")

    # 用同一个键回放：命中说明条目从卷上恢复，而不是残留在进程内。
    replayed, detail = idempotency_roundtrip(port, replay_key)
    if replayed:
        report.passed(group, "重启后幂等缓存可回放", detail)
    else:
        report.failed(group, "重启后幂等缓存可回放", f"未命中缓存：{detail}")

    return after, points_count_of(after)


def run_persistence(report: Report, port: int, count_before: int | None) -> None:
    """持久化组：down 后 up（不删卷），数据仍在且与重启前一致。"""
    group = "persistence"
    print("\n== 持久化 ==")

    down = compose_stop(remove=True)
    if down.ok:
        report.passed(group, "docker compose down", "未删卷（无 -v）")
    else:
        report.failed(group, "docker compose down", down.brief())
        return

    states = compose_ps()
    if not states:
        report.passed(group, "down 后无残留服务", "compose ps 为空")
    else:
        report.failed(group, "down 后无残留服务", f"仍在运行：{states}")

    up = compose_up()
    if up.ok:
        report.passed(group, "docker compose up -d", result_brief(up))
    else:
        report.failed(group, "docker compose up -d", up.brief())
        return

    after = wait_until_ready(port)
    if after is None:
        report.failed(group, "up 后 /ready 恢复 200", f"超时未恢复（{_LAST_READY_HINT}）")
        return
    report.passed(group, "up 后 /ready 恢复 200", "依赖明细完整")

    count_after = points_count_of(after)
    if count_before is None or count_after is None:
        report.failed(
            group,
            "points_count 与重启前一致",
            f"取不到片段数：before={count_before} after={count_after}",
        )
    elif count_after == count_before:
        report.passed(group, "points_count 与重启前一致", f"points_count={count_after}")
    else:
        report.failed(
            group,
            "points_count 与重启前一致",
            f"before={count_before} after={count_after}（卷可能未复用）",
        )

    status, body = rag_recall(port)
    if status == 200 and body.get("has_answer") and body.get("citations"):
        report.passed(
            group,
            "down/up 后仍能召回",
            f"citations={len(body['citations'])} confidence={body.get('confidence')}",
        )
    else:
        report.failed(group, "down/up 后仍能召回", f"status={status} body={brief_body(body)}")


def result_brief(result: CommandResult) -> str:
    """compose up 的输出很长，只留最后一行有效信息。"""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return lines[-1][:160] if lines else "ok"


def brief_body(body: dict[str, Any]) -> str:
    """压缩响应体，避免把长答案打进报告。"""
    if not body:
        return "<empty>"
    text = json.dumps(body, ensure_ascii=False)
    return text[:200]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第 9 周容器重启与持久化检查")
    parser.add_argument(
        "--port", type=int, default=8080, help="API 映射到宿主机的端口（默认 8080）"
    )
    parser.add_argument(
        "--only",
        default="",
        help="只跑指定组，逗号分隔：restart,persistence",
    )
    parser.add_argument("--skip-up", action="store_true", help="容器已在跑，跳过首次 up -d")
    parser.add_argument("--check-only", action="store_true", help="只做前置检查，不动容器")
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="up 时不加 --build（默认也不加；镜像已存在时更快）",
    )
    args = parser.parse_args(argv)

    groups = [item.strip() for item in args.only.split(",") if item.strip()]

    def selected(name: str) -> bool:
        return not groups or name in groups

    print("== 前置检查 ==")
    ok, detail = docker_available()
    if not ok:
        print(f"[FAIL] {detail}")
        return 2
    print(f"[PASS] {detail}")

    occupied, holder = host_port_in_use(6333)
    if args.check_only:
        # --check-only 承诺「不改动容器」，因此这里只报告不处置。
        if occupied:
            print(f"[WARN] 6333 被占用：{holder}")
            print(f"       腾出端口请执行：docker stop {holder.replace(', ', ' ')}")
        else:
            print("[PASS] 6333 空闲")
        print("\n[INFO] --check-only，前置检查完成，未改动容器")
        return 0

    if occupied:
        print(f"[INFO] 6333 被占用：{holder}；先停掉它（数据在独立卷上，不影响）")
        stopped = stop_conflicting(6333)
        if stopped:
            print(f"[PASS] 已停止：{stopped}")
    else:
        print("[PASS] 6333 空闲")

    report = Report()
    count_before: int | None = None

    if not args.skip_up:
        print("\n[INFO] 启动 compose 栈")
        up = compose_up()
        if not up.ok:
            print(f"[FAIL] docker compose up 失败：{up.brief()}")
            return 2
        if wait_until_ready(args.port) is None:
            print("[FAIL] 容器起来了但 /ready 未在限时内返回 200")
            print(
                _run(
                    ["docker", "compose", "logs", "--no-color", "--tail", "40", API_SERVICE]
                ).stdout
            )
            return 2
        print("[PASS] compose 栈已就绪")

    try:
        if selected("restart"):
            after, count_before = run_restart(report, args.port)
        if selected("persistence"):
            if count_before is None and not selected("restart"):
                # 只跑持久化组时，用当前的 /ready 作为基准。
                current = wait_until_ready(args.port)
                count_before = points_count_of(current) if current is not None else None
            run_persistence(report, args.port, count_before)
    finally:
        pass

    report.dump()
    print()
    if report.failures:
        print(f"容器检查未通过：{len(report.failures)} 项失败")
        print("[提示] 排查用：docker compose logs --no-color --tail 50 api")
        return 1
    print(f"容器检查通过：{len(report.results)} 项断言全部通过")
    # compose 栈此刻仍在运行（持久化组要以「栈还活着」为前提），所以还原本机
    # 常驻 Qdrant 之前必须先停栈、让出 6333，否则 start 会撞端口分配失败。
    # 这两条不合并写：顺序反了会报 "Bind for 0.0.0.0:6333 failed"。
    print("[提示] 收尾还原需两条命令，顺序不能反：")
    print("       1) docker compose down          # 释放 6333 与 8080，不删卷")
    print("       2) docker start qdrant_server   # 还原本机常驻 Qdrant")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
