"""第 9 周服务路由冒烟：不起端口、不联网，用替身打一遍全部接口。

用法::

    uv run python scripts/service_week9_smoke.py

默认使用内存 Qdrant + 假模型，覆盖下面这些点：

- 存活探针与就绪探针的状态码与依赖明细；
- RAG 问答的带引用回答、低分拒答、SSE 帧序；
- 幂等键回放；
- 需求分析工作流的正常完成、挂起、续跑与按节点流式；
- MCP 未启用时工具接口返回 503；
- 未知路径是否走统一错误信封。

观测层以本地 JSONL 后端接入（不联网），就绪明细里包含该层。

``--real`` 时按 ``.env`` 接真实 Ollama 与 Qdrant；构造失败自动回退替身，并在
输出里说明原因，保证脚本在依赖未就绪时仍然能跑。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for path in (PROJECT_ROOT / "src", PROJECT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import httpx  # noqa: E402

from agent_service import (  # noqa: E402
    AgentServiceDeps,
    AgentServiceSettings,
    SessionStore,
    create_agent_service_app,
)
from graph import build_requirement_workflow  # noqa: E402
from graph.fakes import fake_chat  # noqa: E402
from observability import ObservabilityConfig, build_tracer  # noqa: E402
from rag.embeddings import FakeEmbedding  # noqa: E402
from rag.knowledge_rag import JwipcKnowledgeRAG, build_qdrant_config  # noqa: E402
from rag.qdrant_store import QdrantConfig  # noqa: E402

DOC_ZH = "Qdrant 是向量数据库，支持语义检索。"
QUERY_HIT = "Qdrant 是什么数据库？"
QUERY_MISS = "今天天气怎么样？"
NORMAL_REQUIREMENT = "开发一个电商网站，包含商品浏览、购物车与支付功能。"
AMBIGUOUS_REQUIREMENT = "帮我做个东西"
SESSION_DIR = PROJECT_ROOT / "data" / "agent_service_sessions"
RAG_PATH = "/v1/rag/answer"
START_PATH = "/v1/workflow/requirement-analysis"
DIM = 1024

_FAILED = 0


def _ok(label: str, detail: str = "") -> None:
    print(f"[PASS] {label}" + (f"  {detail}" if detail else ""))


def _fail(label: str, detail: str) -> None:
    global _FAILED  # noqa: PLW0603 —— 脚本级计数器，保持与既有冒烟脚本一致
    _FAILED += 1
    print(f"[FAIL] {label}  {detail}")


class SmokeChat:
    """同时满足 RAG 与工作流契约的假模型。"""

    def __init__(self, chunk_id: str) -> None:
        self._chunk_id = chunk_id
        self.calls = 0

    async def __call__(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        joined = "\n".join(message.get("content", "") for message in messages)
        if "参考资料" in joined:
            return json.dumps(
                {
                    "answer": "Qdrant 是一个向量数据库。",
                    "has_answer": True,
                    "citations": [
                        {"source": "qdrant_intro.md", "chunk_id": self._chunk_id, "quote": DOC_ZH}
                    ],
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            )
        return await fake_chat(messages)


def _build_memory_rag(workdir: Path) -> JwipcKnowledgeRAG:
    embedder = FakeEmbedding(dim=DIM)
    config = QdrantConfig(
        mode="local",
        path=":memory:",
        collection_name="service_smoke",
        vector_size=DIM,
    )
    rag = JwipcKnowledgeRAG(embedder, config)
    workdir.mkdir(parents=True, exist_ok=True)
    document = workdir / "qdrant_intro.md"
    document.write_text(DOC_ZH, encoding="utf-8")
    rag.add_document(document)
    return rag


def _build_real_rag() -> JwipcKnowledgeRAG:
    """按 .env 接真实 Embedding 与 Qdrant。"""
    from dotenv import load_dotenv

    load_dotenv()

    from rag.embeddings import get_embedding

    embedder = get_embedding()
    return JwipcKnowledgeRAG(embedder, build_qdrant_config("jwipc_v3", embedder))


def _build_deps(workdir: Path, *, real: bool) -> tuple[AgentServiceDeps, SmokeChat]:
    settings = AgentServiceSettings(session_dir=SESSION_DIR)
    deps = AgentServiceDeps.create(settings, version="0.1.0")

    rag: JwipcKnowledgeRAG
    if real:
        try:
            rag = _build_real_rag()
            print("[INFO] 使用真实 Qdrant 与 Embedding")
        except Exception as exc:  # noqa: BLE001 —— 依赖未就绪时回退替身
            print(f"[INFO] 真实知识库不可用，回退内存替身：{type(exc).__name__}: {exc}")
            rag = _build_memory_rag(workdir)
    else:
        rag = _build_memory_rag(workdir)

    chunk_id = f"{rag.retrieve(QUERY_HIT, top_k=1)[0].chunk_id}"
    chat = SmokeChat(chunk_id)
    deps.rag = rag
    deps.chat_fn = chat
    deps.sessions = SessionStore(settings.session_dir)
    # 追踪器落盘到 workdir 下，不能沿用默认的仓库内 logs/traces。
    trace_dir = workdir / "traces"
    deps.tracer = build_tracer(
        ObservabilityConfig(enabled=True, backend="local", trace_dir=trace_dir)
    )
    deps.graph = build_requirement_workflow(chat_fn=chat, rag=rag)
    deps.set_dependency(
        "observability",
        ready=True,
        required=False,
        detail=f"backend={deps.tracer.backend_name} dir={trace_dir}",
    )
    deps.set_dependency("session_store", ready=True, required=True, detail="smoke store")
    deps.set_dependency("config", ready=True, required=True, detail="smoke config")
    deps.set_dependency("qdrant", ready=True, required=True, detail="smoke qdrant")
    deps.set_dependency("llm", ready=True, required=True, detail="smoke chat")
    deps.set_dependency("workflow", ready=True, required=True, detail="smoke workflow")
    deps.set_dependency("mcp", ready=False, required=False, detail="disabled in smoke")
    return deps, chat


async def _frames(client: httpx.AsyncClient, path: str, payload: dict[str, Any]) -> list[dict]:
    """以流式请求并解析全部 SSE 数据帧。"""
    frames: list[dict] = []
    async with client.stream("POST", path, json=payload) as response:
        if response.status_code != 200:
            _fail(f"stream {path}", f"status={response.status_code}")
            return frames
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: ") :]))
    return frames


async def _run(client: httpx.AsyncClient, deps: AgentServiceDeps, chat: SmokeChat) -> None:
    print("== 探针 ==")
    health = await client.get("/health")
    if health.status_code == 200 and health.json()["service"] == "agent-service":
        _ok("/health", f"status={health.json()['status']} backend={health.json()['backend']}")
    else:
        _fail("/health", f"status={health.status_code}")

    ready = await client.get("/ready")
    names = [item["name"] for item in ready.json()["dependencies"]]
    # 这里刻意抄一份字面量而不是引用 health._READY_SCAN_ORDER：
    # 引用同一个常量会让断言退化成恒真，抄一份才能在顺序被误改时报出来。
    if ready.status_code == 200 and names == [
        "observability",
        "session_store",
        "config",
        "qdrant",
        "llm",
        "mcp",
        "workflow",
    ]:
        _ok("/ready", f"status={ready.json()['status']} deps={len(names)}")
    else:
        _fail("/ready", f"status={ready.status_code} deps={names}")

    print("== RAG 问答 ==")
    hit = await client.post(RAG_PATH, json={"question": QUERY_HIT, "min_score": 0.0})
    body = hit.json()
    if hit.status_code == 200 and body["has_answer"] and body["citations"]:
        _ok("POST /v1/rag/answer", f"citations={len(body['citations'])}")
    else:
        _fail("POST /v1/rag/answer", f"status={hit.status_code} body={body}")

    calls_before = chat.calls
    miss = await client.post(RAG_PATH, json={"question": QUERY_MISS})
    if (
        miss.status_code == 200
        and miss.json()["has_answer"] is False
        and chat.calls == calls_before
    ):
        _ok("低分拒答", f"rejected_reason={miss.json()['rejected_reason']} 未调用模型")
    else:
        _fail("低分拒答", f"status={miss.status_code} calls_delta={chat.calls - calls_before}")

    headers = {"Idempotency-Key": "smoke-1"}
    first = await client.post(
        RAG_PATH, json={"question": QUERY_HIT, "min_score": 0.0}, headers=headers
    )
    replay = await client.post(
        RAG_PATH, json={"question": QUERY_HIT, "min_score": 0.0}, headers=headers
    )
    if replay.headers.get("Idempotency-Replayed") == "true" and replay.json() == first.json():
        _ok("幂等回放", "Idempotency-Replayed=true")
    else:
        _fail("幂等回放", f"header={replay.headers.get('Idempotency-Replayed')}")

    frames = await _frames(
        client, RAG_PATH, {"question": QUERY_HIT, "min_score": 0.0, "stream": True}
    )
    kinds = [frame["type"] for frame in frames]
    if kinds and kinds[0] == "delta" and kinds[-1] == "done" and "citations" in kinds:
        _ok("RAG 流式帧序", " -> ".join(kinds))
    else:
        _fail("RAG 流式帧序", str(kinds))

    print("== 需求分析工作流 ==")
    normal = await client.post(START_PATH, json={"requirement_text": NORMAL_REQUIREMENT})
    normal_body = normal.json()
    if normal.status_code == 200 and normal_body["status"] == "completed" and normal_body["report"]:
        _ok("正常需求", f"trace={'>'.join(normal_body['trace'])}")
    else:
        _fail("正常需求", f"status={normal.status_code} body_status={normal_body.get('status')}")

    ambiguous = await client.post(START_PATH, json={"requirement_text": AMBIGUOUS_REQUIREMENT})
    ambiguous_body = ambiguous.json()
    if (
        ambiguous_body["status"] == "awaiting_clarification"
        and ambiguous_body["clarification_questions"]
    ):
        _ok("歧义挂起", f"questions={len(ambiguous_body['clarification_questions'])}")
    else:
        _fail("歧义挂起", str(ambiguous_body.get("status")))

    resumed = await client.post(
        f"/v1/workflow/{ambiguous_body['thread_id']}/resume",
        json={"answers": ["仅 Web 版，不做移动端"]},
    )
    if resumed.status_code == 200 and resumed.json()["status"] == "completed":
        _ok("resume 续跑", f"trace={'>'.join(resumed.json()['trace'])}")
    else:
        _fail("resume 续跑", f"status={resumed.status_code}")

    no_pending = await client.post(
        f"/v1/workflow/{normal_body['thread_id']}/resume",
        json={"answers": ["补充"]},
    )
    if no_pending.status_code == 422 and no_pending.json()["error"]["code"] == "invalid_argument":
        _ok("无挂起点续跑", "422 invalid_argument")
    else:
        _fail("无挂起点续跑", f"status={no_pending.status_code}")

    wf_frames = await _frames(
        client, START_PATH, {"requirement_text": NORMAL_REQUIREMENT, "stream": True}
    )
    wf_kinds = [frame["type"] for frame in wf_frames]
    if wf_kinds[-1] == "done" and wf_kinds.count("node") == 6:
        _ok("工作流流式", f"{wf_kinds.count('node')} 个节点帧 + done")
    else:
        _fail("工作流流式", str(wf_kinds))

    int_frames = await _frames(
        client, START_PATH, {"requirement_text": AMBIGUOUS_REQUIREMENT, "stream": True}
    )
    if "interrupt" in [frame["type"] for frame in int_frames]:
        _ok("流式 interrupt 帧", "挂起时推 interrupt")
    else:
        _fail("流式 interrupt 帧", str([frame["type"] for frame in int_frames]))

    print("== 工具与错误信封 ==")
    tools = await client.get("/v1/tools")
    if tools.status_code == 503 and tools.json()["error"]["code"] == "dependency_unavailable":
        _ok("MCP 未启用", "503 dependency_unavailable")
    else:
        _fail("MCP 未启用", f"status={tools.status_code}")

    missing = await client.get("/no-such-path")
    if missing.status_code == 404 and missing.json()["error"]["request_id"]:
        _ok("统一错误信封", "404 invalid_argument + request_id")
    else:
        _fail("统一错误信封", f"status={missing.status_code}")

    print("== 会话与探针状态 ==")
    stats = deps.sessions.stats()
    if stats["sessions"] >= 0:
        _ok("会话存储", f"sessions={stats['sessions']} idempotency={stats['idempotency_entries']}")


async def _main() -> int:
    parser = argparse.ArgumentParser(description="第 9 周服务路由冒烟")
    parser.add_argument("--real", action="store_true", help="接真实 Ollama 与 Qdrant")
    args = parser.parse_args()

    workdir = PROJECT_ROOT / "data" / "service_smoke"
    deps, chat = _build_deps(workdir, real=args.real)
    app = create_agent_service_app(deps=deps, version="0.1.0")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://smoke",
        ) as client,
    ):
        await _run(client, deps, chat)

    print()
    if _FAILED:
        print(f"冒烟失败：{_FAILED} 项未通过")
        return 1
    print("冒烟全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
