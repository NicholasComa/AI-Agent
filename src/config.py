"""Day 2 - typed application configuration.

Defines :class:`AppConfig` (a :class:`pydantic.BaseModel`) and helpers to
load it from either the process environment or a JSON file, and to dump it
back to JSON.

Usage
-----
As a module::

    from src.config import load_config

    cfg = load_config()  # env -> config.json fallback
    print(cfg.model_name, cfg.api_base_url)

From the command line (smoke test)::

    uv run python -m src.config --source auto --path config.json

Resolution rules (``source="auto"``)
------------------------------------
1. Read every field whose matching environment variable is set and non-empty.
   Field name -> env var name is just ``field.upper()``.
2. If at least one env var is set, the result is fed straight into
   :class:`AppConfig`; missing required fields raise
   :class:`pydantic.ValidationError`.
3. If no env vars are set, fall back to ``config.json`` (the project's
   working directory by default).

Failure modes are explicit: no environment *and* no file -> the helper
raises. It never silently returns a partially-initialised model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """Application configuration.

    Only ``app_name`` has a default; the other fields are required when the
    config is loaded from environment or JSON, and the helper will surface
    :class:`pydantic.ValidationError` if any of them is missing.
    """

    app_name: str = "week01-ai-basics"
    model_name: str
    api_base_url: str
    timeout_seconds: float = 30.0
    enable_stream: bool = False

    def env_var_name(self, field: str) -> str:
        """Return the environment variable name that maps to ``field``."""
        if field not in type(self).model_fields:
            msg = f"unknown field: {field!r}"
            raise KeyError(msg)
        return field.upper()


# ---------------------------------------------------------------------------
# Loaders / writers
# ---------------------------------------------------------------------------

Source = Literal["env", "json", "auto"]


def _read_env(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Collect env-var values for every declared field.

    Empty strings are treated as "not set" so that an empty ``API_BASE_URL=``
    in the environment does not silently overwrite a JSON-sourced value.
    """
    source = os.environ if env is None else env
    out: dict[str, Any] = {}
    for fname in AppConfig.model_fields:
        raw = source.get(fname.upper())
        if raw:
            out[fname] = raw
    return out


def _read_json(path: Path) -> dict[str, Any]:
    """Read and parse a JSON object from ``path``."""
    if not path.exists():
        msg = f"config file not found: {path}"
        raise FileNotFoundError(msg)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read config file {path}: {exc}"
        raise ValueError(msg) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"invalid JSON in {path}: {exc.msg} (line {exc.lineno}, col {exc.colno})"
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = f"top-level JSON in {path} must be an object, got {type(data).__name__}"
        raise ValueError(msg)
    return data


def load_config(
    source: Source = "auto",
    config_path: Path | str = "config.json",
    *,
    env: dict[str, str] | None = None,
) -> AppConfig:
    """Load :class:`AppConfig` from env, file, or both.

    Args:
        source: ``"env"`` forces environment-only; ``"json"`` forces
            file-only; ``"auto"`` (default) tries env first and falls
            back to ``config_path``.
        config_path: Path to the JSON file (used when ``source`` is
            ``"json"`` or ``"auto"``).
        env: Optional override for ``os.environ`` (handy for tests and
            notebook demos). Ignored when ``source == "json"``.

    Returns:
        A validated :class:`AppConfig`.

    Raises:
        pydantic.ValidationError: a required field is missing or has the
            wrong type. Pydantic's error message lists the offending
            field, expected type, and the bad value.
        FileNotFoundError: ``source == "json"`` (or auto-fallback) and
            the file does not exist.
        ValueError: the file exists but is malformed or its top-level
            JSON value is not an object.
    """
    path = Path(config_path)
    if source == "env":
        data = _read_env(env)
    elif source == "json":
        data = _read_json(path)
    elif source == "auto":
        data = _read_env(env) or _read_json(path)
    else:
        msg = f"unknown source: {source!r} (expected 'env', 'json' or 'auto')"
        raise ValueError(msg)

    if not data and source == "auto" and not path.exists():
        # _read_json already raised in this branch, but be defensive in
        # case a future refactor changes that.
        msg = (
            "no configuration found: set environment variables (MODEL_NAME, "
            "API_BASE_URL, ...) or create a config.json at the project root"
        )
        raise FileNotFoundError(msg)

    # Let pydantic do the heavy lifting; its ValidationError is the
    # "explicit error" we promise.
    return AppConfig(**data)


def dump_config(cfg: AppConfig, path: Path | str, *, indent: int = 2) -> None:
    """Serialise ``cfg`` to ``path`` as pretty-printed UTF-8 JSON."""
    p = Path(path)
    p.write_text(
        json.dumps(cfg.model_dump(mode="json"), indent=indent, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Load and print AppConfig.")
    parser.add_argument(
        "--source",
        choices=("env", "json", "auto"),
        default="auto",
        help="where to read the config from (default: env -> json fallback)",
    )
    parser.add_argument(
        "--path",
        default="config.json",
        help="JSON config path (default: ./config.json)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.source, args.path)
    except (ValidationError, FileNotFoundError, ValueError) as exc:
        print(f"[config error] {type(exc).__name__}: {exc}")
        return 1
    print(cfg.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
