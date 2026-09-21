"""配置解析用例。

要点是「配错不等于配成假」：无法识别的取值必须回退默认值，而不是被当成显式
的 ``False``。所有用例都传 ``source=`` 字典，不动进程级环境，因此互不干扰、
也能并行。
"""

from __future__ import annotations

import pytest

from observability import ObservabilityConfig
from observability.config import DEFAULT_LANGFUSE_BASE_URL, DEFAULT_TRACE_DIR, describe


def test_defaults_without_environment() -> None:
    """环境里什么都没有时，取值全部落在保守默认上。"""
    config = ObservabilityConfig.from_env(source={})

    assert config.enabled is True
    assert config.backend == "local"
    assert config.trace_dir == DEFAULT_TRACE_DIR
    assert config.capture_content is False
    assert config.max_content_chars == 2000
    assert config.max_label_chars == 64
    assert config.sample_rate == 1.0
    assert config.langfuse_base_url == DEFAULT_LANGFUSE_BASE_URL
    assert config.langfuse_configured is False


def test_environment_overrides_every_field() -> None:
    """每一项自有配置都能被 ``OBS_*`` 覆盖。"""
    config = ObservabilityConfig.from_env(
        source={
            "OBS_ENABLED": "false",
            "OBS_BACKEND": "LANGFUSE",
            "OBS_TRACE_DIR": "var/traces",
            "OBS_CAPTURE_CONTENT": "true",
            "OBS_MAX_CONTENT_CHARS": "512",
            "OBS_MAX_LABEL_CHARS": "24",
            "OBS_SAMPLE_RATE": "0.25",
            "LANGFUSE_PUBLIC_KEY": "pk-lf-demo",
            "LANGFUSE_SECRET_KEY": "sk-lf-demo",
            "LANGFUSE_BASE_URL": "https://us.cloud.langfuse.com",
            "LANGFUSE_RELEASE": "v0.10.0",
        }
    )

    assert config.enabled is False
    assert config.backend == "langfuse"
    assert str(config.trace_dir).replace("\\", "/") == "var/traces"
    assert config.capture_content is True
    assert config.max_content_chars == 512
    assert config.max_label_chars == 24
    assert config.sample_rate == 0.25
    assert config.langfuse_public_key == "pk-lf-demo"
    assert config.langfuse_secret_key == "sk-lf-demo"
    assert config.langfuse_base_url == "https://us.cloud.langfuse.com"
    assert config.langfuse_release == "v0.10.0"
    assert config.langfuse_configured is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc", 2000),
        ("0", 2000),
        ("-5", 2000),
        ("  ", 2000),
        ("1", 1),
        ("3000", 3000),
    ],
)
def test_invalid_numbers_fall_back_to_defaults(raw: str, expected: int) -> None:
    """非数字与小于下限的取值都回退默认；配错绝不抛异常。"""
    config = ObservabilityConfig.from_env(source={"OBS_MAX_CONTENT_CHARS": raw})

    assert config.max_content_chars == expected


@pytest.mark.parametrize("raw", ["2.5", "-0.1", "nan", "inf", "abc"])
def test_invalid_sample_rate_falls_back(raw: str) -> None:
    """采样率只接受 0 到 1 之间的有限值。"""
    config = ObservabilityConfig.from_env(source={"OBS_SAMPLE_RATE": raw})

    assert config.sample_rate == 1.0


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", " On "])
def test_truthy_spellings_for_capture(raw: str) -> None:
    """常见真值写法都识别。"""
    config = ObservabilityConfig.from_env(source={"OBS_CAPTURE_CONTENT": raw})

    assert config.capture_content is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "No", "off"])
def test_falsy_spellings_for_capture(raw: str) -> None:
    """常见假值写法都识别。"""
    config = ObservabilityConfig.from_env(source={"OBS_CAPTURE_CONTENT": raw})

    assert config.capture_content is False


def test_unrecognized_boolean_falls_back_instead_of_becoming_false() -> None:
    """拼错的布尔值回退默认，而不是被当成显式的假。

    ``OBS_ENABLED`` 的默认是真，因此拼错时必须仍为真；若实现把无法识别的取值
    当作 ``False``，一个拼写错误就会静默关掉整个追踪层，且没有任何报错。
    """
    config = ObservabilityConfig.from_env(source={"OBS_ENABLED": "flase"})

    assert config.enabled is True


def test_unknown_backend_falls_back_to_local() -> None:
    """未登记的后端名回退本地后端。"""
    config = ObservabilityConfig.from_env(source={"OBS_BACKEND": "prometheus"})

    assert config.backend == "local"


def test_langfuse_base_url_accepts_host_alias() -> None:
    """``LANGFUSE_BASE_URL`` 缺省时回退到部分集成示例使用的 ``LANGFUSE_HOST``。

    两者都给出时以官方文档的 ``LANGFUSE_BASE_URL`` 为准。
    """
    only_host = ObservabilityConfig.from_env(source={"LANGFUSE_HOST": "https://lf.internal"})
    assert only_host.langfuse_base_url == "https://lf.internal"

    both = ObservabilityConfig.from_env(
        source={
            "LANGFUSE_HOST": "https://lf.internal",
            "LANGFUSE_BASE_URL": "https://cloud.langfuse.com",
        }
    )
    assert both.langfuse_base_url == "https://cloud.langfuse.com"


def test_credentials_require_both_keys() -> None:
    """只配一个 key 不算配好。"""
    only_public = ObservabilityConfig.from_env(source={"LANGFUSE_PUBLIC_KEY": "pk-lf-demo"})
    only_secret = ObservabilityConfig.from_env(source={"LANGFUSE_SECRET_KEY": "sk-lf-demo"})
    both = ObservabilityConfig.from_env(
        source={"LANGFUSE_PUBLIC_KEY": "pk-lf-demo", "LANGFUSE_SECRET_KEY": "sk-lf-demo"}
    )
    blank = ObservabilityConfig.from_env(
        source={"LANGFUSE_PUBLIC_KEY": "  ", "LANGFUSE_SECRET_KEY": "sk-lf-demo"}
    )

    assert only_public.langfuse_configured is False
    assert only_secret.langfuse_configured is False
    assert blank.langfuse_configured is False
    assert both.langfuse_configured is True


def test_describe_masks_credentials() -> None:
    """配置摘要只给密钥前缀与长度，不得出现完整密钥。"""
    secret = "sk-lf-0123456789abcdef0123456789abcdef"
    config = ObservabilityConfig.from_env(
        source={"LANGFUSE_PUBLIC_KEY": "pk-lf-0123456789", "LANGFUSE_SECRET_KEY": secret}
    )

    summary = describe(config)

    assert summary["langfuse_configured"] is True
    assert summary["langfuse_public_key_hint"] == "pk-lf-...(16)"
    assert secret not in str(summary)
