"""``observability`` 测试夹具。

只放 fixture；替身与工厂函数在 :mod:`observability_fakes`，测试文件直接
``from observability_fakes import ...`` 复用。本目录**不放** ``__init__.py``，
让 pytest 把目录加入导入路径——与 ``tests/agent_service`` 的组织方式一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from observability_fakes import RecordingBackend, make_tracer

from observability import Tracer


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    """本次用例专属的落盘目录，保证不写进仓库。"""
    return tmp_path / "traces"


@pytest.fixture
def recorded() -> RecordingBackend:
    """记录型替身后端，用来观察门面投递了什么。"""
    return RecordingBackend()


@pytest.fixture
def tracer(tmp_path: Path, recorded: RecordingBackend) -> Tracer:
    """门面 + 替身后端。断言走内存，不读文件。"""
    return make_tracer(tmp_path, backend=recorded)


@pytest.fixture
def jsonl_tracer(tmp_path: Path) -> Tracer:
    """门面 + 真实本地后端，用来验证落盘格式与读回。"""
    return make_tracer(tmp_path)
