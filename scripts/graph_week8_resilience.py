"""韧性演示：RetryPolicy 重试 / Fallback 降级 / Checkpoint 持久化。

运行（Git Bash）：
    uv run python scripts/graph_week8_resilience.py

演示三块：
  1. RetryPolicy：内置重试原语，瞬时失败后自动重试，第 N 次成功。
  2. Fallback：LLM 永久失败时，工作流不崩溃，产出降级结果并记录 errors。
  3. Checkpoint：中断后同一 thread_id 续跑，状态被持久化（InMemorySaver 示例，
     跨进程持久化可换 SqliteSaver）。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")  # noqa: E402  bootstrap: 让 `import graph` 在脚本里可用

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, RetryPolicy  # noqa: E402

from graph.workflow import build_requirement_workflow  # noqa: E402


def _section(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


# ---------------------------------------------------------------------------
# 1. RetryPolicy：内置重试原语
# ---------------------------------------------------------------------------
async def _demo_retry_policy() -> None:
    _section("1. RetryPolicy 重试（内置原语）")
    counter = {"n": 0}

    async def flaky(_state: dict) -> dict:
        counter["n"] += 1
        # 用 ConnectionError 模拟瞬时网络抖动：RetryPolicy 默认对它重试
        if counter["n"] < 3:
            raise ConnectionError(f"瞬时网络错误 #{counter['n']}")
        return {"value": "ok", "attempts": counter["n"]}

    graph = StateGraph(dict)
    graph.add_node("flaky", flaky, retry=RetryPolicy(max_attempts=3))
    graph.add_edge(START, "flaky")
    graph.add_edge("flaky", END)
    app = graph.compile(checkpointer=InMemorySaver())

    result = await app.ainvoke({"x": 1}, {"configurable": {"thread_id": "retry-demo"}})
    print("重试后结果 =", result)
    print("实际尝试次数 =", counter["n"], "(前两次抛错，第三次成功)")


# ---------------------------------------------------------------------------
# 2. Fallback：LLM 永久失败时的降级
# ---------------------------------------------------------------------------
async def _demo_fallback() -> None:
    _section("2. Fallback 降级（LLM 永久失败）")

    async def broken_chat(_messages):  # 永远抛错，模拟模型不可用
        raise RuntimeError("LLM 服务不可用")

    wf = build_requirement_workflow(chat_fn=broken_chat)
    res = await wf.ainvoke(
        {"requirement_text": "我们公司要做一个 B2C 电商网站，支持登录、商品、购物车、支付、后台。"},
        {"configurable": {"thread_id": "fallback-demo"}},
    )
    print("trace =", res["trace"])
    print("category（降级）=", res["category"])
    print("functional_points（降级空）=", res["functional_points"])
    print("errors（记录失败节点）=", res["errors"])
    print("报告仍能生成 =", res["report"].splitlines()[0])


# ---------------------------------------------------------------------------
# 3. Checkpoint：中断 + 同一 thread_id 续跑
# ---------------------------------------------------------------------------
async def _demo_checkpoint() -> None:
    _section("3. Checkpoint 持久化（中断 -> 续跑）")
    wf = build_requirement_workflow()
    cfg = {"configurable": {"thread_id": "checkpoint-demo"}}

    print("--- 3a. 触发歧义中断 ---")
    async for ev in wf.astream({"requirement_text": "我想做个东西"}, cfg, stream_mode="updates"):
        print("event =", ev)
    state = wf.get_state(cfg)
    print("挂起节点 next =", state.next)
    print("已持久化的状态键 =", sorted(state.values.keys()))

    print("\n--- 3b. 同一 thread_id 续跑 ---")
    resumed = await wf.ainvoke(Command(resume="面向零售商的智能推荐系统"), cfg)
    print("resume 后 trace =", resumed["trace"])
    print("human_answers =", resumed["human_answers"])

    print("\n注：跨进程持久化把 InMemorySaver 换成 SqliteSaver 即可：")
    print("    uv add langgraph-checkpoint-sqlite")
    print("    from langgraph.checkpoint.sqlite import SqliteSaver")
    print("    saver = SqliteSaver.from_conn_string('checkpoints.db')")


async def main() -> None:
    await _demo_retry_policy()
    await _demo_fallback()
    await _demo_checkpoint()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
