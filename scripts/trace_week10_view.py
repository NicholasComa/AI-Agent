"""把一条 trace 渲染成自包含 HTML 视图，并可自动截图存证。

用法::

    # 列出本地最近的 trace，挑一条来看
    uv run python scripts/trace_week10_view.py --list

    # 渲染指定 trace 到 HTML
    uv run python scripts/trace_week10_view.py --trace-id <trace_id> --out logs/traces/view.html

    # 渲染并同时截图（Edge 无头模式）
    uv run python scripts/trace_week10_view.py --trace-id <trace_id> \
        --out logs/traces/view.html --screenshot docs/poho/week10/d47_trace_rag.png

不传 ``--trace-id`` 时取本地最新的那条 trace，便于冒烟刚跑完直接看结果。

为什么自己做视图而不只用 Langfuse
---------------------------------

远端实例要联网、要等入库、还要点进界面翻菜单，这三件事都不适合做**离线可重复**
的证据。本脚本只读本地 JSONL：同一份数据任何时候渲染结果都一样，且产出的 HTML
是自包含的（不引外部 CSS/JS），可以直接当交付物归档。

HTML 布局
---------

- 左栏：span 树，按 ``parent_id`` 还原层级，每个节点标注类型与耗时；
- 右栏：时间轴，按 ``started_at`` 定位、按 ``duration_ms`` 画横向条；
- 下方：属性表，逐 span 列出清洗后的元数据与内容哈希。

时间轴的宽度对所有 span 统一按同一条 trace 的总跨度计算，因此「谁耗时更长」是
可以直接目视比较的，而不是每行各自归一化后的假象。
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for path in (PROJECT_ROOT / "src", PROJECT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

DEFAULT_TRACE_DIR = PROJECT_ROOT / "logs" / "traces"
"""本地后端的缺省落盘目录。"""

KIND_COLORS = {
    "trace": "#5b8def",
    "generation": "#c96bd8",
    "retriever": "#3fb28a",
    "tool": "#e0a13c",
    "chain": "#8a8f99",
    "span": "#8a8f99",
}
"""各 span 类型在树与时间轴上的颜色。"""

ELEMENT_ID = "t"
"""各 span 在 HTML 里的锚点前缀，用于树与属性表互相跳转。"""


def read_traces(directory: Path) -> list[dict[str, Any]]:
    """读回目录下全部 trace 行；坏行跳过。

    口径与本地后端一致：解析失败的行只跳过，不让一条坏行挡住整个视图。
    """
    payloads: list[dict[str, Any]] = []
    for path in sorted(directory.glob("traces-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payloads.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return payloads


def group_by_trace(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按 ``trace_id`` 分组，组内按进入时间排序。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        trace_id = str(row.get("trace_id") or "")
        if not trace_id:
            continue
        groups.setdefault(trace_id, []).append(row)
    for spans in groups.values():
        spans.sort(key=lambda item: str(item.get("started_at") or ""))
    return groups


def pick_latest(groups: dict[str, list[dict[str, Any]]]) -> str | None:
    """取最近开始的那条 trace 标识。"""
    latest: tuple[str, str] | None = None
    for trace_id, spans in groups.items():
        started = str(spans[0].get("started_at") or "")
        if latest is None or started > latest[1]:
            latest = (trace_id, started)
    return latest[0] if latest else None


def build_tree(
    spans: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """按 ``parent_id`` 还原层级。

    Returns:
        ``(根列表, 父标识到子列表的映射)``。父标识指向不存在的 span（例如被采样
        丢弃）时，该 span 当成根处理，避免整棵子树因一个断点而消失。
    """
    by_id = {str(span.get("span_id")): span for span in spans}
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for span in spans:
        parent = span.get("parent_id")
        if parent and str(parent) in by_id:
            children.setdefault(str(parent), []).append(span)
        else:
            roots.append(span)
    return roots, children


def timeline_bounds(spans: list[dict[str, Any]]) -> tuple[float, float]:
    """算整条 trace 的时间跨度（毫秒），用作时间轴统一标尺。"""
    starts: list[float] = []
    ends: list[float] = []
    for span in spans:
        started = _parse_time(span.get("started_at"))
        if started is None:
            continue
        starts.append(started)
        ended = _parse_time(span.get("ended_at"))
        ends.append(ended if ended is not None else started)
    if not starts:
        return 0.0, 1.0
    begin = min(starts)
    return begin, max(max(ends) - begin, 0.001)


def _parse_time(value: Any) -> float | None:
    """把 ISO8601 时间戳转成毫秒偏移（相对纪元）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp() * 1000
    except ValueError:
        return None


def render_html(
    trace_id: str,
    spans: list[dict[str, Any]],
    *,
    source: str,
) -> str:
    """渲染自包含 HTML。"""
    roots, children = build_tree(spans)
    begin, total = timeline_bounds(spans)
    kinds = sorted({str(span.get("kind") or "span") for span in spans})
    errors = sum(1 for span in spans if span.get("status") == "error")
    total_ms = sum(float(span.get("duration_ms") or 0.0) for span in spans)

    rows: list[str] = []
    order: list[dict[str, Any]] = []
    _flatten(roots, children, 0, order)
    # build_tree 之后仍可能有没被遍历到的 span（环状父子关系），补到末尾。
    seen = {id(span) for span in order}
    for span in spans:
        if id(span) not in seen:
            order.append(span)
    for index, span in enumerate(order, start=1):
        rows.append(_render_row(index, span, begin=begin, total=total, depth=span.get("_depth", 0)))

    detail_rows = "\n".join(
        _render_detail(index, span) for index, span in enumerate(order, start=1)
    )

    legend = "".join(
        f'<span class="lg"><i style="background:{KIND_COLORS.get(kind, "#8a8f99")}"></i>{html.escape(kind)}</span>'
        for kind in kinds
    )
    return _PAGE.format(
        trace_id=html.escape(trace_id),
        source=html.escape(source),
        count=len(spans),
        kinds=html.escape(", ".join(kinds)),
        errors=errors,
        total_ms=f"{total_ms:.1f}",
        legend=legend,
        rows="\n".join(rows),
        detail_rows=detail_rows,
    )


def _flatten(
    nodes: list[dict[str, Any]],
    children: dict[str, list[dict[str, Any]]],
    depth: int,
    out: list[dict[str, Any]],
) -> None:
    """深度优先展开，记录每个 span 的层级。"""
    for node in nodes:
        node["_depth"] = depth
        out.append(node)
        _flatten(children.get(str(node.get("span_id")), []), children, depth + 1, out)


def _render_row(
    index: int,
    span: dict[str, Any],
    *,
    begin: float,
    total: float,
    depth: int,
) -> str:
    """渲染一行：树节点 + 时间轴条。"""
    kind = str(span.get("kind") or "span")
    color = KIND_COLORS.get(kind, "#8a8f99")
    name = html.escape(str(span.get("name") or "?"))
    duration = float(span.get("duration_ms") or 0.0)
    started = _parse_time(span.get("started_at"))
    offset = ((started - begin) / total * 100) if started is not None else 0.0
    width = max(duration / total * 100, 0.4)
    status = str(span.get("status") or "ok")
    marker = "error" if status == "error" else "ok"
    error_note = ""
    if status == "error":
        error_note = (
            f' <span class="err">{html.escape(str(span.get("error_type") or "error"))}</span>'
        )
    return (
        f'<div class="row">'
        f'<a class="tree" href="#{ELEMENT_ID}{index}" style="padding-left:{depth * 16}px">'
        f'<i style="background:{color}"></i>{name}{error_note}</a>'
        f'<div class="track">'
        f'<div class="bar {marker}" style="left:{offset:.3f}%;width:{width:.3f}%;background:{color}"></div>'
        f"</div>"
        f'<span class="ms">{duration:.1f} ms</span>'
        f"</div>"
    )


def _render_detail(index: int, span: dict[str, Any]) -> str:
    """渲染一个 span 的属性表。"""
    attributes = span.get("attributes") or {}
    content = span.get("content") or {}
    usage = span.get("usage") or {}
    items: list[tuple[str, str]] = [
        ("span_id", str(span.get("span_id") or "")),
        ("parent_id", str(span.get("parent_id") or "-")),
        ("kind", str(span.get("kind") or "")),
        ("status", str(span.get("status") or "")),
        ("started_at", str(span.get("started_at") or "")),
        ("duration_ms", f"{float(span.get('duration_ms') or 0.0):.3f}"),
    ]
    if span.get("model"):
        items.append(("model", str(span["model"])))
    if usage:
        items.append(("usage", json.dumps(usage, ensure_ascii=False)))
    if span.get("cost_usd") is not None:
        items.append(("cost_usd", f"{float(span['cost_usd']):.6f}"))
    if span.get("request_id"):
        items.append(("request_id", str(span["request_id"])))
    if span.get("error_message"):
        items.append(("error_message", str(span["error_message"])))
    if content:
        items.append(("content", json.dumps(content, ensure_ascii=False)))
    if span.get("unfinished"):
        items.append(("unfinished", "true"))
    for key in sorted(attributes):
        items.append((str(key), json.dumps(attributes[key], ensure_ascii=False)))
    cells = "".join(
        f"<tr><th>{html.escape(key)}</th><td>{html.escape(value)}</td></tr>" for key, value in items
    )
    return (
        f'<section class="card" id="{ELEMENT_ID}{index}">'
        f'<h3><span class="idx">#{index}</span> {html.escape(str(span.get("name") or "?"))}'
        f'<span class="kind" style="background:{KIND_COLORS.get(str(span.get("kind") or "span"), "#8a8f99")}">'
        f"{html.escape(str(span.get('kind') or 'span'))}</span></h3>"
        f"<table>{cells}</table></section>"
    )


_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>trace {trace_id}</title>
<style>
  :root {{
    --bg: #ffffff; --panel: #f6f7f9; --line: #d9dde3;
    --text: #1f2329; --muted: #6b7280; --track: #eceef2;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 24px; background: var(--bg); color: var(--text);
         font: 13px/1.6 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); margin-bottom: 16px; font-size: 12px; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 12px; }}
  .meta div {{ background: var(--panel); border: 1px solid var(--line);
               border-radius: 6px; padding: 6px 12px; }}
  .meta b {{ font-weight: 600; }}
  .legend {{ display: flex; gap: 14px; margin-bottom: 14px; font-size: 12px; color: var(--muted); }}
  .lg {{ display: inline-flex; align-items: center; gap: 5px; }}
  .lg i {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
  .panel {{ border: 1px solid var(--line); border-radius: 8px; overflow: hidden; margin-bottom: 18px; }}
  .row {{ display: grid; grid-template-columns: 320px 1fr 90px; align-items: center;
          gap: 10px; padding: 5px 12px; border-bottom: 1px solid var(--panel); }}
  .row:last-child {{ border-bottom: none; }}
  .tree {{ color: var(--text); text-decoration: none; display: flex; align-items: center;
           gap: 7px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .tree:hover {{ color: #2f6fd0; }}
  .tree i {{ width: 8px; height: 8px; border-radius: 2px; flex: 0 0 auto; }}
  .track {{ position: relative; height: 14px; background: var(--track); border-radius: 3px; }}
  .bar {{ position: absolute; top: 0; height: 14px; border-radius: 3px; opacity: .85; }}
  .bar.error {{ background-image: repeating-linear-gradient(45deg, rgba(255,255,255,.45) 0 4px, transparent 4px 8px); }}
  .ms {{ text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }}
  .err {{ color: #c0392b; font-size: 11px; }}
  .card {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; margin-bottom: 10px; }}
  .card h3 {{ font-size: 13px; margin: 0 0 8px; display: flex; align-items: center; gap: 8px; }}
  .idx {{ color: var(--muted); font-weight: 400; }}
  .kind {{ color: #fff; border-radius: 4px; padding: 1px 7px; font-size: 11px; font-weight: 400; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; width: 150px; color: var(--muted); font-weight: 400;
        vertical-align: top; padding: 2px 8px 2px 0; }}
  td {{ padding: 2px 0; word-break: break-all; font-variant-numeric: tabular-nums; }}
  footer {{ color: var(--muted); font-size: 11px; margin-top: 8px; }}
</style>
</head>
<body>
  <h1>Trace {trace_id}</h1>
  <div class="sub">来源：{source}</div>
  <div class="meta">
    <div>span 总数 <b>{count}</b></div>
    <div>类型 <b>{kinds}</b></div>
    <div>错误 span <b>{errors}</b></div>
    <div>累计耗时 <b>{total_ms} ms</b></div>
  </div>
  <div class="legend">{legend}</div>
  <div class="panel">
{rows}
  </div>
  <h2 style="font-size:14px">属性明细</h2>
{detail_rows}
  <footer>由 scripts/trace_week10_view.py 渲染；数据源为本地 JSONL，未捕获正文时仅显示哈希与长度。</footer>
</body>
</html>
"""


def screenshot(html_path: Path, target: Path, *, browser: str | None = None) -> tuple[bool, str]:
    """用 Edge 无头模式对 HTML 截图。

    Returns:
        ``(是否成功, 说明)``。截图是可选步骤，失败不应让整条命令失败——视图
        HTML 本身已经产出，截图只是便于归档的附加物。
    """
    exe = browser or _find_browser()
    if not exe:
        return False, "未找到 Edge 可执行文件，请用 --browser 指定"
    target.parent.mkdir(parents=True, exist_ok=True)
    # ``--screenshot`` 的值由浏览器自己解析，必须给**原生**路径（Windows 上是
    # 反斜杠形式）。传 POSIX 风格路径时 Edge 会静默退出且不写文件，退出码仍是
    # 0，看起来像「跑成功了但没产物」。
    native_target = str(target.resolve())
    command = [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1440,2400",
        "--virtual-time-budget=4000",
        f"--screenshot={native_target}",
        html_path.resolve().as_uri(),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if target.exists():
        return True, str(target)
    detail = (completed.stderr or b"").decode("utf-8", "replace").strip()[:200]
    return False, f"退出码 {completed.returncode} {detail}"


def _find_browser() -> str | None:
    """按常见安装位置找 Edge。"""
    candidates = [
        shutil.which("msedge"),
        shutil.which("microsoft-edge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="渲染本地 trace 为自包含 HTML")
    parser.add_argument("--trace-id", help="要渲染的 trace 标识；缺省取最新一条")
    parser.add_argument("--dir", default=str(DEFAULT_TRACE_DIR), help="JSONL 所在目录")
    parser.add_argument("--out", help="HTML 输出路径；缺省只打印摘要")
    parser.add_argument("--screenshot", help="截图输出路径（PNG）")
    parser.add_argument("--browser", help="浏览器可执行文件路径，缺省自动探测 Edge")
    parser.add_argument("--list", action="store_true", help="列出最近的 trace 后退出")
    args = parser.parse_args(argv)

    directory = Path(args.dir)
    if not directory.exists():
        print(f"目录不存在：{directory}", file=sys.stderr)
        return 1
    groups = group_by_trace(read_traces(directory))
    if not groups:
        print(f"目录下没有可用 trace：{directory}", file=sys.stderr)
        return 1

    if args.list:
        print(f"{'trace_id':34} {'span 数':>7}  {'开始时间':26} 名称")
        ordered = sorted(groups.items(), key=lambda item: str(item[1][0].get("started_at") or ""))
        for trace_id, spans in ordered[-20:]:
            root = next((s for s in spans if s.get("kind") == "trace"), spans[0])
            print(
                f"{trace_id:34} {len(spans):>7}  "
                f"{str(spans[0].get('started_at') or ''):26} {root.get('name')}"
            )
        return 0

    trace_id = args.trace_id or pick_latest(groups)
    if trace_id is None or trace_id not in groups:
        print(f"找不到 trace：{trace_id}", file=sys.stderr)
        return 1
    spans = groups[trace_id]

    kinds = sorted({str(span.get("kind") or "span") for span in spans})
    roots, _ = build_tree(spans)
    print(f"trace_id   : {trace_id}")
    print(f"span 数    : {len(spans)}")
    print(f"类型       : {', '.join(kinds)}")
    print(f"根 span 数 : {len(roots)}")
    for kind in kinds:
        print(f"  {kind:12} {sum(1 for s in spans if str(s.get('kind') or 'span') == kind)}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            render_html(trace_id, spans, source=str(directory)),
            encoding="utf-8",
        )
        print(f"HTML       : {out_path.resolve()}")

        if args.screenshot:
            ok, detail = screenshot(out_path, Path(args.screenshot), browser=args.browser)
            print(("截图       : " if ok else "截图失败   : ") + detail)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
