"""Day 4 - real Ollama verification of structured output (5x stability + 5 samples).

Run from the project root (``src/`` is added to ``sys.path`` automatically
by this script, so no ``PYTHONPATH`` prefix is needed)::

    uv run python scripts/run_real_ollama.py

Requires the local Ollama daemon running with the ``qwen3:latest`` model
already pulled. Reads ``.env`` for ``API_BASE_URL`` / ``API_KEY`` /
``MODEL_NAME`` / ``TIMEOUT_SECONDS``.

Output is printed to stdout and also saved to
``examples/structured_run_real.json`` for later inspection.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# Make the project's ``src/`` importable no matter how this script is launched
# (``uv run python scripts/...``, plain ``python scripts/...``, or with an
# explicit ``PYTHONPATH=src``). Without this, ``from config import load_config``
# fails with ModuleNotFoundError because ``src/`` is not on sys.path.
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from dotenv import load_dotenv

from config import load_config
from llm_client import LlmClient, LlmError
from prompts import build_messages
from schemas import RequirementAnalysis

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_PATH = REPO_ROOT / "examples" / "requirement_samples.json"
REPORT_PATH = REPO_ROOT / "examples" / "structured_run_real.json"

STABILITY_INPUT = "做一个电商网站，登录+商品浏览+购物车+支付"
STABILITY_RUNS = 5


def _load_samples() -> dict[str, dict]:
    with SAMPLES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


async def _run_one(client: LlmClient, text: str, *, label: str) -> dict:
    """Run a single chat call and return a record dict.

    On success, includes parsed RequirementAnalysis fields. On failure,
    includes the error type/message and the truncated raw text.
    """
    started = time.perf_counter()
    try:
        resp = await client.chat(
            build_messages(text),
            extra_body={
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
            },
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        try:
            parsed = RequirementAnalysis.model_validate_json(resp.text)
        except Exception as e:
            return {
                "label": label,
                "ok": False,
                "stage": "validate",
                "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": elapsed_ms,
                "raw_text": resp.text[:200] if resp.text else "",
            }
        return {
            "label": label,
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "title": parsed.title,
            "category": parsed.category,
            "n_functional": len(parsed.functional_points),
            "n_risks": len(parsed.risks),
            "n_clarification": len(parsed.clarification_questions),
            "confidence": parsed.confidence,
        }
    except LlmError as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {
            "label": label,
            "ok": False,
            "stage": "call",
            "error": f"{type(e).__name__}: {e}",
            "elapsed_ms": elapsed_ms,
        }


async def main() -> int:
    load_dotenv(dotenv_path=REPO_ROOT / ".env")
    cfg = load_config()
    samples = _load_samples()

    print("===== Real Ollama Verification =====")
    print(f"base_url: {cfg.api_base_url}")
    print(f"model:    {cfg.model_name}")
    print(f"timeout:  {cfg.timeout_seconds}s")
    print()

    client = LlmClient(
        base_url=cfg.api_base_url,
        model=cfg.model_name,
        timeout_seconds=cfg.timeout_seconds,
    )
    try:
        # 1) 5x stability on the same input
        print(f"===== 5x stability: same input, {STABILITY_RUNS} runs =====")
        stability_records: list[dict] = []
        for i in range(STABILITY_RUNS):
            rec = await _run_one(client, STABILITY_INPUT, label=f"5x-{i + 1}")
            stability_records.append(rec)
            if rec["ok"]:
                print(
                    f"  run {i + 1}: title={rec['title']!r} | cat={rec['category']} | "
                    f"fp={rec['n_functional']} | risks={rec['n_risks']} | "
                    f"cq={rec['n_clarification']} | conf={rec['confidence']} | "
                    f"{rec['elapsed_ms']}ms"
                )
            else:
                print(f"  run {i + 1}: FAILED ({rec.get('stage')}) - {rec.get('error')}")
        print()

        # 2) 5 samples
        print("===== 5 samples =====")
        sample_records: dict[str, dict] = {}
        for sid, sample in samples.items():
            rec = await _run_one(client, sample["text"], label=sid)
            sample_records[sid] = rec
            if rec["ok"]:
                print(
                    f"  {sid:14s} ({sample['type']:6s}): title={rec['title']!r} | "
                    f"cat={rec['category']} | fp={rec['n_functional']} | "
                    f"conf={rec['confidence']} | {rec['elapsed_ms']}ms"
                )
            else:
                print(f"  {sid:14s}: FAILED ({rec.get('stage')}) - {rec.get('error')}")
        print()

        # Summary
        n_ok_stab = sum(1 for r in stability_records if r["ok"])
        n_ok_samp = sum(1 for r in sample_records.values() if r["ok"])
        print("===== Summary =====")
        print(f"5x stability: {n_ok_stab}/{STABILITY_RUNS} ok")
        print(f"5 samples:    {n_ok_samp}/{len(samples)} ok")
        confs = [r["confidence"] for r in stability_records if r["ok"]]
        if confs:
            print(
                f"5x confidence: min={min(confs):.2f} max={max(confs):.2f} "
                f"mean={sum(confs) / len(confs):.3f}"
            )
        titles = [r["title"] for r in stability_records if r["ok"]]
        if titles:
            same = sum(1 for t in titles if t == titles[0])
            print(f"5x title unique: {len(set(titles))}/{len(titles)}; matches-first: {same}")
        cats = [r["category"] for r in stability_records if r["ok"]]
        if cats:
            print(f"5x category unique: {set(cats)}")
    finally:
        await client.aclose()

    report = {
        "base_url": cfg.api_base_url,
        "model": cfg.model_name,
        "stability": stability_records,
        "samples": sample_records,
    }
    with REPORT_PATH.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存到: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
