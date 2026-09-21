"""可观测性子系统的运行配置。

配置项支持通过 ``OBS_*`` 环境变量覆盖（后端选择、落盘目录、内容捕获开关
等），另有一组 ``LANGFUSE_*`` 由 SDK 自己消费，本模块只负责把它们读进来
做「凭据是否齐全」的判断。

为什么独立成一个模块
--------------------

项目里已有的两个配置类都装不下这组键：

* :class:`config.AppConfig` 没有前缀，且覆盖了 ``settings_customise_sources``
  只保留构造参数（禁用自动读取环境变量与 ``.env``），语义上属于「上游连接」；
* :class:`agent_service.settings.AgentServiceSettings` 的前缀被
  ``AGENT_SERVICE_`` 写死，而一个 ``BaseSettings`` 只能有一个前缀，容不下
  ``LANGFUSE_*``。

因此沿用 :class:`graph.config.WorkflowConfig` 的做法：冻结 dataclass +
手写 ``os.getenv``。解析失败一律静默回退默认值，绝不阻断启动——配置写错
不该让服务起不来，只该让它退化成保守默认行为。
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_PREFIX = "OBS_"
"""追踪层自有配置的环境变量前缀。"""

LANGFUSE_ENV_PREFIX = "LANGFUSE_"
"""Langfuse SDK 读取的环境变量前缀，本模块只读不写。"""

BACKENDS: tuple[str, ...] = ("local", "langfuse")
"""可选后端：本地 JSONL 落盘、Langfuse 上报。"""

DEFAULT_TRACE_DIR = Path("logs/traces")
"""JSONL 缺省落盘目录（``logs/`` 已被 .gitignore 整体忽略）。"""

DEFAULT_LANGFUSE_BASE_URL = "https://cloud.langfuse.com"
"""Langfuse 缺省端点（欧盟区）。美国区为 ``https://us.cloud.langfuse.com``。"""

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _parse_bool(raw: str) -> bool | None:
    """解析布尔值；无法识别的取值返回 ``None`` 而不是 ``False``。

    区分「没配」与「配错」很重要：``OBS_CAPTURE_CONTENT=flase``（拼错）若被
    当成显式的 ``False``，看起来无害，实际掩盖了拼写错误；统一返回 ``None``
    让调用方回退默认值，行为更可预测。
    """
    normalized = raw.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    return None


def _parse_int(raw: str, *, minimum: int) -> int | None:
    """解析整数；非法值或小于下限时返回 ``None``。"""
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= minimum else None


def _parse_rate(raw: str) -> float | None:
    """解析采样率，只接受 0.0 到 1.0 之间的有限值。"""
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return value


def _parse_path(raw: str) -> Path | None:
    """解析落盘目录；空串视为未配置。"""
    text = raw.strip()
    return Path(text) if text else None


def _first_env(env: Mapping[str, str], *names: str) -> str | None:
    """按顺序取第一个非空环境变量值。

    Langfuse 官方文档以 ``LANGFUSE_BASE_URL`` 为准，但部分集成示例使用
    ``LANGFUSE_HOST``。这里按顺序回退，两种写法都能生效。
    """
    for name in names:
        raw = env.get(name)
        if raw is not None and raw.strip():
            return raw.strip()
    return None


@dataclass(frozen=True)
class ObservabilityConfig:
    """追踪层配置，全部字段都有可用默认值。

    默认值刻意偏保守：不联网（``local`` 后端）、不落正文
    （``capture_content=False``）、全量采样（``sample_rate=1.0``）。

    Attributes:
        enabled: 总开关。关闭时全部追踪 API 退化为空操作，零落盘、零上报。
        backend: 目标后端，取值见 :data:`BACKENDS`；非法值回退 ``local``。
        trace_dir: JSONL 落盘目录。
        capture_content: 是否记录提示词与回答正文。默认 ``False`` 时
            :class:`models.SpanRecord` 的 ``content`` 字段恒为 ``None``。
        max_content_chars: 正文单字段截断长度，仅 ``capture_content`` 为真时生效。
        max_label_chars: 属性中字符串值的长度上限，超出即丢弃。
        sample_rate: 采样率，``1.0`` 表示全量。
        langfuse_public_key: Langfuse 公钥。
        langfuse_secret_key: Langfuse 私钥（真实凭据，只应存在于 ``.env``）。
        langfuse_base_url: Langfuse 端点。
        langfuse_release: 可选的发布标识，写进 trace 的版本字段。
    """

    enabled: bool = True
    backend: str = "local"
    trace_dir: Path = DEFAULT_TRACE_DIR
    capture_content: bool = False
    max_content_chars: int = 2000
    max_label_chars: int = 64
    sample_rate: float = 1.0
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = DEFAULT_LANGFUSE_BASE_URL
    langfuse_release: str = ""

    @property
    def langfuse_configured(self) -> bool:
        """凭据是否齐全。

        公钥与私钥**同时非空**才算配好。只填一个几乎总是漏填，此时把它当作
        「已配置」会让后端初始化失败在更深的位置，报错信息也更难懂。
        """
        return bool(self.langfuse_public_key) and bool(self.langfuse_secret_key)

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> ObservabilityConfig:
        """按环境变量构造配置。

        Args:
            source: 环境变量来源；缺省取 :data:`os.environ`。测试可传入普通
                字典，避免改动进程级环境。

        Returns:
            配置实例。任何解析失败都回退到对应字段的默认值。
        """
        env: Mapping[str, str] = os.environ if source is None else source
        defaults = cls()

        def _read(name: str) -> str | None:
            raw = env.get(ENV_PREFIX + name)
            if raw is None or not raw.strip():
                return None
            return raw

        enabled = defaults.enabled
        raw_enabled = _read("ENABLED")
        if raw_enabled is not None:
            parsed_enabled = _parse_bool(raw_enabled)
            if parsed_enabled is None:
                logger.debug("observability.config_invalid key=OBS_ENABLED value=%s", raw_enabled)
            else:
                enabled = parsed_enabled

        backend = defaults.backend
        raw_backend = _read("BACKEND")
        if raw_backend is not None:
            candidate = raw_backend.strip().lower()
            if candidate in BACKENDS:
                backend = candidate
            else:
                logger.debug(
                    "observability.config_invalid key=OBS_BACKEND value=%s fallback=%s",
                    raw_backend,
                    defaults.backend,
                )

        trace_dir = defaults.trace_dir
        raw_trace_dir = _read("TRACE_DIR")
        if raw_trace_dir is not None:
            parsed_path = _parse_path(raw_trace_dir)
            if parsed_path is None:
                logger.debug(
                    "observability.config_invalid key=OBS_TRACE_DIR value=%s", raw_trace_dir
                )
            else:
                trace_dir = parsed_path

        capture_content = defaults.capture_content
        raw_capture = _read("CAPTURE_CONTENT")
        if raw_capture is not None:
            parsed_capture = _parse_bool(raw_capture)
            if parsed_capture is None:
                logger.debug(
                    "observability.config_invalid key=OBS_CAPTURE_CONTENT value=%s", raw_capture
                )
            else:
                capture_content = parsed_capture

        max_content_chars = defaults.max_content_chars
        raw_max_content = _read("MAX_CONTENT_CHARS")
        if raw_max_content is not None:
            parsed_max_content = _parse_int(raw_max_content, minimum=1)
            if parsed_max_content is None:
                logger.debug(
                    "observability.config_invalid key=OBS_MAX_CONTENT_CHARS value=%s",
                    raw_max_content,
                )
            else:
                max_content_chars = parsed_max_content

        max_label_chars = defaults.max_label_chars
        raw_max_label = _read("MAX_LABEL_CHARS")
        if raw_max_label is not None:
            parsed_max_label = _parse_int(raw_max_label, minimum=1)
            if parsed_max_label is None:
                logger.debug(
                    "observability.config_invalid key=OBS_MAX_LABEL_CHARS value=%s", raw_max_label
                )
            else:
                max_label_chars = parsed_max_label

        sample_rate = defaults.sample_rate
        raw_sample_rate = _read("SAMPLE_RATE")
        if raw_sample_rate is not None:
            parsed_sample_rate = _parse_rate(raw_sample_rate)
            if parsed_sample_rate is None:
                logger.debug(
                    "observability.config_invalid key=OBS_SAMPLE_RATE value=%s", raw_sample_rate
                )
            else:
                sample_rate = parsed_sample_rate

        base_url = _first_env(env, LANGFUSE_ENV_PREFIX + "BASE_URL", LANGFUSE_ENV_PREFIX + "HOST")

        return cls(
            enabled=enabled,
            backend=backend,
            trace_dir=trace_dir,
            capture_content=capture_content,
            max_content_chars=max_content_chars,
            max_label_chars=max_label_chars,
            sample_rate=sample_rate,
            langfuse_public_key=(env.get(LANGFUSE_ENV_PREFIX + "PUBLIC_KEY") or "").strip(),
            langfuse_secret_key=(env.get(LANGFUSE_ENV_PREFIX + "SECRET_KEY") or "").strip(),
            langfuse_base_url=base_url or defaults.langfuse_base_url,
            langfuse_release=(env.get(LANGFUSE_ENV_PREFIX + "RELEASE") or "").strip(),
        )


def describe(config: ObservabilityConfig) -> dict[str, Any]:
    """导出用于日志与探针展示的配置摘要。

    凭据只保留是否齐全与前 6 位前缀，长度足够辨认用的是哪把 key，又不至于
    把完整密钥写进日志或探针响应。
    """
    return {
        "enabled": config.enabled,
        "backend": config.backend,
        "trace_dir": str(config.trace_dir),
        "capture_content": config.capture_content,
        "sample_rate": config.sample_rate,
        "langfuse_base_url": config.langfuse_base_url,
        "langfuse_configured": config.langfuse_configured,
        "langfuse_public_key_hint": _key_hint(config.langfuse_public_key),
    }


def _key_hint(key: str) -> str:
    """密钥提示：前 6 位 + 总长度，空值返回空串。"""
    if not key:
        return ""
    return f"{key[:6]}...({len(key)})"


__all__ = [
    "BACKENDS",
    "DEFAULT_LANGFUSE_BASE_URL",
    "DEFAULT_TRACE_DIR",
    "ENV_PREFIX",
    "LANGFUSE_ENV_PREFIX",
    "ObservabilityConfig",
    "describe",
]
