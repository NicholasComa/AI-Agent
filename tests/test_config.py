"""Tests for ``src.config``.

Covers happy paths and every documented failure mode. Run with::

    uv run pytest -q

The ``pythonpath = ["src"]`` setting in ``pyproject.toml`` is what makes
``from config import ...`` work inside the pytest process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from config import AppConfig, dump_config, load_config

# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_load_from_json_full(tmp_path: Path) -> None:
    """All required fields present in JSON -> AppConfig is populated."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "model_name": "ai-mini",
                "api_base_url": "https://api.example.com/v1",
                "timeout_seconds": 15.5,
                "enable_stream": True,
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(source="json", config_path=cfg_file)

    assert isinstance(cfg, AppConfig)
    assert cfg.app_name == "week01-ai-basics"  # default kicks in
    assert cfg.model_name == "ai-mini"
    assert cfg.api_base_url == "https://api.example.com/v1"
    assert cfg.timeout_seconds == 15.5
    assert cfg.enable_stream is True


def test_load_from_env_all_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """All required env vars set -> source='env' returns AppConfig.

    Pydantic v2 coerces ``"12.5"`` -> ``12.5`` and ``"true"`` -> ``True``,
    which is what makes env-based loading viable.
    """
    monkeypatch.setenv("MODEL_NAME", "ai-mini")
    monkeypatch.setenv("API_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("ENABLE_STREAM", "true")

    cfg = load_config(source="env")

    assert cfg.model_name == "ai-mini"
    assert cfg.api_base_url == "https://api.example.com/v1"
    assert cfg.timeout_seconds == 12.5
    assert cfg.enable_stream is True


def test_dump_and_reload_roundtrip(tmp_path: Path) -> None:
    """dump_config writes JSON that load_config can read back equivalently."""
    cfg_file = tmp_path / "roundtrip.json"
    original = AppConfig(
        model_name="m",
        api_base_url="https://x",
        timeout_seconds=42.0,
        enable_stream=True,
    )

    dump_config(original, cfg_file)
    reloaded = load_config(source="json", config_path=cfg_file)

    assert reloaded == original


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_load_from_json_missing_field(tmp_path: Path) -> None:
    """Missing required field -> ValidationError naming the field."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"model_name": "m"}),  # api_base_url is missing
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_config(source="json", config_path=cfg_file)

    assert "api_base_url" in str(exc_info.value)


def test_load_from_json_wrong_type(tmp_path: Path) -> None:
    """Wrong type for a field -> ValidationError naming the field."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "model_name": "m",
                "api_base_url": "https://x",
                "timeout_seconds": "not-a-number",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_config(source="json", config_path=cfg_file)

    assert "timeout_seconds" in str(exc_info.value)


def test_load_from_json_file_missing(tmp_path: Path) -> None:
    """Non-existent path -> FileNotFoundError naming the path."""
    missing = tmp_path / "nope.json"

    with pytest.raises(FileNotFoundError) as exc_info:
        load_config(source="json", config_path=missing)

    assert "nope.json" in str(exc_info.value)


def test_load_from_json_top_level_not_object(tmp_path: Path) -> None:
    """Top-level JSON value is a list, not an object -> ValueError."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with pytest.raises(ValueError, match="must be an object"):
        load_config(source="json", config_path=cfg_file)


def test_load_from_json_malformed(tmp_path: Path) -> None:
    """Malformed JSON -> ValueError with file path in the message."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{this is not json", encoding="utf-8")

    with pytest.raises(ValueError, match=r"invalid JSON in .*config\.json"):
        load_config(source="json", config_path=cfg_file)


def test_env_empty_string_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env var set to '' is treated as 'not set' (does not override defaults)."""
    monkeypatch.setenv("MODEL_NAME", "m")
    monkeypatch.setenv("API_BASE_URL", "https://x")
    monkeypatch.setenv("TIMEOUT_SECONDS", "")  # empty -> ignored

    cfg = load_config(source="env")

    assert cfg.timeout_seconds == 30.0  # default


# ---------------------------------------------------------------------------
# auto mode
# ---------------------------------------------------------------------------


def test_load_auto_prefers_env_over_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """auto mode: when env is non-empty, JSON is ignored."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "model_name": "from-json",
                "api_base_url": "https://json",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_NAME", "from-env")
    monkeypatch.setenv("API_BASE_URL", "https://env")

    cfg = load_config(source="auto", config_path=cfg_file)

    assert cfg.model_name == "from-env"
    assert cfg.api_base_url == "https://env"


def test_load_auto_falls_back_to_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """auto mode: with no env vars, JSON file is used."""
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("API_BASE_URL", raising=False)
    monkeypatch.delenv("TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("ENABLE_STREAM", raising=False)
    monkeypatch.delenv("APP_NAME", raising=False)

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "model_name": "from-json",
                "api_base_url": "https://json",
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(source="auto", config_path=cfg_file)

    assert cfg.model_name == "from-json"
    assert cfg.api_base_url == "https://json"


def test_load_auto_no_env_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """auto mode: no env, no file -> FileNotFoundError (never silent)."""
    for var in ("MODEL_NAME", "API_BASE_URL", "TIMEOUT_SECONDS", "ENABLE_STREAM", "APP_NAME"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(FileNotFoundError):
        load_config(source="auto", config_path=tmp_path / "absent.json")
