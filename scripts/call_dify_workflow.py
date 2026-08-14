"""Day 16 — verify Dify Cloud Workflow REST API by calling the published
30-minute tutorial workflow ("多平台内容生成器").

WHY THIS SCRIPT
---------------
Day 16 的任务之一是「用 Dify API 调用工作流，确认 REST API 可用」。本脚本
用 httpx 调用已发布的工作流，验证：
  1. 认证（workflow 级 API Key）是否正确
  2. 请求体格式（inputs / response_mode / user）是否被接受
  3. blocking 模式下 `data.outputs` 能否正常返回

WHAT YOU NEED
-------------
在 `.env` 中配置：
  DIFY_API_KEY=<workflow 级密钥，从 Dify Studio → 发布 → API 访问 复制>
  DIFY_BASE_URL=https://api.dify.ai/v1   （不填则使用默认值）

注意：DIFY_API_KEY 是「工作流级」密钥，不是 Dify 账号级密钥。每个应用
独立。复制路径：Studio 打开应用 → 发布 → API 访问 → 密钥/API Key。

RUN
---
    uv run python scripts/call_dify_workflow.py

输出：workflow_run_id、status、耗时、token 数、最终内容（前 500 字预览）。
同时把完整响应当作 JSON 保存到 examples/dify_day16_run.json 供检查。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
REPORT_PATH = REPO_ROOT / "examples" / "dify_day16_run.json"

# 默认 base URL（Dify Cloud）。可用 .env 的 DIFY_BASE_URL 覆盖。
DEFAULT_BASE_URL = "https://api.dify.ai/v1"

# 教程工作流的输入变量（与「用户输入」节点定义一致）
SAMPLE_INPUTS = {
    "draft": (
        "We just launched a new AI writing assistant that helps teams create content 10x faster."
    ),
    "platform": "Twitter and LinkedIn",
    "language": "English",
    "voice_and_tone": "Friendly and enthusiastic, but professional",
    "user_file": [],
}

DIFY_ENDPOINT = "/workflows/run"


def _load_env() -> tuple[str, str]:
    """Return (api_key, base_url) from .env, failing loudly if key missing."""
    load_dotenv(dotenv_path=ENV_PATH)

    api_key = os.getenv("DIFY_API_KEY", "").strip()
    base_url = os.getenv("DIFY_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    if not api_key:
        raise SystemExit(
            "✗ 未找到 DIFY_API_KEY。请在 .env 中设置 workflow 级 API Key\n"
            "  获取路径：Dify Studio → 打开应用 → 发布 → API 访问 → 复制密钥"
        )
    return api_key, base_url


def _print_summary(resp_json: dict) -> None:
    data = resp_json.get("data", {})
    status = data.get("status")
    outputs = data.get("outputs") or {}
    # 教程工作流最终输出变量名是 "output"（模板节点 → 输出节点）
    final = outputs.get("output", json.dumps(outputs, ensure_ascii=False))
    preview = final[:500] if isinstance(final, str) else json.dumps(final, ensure_ascii=False)[:500]

    print("===== Dify Workflow API 调用结果 =====")
    print(f"workflow_run_id : {resp_json.get('workflow_run_id')}")
    print(f"task_id         : {resp_json.get('task_id')}")
    print(f"status          : {status}")
    print(f"elapsed_time    : {data.get('elapsed_time')}s")
    print(f"total_tokens    : {data.get('total_tokens')}")
    if data.get("error"):
        print(f"error           : {data.get('error')}")
    print("\n----- 输出预览（前 500 字）-----")
    print(preview)


def main() -> int:
    api_key, base_url = _load_env()
    url = f"{base_url}{DIFY_ENDPOINT}"

    payload = {
        "inputs": SAMPLE_INPUTS,
        "response_mode": "blocking",
        "user": "day16-verify",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    print(f"POST {url}")
    print(f"inputs.platform = {SAMPLE_INPUTS['platform']!r}")
    print(f"inputs.language = {SAMPLE_INPUTS['language']!r}\n")

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, json=payload, headers=headers)
    except httpx.ConnectError as e:
        raise SystemExit(f"✗ 网络不可达：{e}\n  检查 DIFY_BASE_URL 与网络连接。") from e
    except httpx.TimeoutException:
        raise SystemExit("✗ 请求超时（60s）。Dify Cloud 可能繁忙或网络慢。") from None

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    print(f"HTTP {resp.status_code} · {elapsed_ms}ms\n")

    # 401 = 认证失败（API Key 错误）；其他非 2xx 也打印原始响应便于排查
    if resp.status_code != 200:
        print("✗ 非预期状态码。原始响应：")
        print(resp.text[:1000])
        return 1

    try:
        resp_json = resp.json()
    except json.JSONDecodeError:
        print("✗ 响应不是合法 JSON：")
        print(resp.text[:1000])
        return 1

    _print_summary(resp_json)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_PATH.open("w", encoding="utf-8") as f:
        json.dump(resp_json, f, ensure_ascii=False, indent=2)
    print(f"\n完整响应已保存: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
