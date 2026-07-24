"""Day 2 - 带类型的应用配置。

定义 :class:`AppConfig`（基于 :class:`pydantic.BaseModel`）以及从进程环境变量
或 JSON 文件加载它、再把它导出为 JSON 的辅助函数。

用法
-----
作为模块使用::

    from src.config import load_config

    cfg = load_config()  # 环境变量优先，回退到 config.json
    print(cfg.model_name, cfg.api_base_url)

从命令行（冒烟测试）::

    uv run python -m src.config --source auto --path config.json

解析规则（``source="auto"``）
------------------------------------
1. 读取每一个已设置且非空的、与字段同名的环境变量。
   字段名 -> 环境变量名就是 ``field.upper()``。
2. 只要设置了至少一个环境变量，就把结果直接喂给
   :class:`AppConfig`；缺失的必填字段会触发
   :class:`pydantic.ValidationError`。
3. 如果没设置任何环境变量，则回退到 ``config.json``（默认是项目的
   工作目录）。

失败模式是显式的：既没有环境变量 *也* 没有文件 -> 辅助函数直接抛错。
绝不悄悄返回一个只初始化了一半的模型。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """应用配置。

    只有 ``app_name`` 有默认值；其余字段从环境变量或 JSON 加载时都是必填项，
    如果缺失，辅助函数会抛出 :class:`pydantic.ValidationError`。
    """

    app_name: str = "week01-ai-basics"
    model_name: str
    api_base_url: str
    timeout_seconds: float = 30.0
    enable_stream: bool = False

    def env_var_name(self, field: str) -> str:
        """返回映射到 ``field`` 的环境变量名。"""
        if field not in type(self).model_fields:
            msg = f"unknown field: {field!r}"
            raise KeyError(msg)
        return field.upper()


# ---------------------------------------------------------------------------
# 加载器 / 写出器
# ---------------------------------------------------------------------------

Source = Literal["env", "json", "auto"]


def _read_env(env: dict[str, str] | None = None) -> dict[str, Any]:
    """收集每个已声明字段对应的环境变量值。

    空字符串被视为「未设置」，这样环境里一个空的 ``API_BASE_URL=``
    不会悄悄覆盖掉 JSON 来源的值。
    """
    source = os.environ if env is None else env
    out: dict[str, Any] = {}
    for fname in AppConfig.model_fields:
        raw = source.get(fname.upper())
        if raw:
            out[fname] = raw
    return out


def _read_json(path: Path) -> dict[str, Any]:
    """从 ``path`` 读取并解析一个 JSON 对象。"""
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
    """从环境变量、文件，或两者加载 :class:`AppConfig`。

    Args:
        source: ``"env"`` 强制只从环境变量读；``"json"`` 强制只从文件读；
            ``"auto"``（默认）先尝试环境变量，再回退到 ``config_path``。
        config_path: JSON 文件路径（当 ``source`` 为 ``"json"`` 或
            ``"auto"`` 时用到）。
        env: 可选，用于覆盖 ``os.environ``（方便测试和笔记本演示）。
            当 ``source == "json"`` 时忽略。

    Returns:
        经过校验的 :class:`AppConfig`。

    Raises:
        pydantic.ValidationError: 某个必填字段缺失或类型错误。Pydantic 的
            报错信息会列出有问题的字段、期望类型和错误取值。
        FileNotFoundError: ``source == "json"``（或 auto 回退）且文件不存在。
        ValueError: 文件存在但格式错误，或顶层 JSON 值不是对象。
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
        # 本分支里 _read_json 已经会抛错，这里再做一次防御，
        # 以防将来的重构改变了它的行为。
        msg = (
            "no configuration found: set environment variables (MODEL_NAME, "
            "API_BASE_URL, ...) or create a config.json at the project root"
        )
        raise FileNotFoundError(msg)

    # 交给 pydantic 做重活；它抛出的 ValidationError 就是
    # 我们承诺的「显式错误」。
    return AppConfig(**data)


def dump_config(cfg: AppConfig, path: Path | str, *, indent: int = 2) -> None:
    """把 ``cfg`` 以美化后的 UTF-8 JSON 形式写到 ``path``。"""
    p = Path(path)
    p.write_text(
        json.dumps(cfg.model_dump(mode="json"), indent=indent, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI 冒烟测试
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="加载并打印 AppConfig。")
    parser.add_argument(
        "--source",
        choices=("env", "json", "auto"),
        default="auto",
        help="从哪里读取配置（默认：环境变量 -> 回退到 JSON）",
    )
    parser.add_argument(
        "--path",
        default="config.json",
        help="JSON 配置文件路径（默认：./config.json）",
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
