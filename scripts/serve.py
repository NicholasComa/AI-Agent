"""Local ASGI launcher that bypasses Windows AppLocker blocking ``_multiprocessing.pyd``.

Why this file exists
--------------------

``fastapi dev`` and plain ``uvicorn`` both fail to start on machines where
Windows Defender Application Control / AppLocker blocks the Python
standard-library extension DLL ``_multiprocessing.pyd``. The import chain
is::

    fastapi_cli.cli -> uvicorn.supervisors -> uvicorn._subprocess
        -> multiprocessing.connection -> import _multiprocessing  # BLOCKED

Since we never enable ``--reload`` or ``--workers``, nothing in the
process actually needs ``_multiprocessing`` (or the uvicorn supervisor
modules). We replace them with inert stub modules **before** importing
``uvicorn`` so the import chain succeeds and the server starts normally.

Usage::

    uv run python scripts/serve.py
    uv run python scripts/serve.py --host 0.0.0.0 --port 9000

Notes
-----

* This script only patches the *current process*'s ``sys.modules``.
  It does not modify the system Python or the .venv. ``uv sync`` will
  not undo it.
* If you ever need the reloader or multi-worker mode, run on a machine
  where ``_multiprocessing.pyd`` is trusted (or ask IT to allow-list it).
"""

from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path


class _Anything:
    """Recursive sentinel: every attribute access and call returns another ``_Anything``."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __getattr__(self, _name: str) -> _Anything:
        return _Anything()

    def __call__(self, *args: object, **kwargs: object) -> _Anything:
        return _Anything()


def _make_stub_module(name: str) -> types.ModuleType:
    """Build an inert module that exposes ``_Anything`` for every attribute lookup.

    Implements PEP 562 module-level ``__getattr__`` so even *missing*
    attributes resolve to a stub instead of raising ``AttributeError``.
    That covers the case where ``multiprocessing.connection`` does
    ``def _close(self, _close=_multiprocessing.closesocket):`` - the
    default-argument evaluation asks the module for ``closesocket`` and
    our PEP-562 hook returns a stub instead of erroring out.
    """
    module = types.ModuleType(name)
    module.__getattr__ = lambda _name: _Anything  # type: ignore[attr-defined]
    module.__doc__ = f"Stub of {name} (multiprocessing/AppLocker workaround)"
    return module


def _install_stubs() -> None:
    """Replace the offending modules with inert stubs in ``sys.modules``."""

    # 1. The actual DLL that AppLocker blocks.
    sys.modules.setdefault("_multiprocessing", _make_stub_module("_multiprocessing"))

    # 2. The uvicorn internals that *use* _multiprocessing. We don't
    #    need them because we never enable --reload / --workers.
    sys.modules.setdefault("uvicorn._subprocess", _make_stub_module("uvicorn._subprocess"))
    sys.modules.setdefault(
        "uvicorn.supervisors.basereload", _make_stub_module("uvicorn.supervisors.basereload")
    )
    sys.modules.setdefault(
        "uvicorn.supervisors.multiprocess", _make_stub_module("uvicorn.supervisors.multiprocess")
    )
    sys.modules.setdefault("uvicorn.supervisors", _make_stub_module("uvicorn.supervisors"))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Day 5 FastAPI app with uvicorn.")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    parser.add_argument(
        "--app",
        default="src.main:app",
        help="ASGI app import path (default: src.main:app)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    # CRITICAL: stubs must be installed before uvicorn (or anything in
    # its import graph) is imported.
    _install_stubs()

    # Ensure ``src`` is importable regardless of cwd. The default app
    # path ``src.main:app`` lives next to this script's parent dir.
    project_root = Path(__file__).resolve().parent.parent
    src_dir = project_root / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    import uvicorn

    config = uvicorn.Config(
        args.app,
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
    )
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
