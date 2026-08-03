"""Day 6 - Pydantic Settings 配置加载。

Week 02 升级要点（week01 → week02）
-----------------------------------

1. ``AppConfig`` 现在继承 :class:`pydantic_settings.BaseSettings`，可自动从
   进程环境变量和 ``.env`` 文件加载 — 不再只依赖手动 loader。
2. 新增字段（Week 02 才用到）：

   - ``api_key`` —— 密钥从 env 读取，绝不写入日志或异常消息。
   - ``max_retries`` / ``retry_backoff`` —— 退避重试（Day 7 接入）。
   - ``max_concurrency`` —— 上游并发上限（Day 9 接入）。
   - ``provider`` —— ``"openai_compatible"`` 或 ``"ollama"``（Day 7 factory 用）。
   - ``log_level`` / ``log_format`` —— Day 8 JSON 日志用。
   - ``extra_models`` —— ``/models`` 端点上游不可用时的兜底列表（Day 9 用）。

3. ``load_config(source=...)`` 函数 **保留** —— 内部用
   :meth:`AppConfig.model_validate` 而不是直接实例化，确保 ``source="env"``
   / ``"json"`` / ``"auto"`` 三种模式行为与 week01 完全一致（不会触发
   ``.env`` 文件加载）。

字段加载优先级
--------------

- **直接实例化** ``AppConfig()``：env vars → ``.env`` 文件 → 字段默认值。
- **``load_config(source=...)``**：显式传入的 dict / env / JSON（不走 .env）。

失败模式
--------

- 必填字段（``model_name`` / ``api_base_url``）缺失或类型错误 → ``ValidationError``。
- ``provider`` / ``log_level`` / ``log_format`` 取值不在 Literal 范围内 → ``ValidationError``。
- 缺 env 也没文件（``source="auto"``） → ``FileNotFoundError``，绝不静默。

用法
----

直接实例化（自动从 env / .env 文件加载）::

    from src.config import AppConfig

    cfg = AppConfig()  # 自动读 env 与 .env

显式加载（兼容旧用法）::

    from src.config import load_config

    cfg = load_config(source="auto")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

Provider = Literal["openai_compatible", "ollama"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["json", "plain"]


class AppConfig(BaseSettings):
    """应用配置（Day 6 Pydantic Settings 升级版）。

    加载语义
    --------

    ``AppConfig`` 是 :class:`pydantic_settings.BaseSettings`，但通过
    :meth:`settings_customise_sources` **禁用** 自动从 ``os.environ`` 和
    ``.env`` 文件加载 —— 这避免 :meth:`model_validate` 在 ``load_config``
    里被外部 env 污染。所有加载都通过 ``init_settings``（kwargs）显式完成。

    显式加载入口
    ------------

    - :meth:`AppConfig.from_env` —— 从 ``os.environ`` 加载（大小写不敏感）。
    - :meth:`AppConfig.from_env_file` —— 从 ``.env`` 文件 + ``os.environ``
      加载（进程 env 覆盖文件）。
    - :func:`load_config` —— 兼容 week01 入口（``source="env" / "json" /
      "auto"`` 三种模式），**不会** 触发自动加载。

    字段
    ----

    ``api_key`` 默认为空字符串（``""``），调用方用 ``bool(api_key)``
    判断 ``key_configured``；这与 week01 的行为保持一致。
    """

    # ----- 身份 / 必填 -----
    app_name: str = "llm-gateway-demo"
    api_key: str = ""  # 默认空，缺失由调用方检测
    api_base_url: str  # 必填：缺失时 Pydantic 抛 ValidationError
    model_name: str  # 必填：缺失时 Pydantic 抛 ValidationError

    # ----- 调用控制 -----
    timeout_seconds: float = 30.0
    enable_stream: bool = True  # Week 02 默认开启流式（Week 01 为 False）
    max_retries: int = 2
    retry_backoff: float = 0.5
    max_concurrency: int = 8

    # ----- Provider / 日志 -----
    provider: Provider = "openai_compatible"
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"
    extra_models: list[str] = Field(default_factory=list)

    # ----- Week 03 Agent 工具沙箱 -----
    # TRAIN_DIR：``read_text_file`` 工具唯一允许读取的根目录（路径穿越防护）
    train_dir: str = "./training_data"
    # max_file_bytes：单文件读取上限（防止工具单次拉爆模型上下文）
    max_file_bytes: int = 50_000

    # ----- Pydantic Settings 配置 -----
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """禁用自动从 env / .env / 文件 secrets 加载 —— 只保留 init kwargs。

        避免 :meth:`model_validate` 被外部 env vars 污染。这样
        :func:`load_config` 在 ``source="json"`` 时严格按 JSON 内容校验。
        """
        return (init_settings,)

    # ----- 显式加载入口 -----

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AppConfig:
        """从 ``os.environ``（或 ``env`` 参数）加载。

        大小写不敏感：``MODEL_NAME`` 和 ``model_name`` 等价。空字符串视为未设置。
        """
        import os

        source = env if env is not None else dict(os.environ)
        data: dict[str, Any] = {}
        for fname in cls.model_fields:
            for key in (fname.upper(), fname.lower()):
                if key in source:
                    raw = source[key]
                    if raw:  # 空字符串视为未设置
                        data[fname] = raw
                    break
        return cls.model_validate(data)

    @classmethod
    def from_env_file(cls, env_file: str | Path = ".env") -> AppConfig:
        """从 ``.env`` 文件 + ``os.environ`` 加载（进程 env 覆盖文件）。

        优先级：进程 env vars > .env 文件 > 字段默认值。

        简单的 ``KEY=VALUE`` 解析（不支持 export / 多行 / 命令替换；
        路线明确不要求 dotenv 高级特性）。
        """
        import os

        merged: dict[str, str] = {}
        path = Path(env_file)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                k, v = stripped.split("=", 1)
                k = k.strip()
                v = v.strip()
                if (v.startswith('"') and v.endswith('"')) or (
                    v.startswith("'") and v.endswith("'")
                ):
                    v = v[1:-1]
                merged[k] = v
        # 进程 env 覆盖文件
        for key, value in os.environ.items():
            if value:  # 空字符串不覆盖文件已有值
                merged[key] = value

        data: dict[str, Any] = {}
        for fname in cls.model_fields:
            for key in (fname.upper(), fname.lower()):
                if key in merged and merged[key]:
                    data[fname] = merged[key]
                    break
        return cls.model_validate(data)


# ---------------------------------------------------------------------------
# 加载器 / 写出器（兼容 week01 入口）
# ---------------------------------------------------------------------------

Source = Literal["env", "json", "auto"]


def _read_env(env: dict[str, str] | None = None) -> dict[str, Any]:
    """收集每个已声明字段对应的环境变量值（空字符串视为未设置）。"""
    source = env if env is not None else _current_environ()
    out: dict[str, Any] = {}
    for fname in AppConfig.model_fields:
        # 大小写不敏感（与 BaseSettings 的 case_sensitive=False 对齐）
        for key in (fname.upper(), fname.lower()):
            if key in source:
                raw = source[key]
                if raw:  # 空字符串视为未设置（与 week01 行为一致）
                    out[fname] = raw
                break
    return out


def _current_environ() -> dict[str, str]:
    """返回 os.environ 的副本（测试时可替换）。"""
    import os

    return dict(os.environ)


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
        pydantic.ValidationError: 某个必填字段缺失或类型错误。
        FileNotFoundError: ``source == "json"``（或 auto 回退）且文件不存在。
        ValueError: 文件存在但格式错误，或顶层 JSON 值不是对象。

    Note:
        本函数使用 :meth:`AppConfig.model_validate`，**不会** 触发
        ``.env`` 文件加载（即使它存在）。如需 ``.env`` 文件加载，请直接
        实例化 :class:`AppConfig`。
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
        msg = (
            "no configuration found: set environment variables (MODEL_NAME, "
            "API_BASE_URL, ...) or create a config.json at the project root"
        )
        raise FileNotFoundError(msg)

    # 用 model_validate 而不是 AppConfig(**data)，避免触发 BaseSettings
    # 的自动 .env / env 加载，确保显式 source 语义不变。
    return AppConfig.model_validate(data)


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
