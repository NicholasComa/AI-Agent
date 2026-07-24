"""``src.config`` 的测试。

覆盖正常路径以及每一个文档化了的失败模式。运行命令::

    uv run pytest -q

``pyproject.toml`` 里的 ``pythonpath = ["src"]`` 设置，正是它让
pytest 进程内 ``from config import ...`` 能够工作。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from config import AppConfig, dump_config, load_config

# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_load_from_json_full(tmp_path: Path) -> None:
    """JSON 中包含了所有必填字段 -> AppConfig 被正确填充。"""
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
    assert cfg.app_name == "week01-ai-basics"  # 默认值生效
    assert cfg.model_name == "ai-mini"
    assert cfg.api_base_url == "https://api.example.com/v1"
    assert cfg.timeout_seconds == 15.5
    assert cfg.enable_stream is True


def test_load_from_env_all_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有必填环境变量都设置 -> source='env' 返回 AppConfig。

    Pydantic v2 会把 ``"12.5"`` 强制转成 ``12.5``、``"true"`` 强制转成
    ``True``，这正是基于环境变量加载得以成立的原因。
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
    """dump_config 写出的 JSON，能被 load_config 等价地读回来。"""
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
# 失败模式
# ---------------------------------------------------------------------------


def test_load_from_json_missing_field(tmp_path: Path) -> None:
    """缺失必填字段 -> 指出该字段名的 ValidationError。"""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"model_name": "m"}),  # api_base_url 缺失
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_config(source="json", config_path=cfg_file)

    assert "api_base_url" in str(exc_info.value)


def test_load_from_json_wrong_type(tmp_path: Path) -> None:
    """字段类型错误 -> 指出该字段名的 ValidationError。"""
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
    """不存在的路径 -> 指出该路径的 FileNotFoundError。"""
    missing = tmp_path / "nope.json"

    with pytest.raises(FileNotFoundError) as exc_info:
        load_config(source="json", config_path=missing)

    assert "nope.json" in str(exc_info.value)


def test_load_from_json_top_level_not_object(tmp_path: Path) -> None:
    """顶层 JSON 值是个列表而不是对象 -> ValueError。"""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with pytest.raises(ValueError, match="must be an object"):
        load_config(source="json", config_path=cfg_file)


def test_load_from_json_malformed(tmp_path: Path) -> None:
    """JSON 格式错误 -> 消息里包含文件路径的 ValueError。"""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{this is not json", encoding="utf-8")

    with pytest.raises(ValueError, match=r"invalid JSON in .*config\.json"):
        load_config(source="json", config_path=cfg_file)


def test_env_empty_string_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """被设成 '' 的环境变量被视为「未设置」（不会覆盖默认值）。"""
    monkeypatch.setenv("MODEL_NAME", "m")
    monkeypatch.setenv("API_BASE_URL", "https://x")
    monkeypatch.setenv("TIMEOUT_SECONDS", "")  # 空 -> 被忽略

    cfg = load_config(source="env")

    assert cfg.timeout_seconds == 30.0  # 默认值


# ---------------------------------------------------------------------------
# auto 模式
# ---------------------------------------------------------------------------


def test_load_auto_prefers_env_over_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """auto 模式：当环境变量非空时，JSON 被忽略。"""
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
    """auto 模式：没有环境变量时，使用 JSON 文件。"""
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
    """auto 模式：没有环境变量、也没有文件 -> FileNotFoundError（绝不静默）。"""
    for var in ("MODEL_NAME", "API_BASE_URL", "TIMEOUT_SECONDS", "ENABLE_STREAM", "APP_NAME"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(FileNotFoundError):
        load_config(source="auto", config_path=tmp_path / "absent.json")
