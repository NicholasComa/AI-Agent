"""本地 JSONL 落盘后端：默认后端，零网络依赖。

按天切片成 ``traces-YYYY-MM-DD.jsonl``：单文件不会随运行时长无限增长，排查
时可以直接 ``tail`` 当天文件，清理也只需按日期删。

写入口径与 :class:`agent_service.session.SessionStore` 一致：追加写一行 UTF-8
JSON，空格不转义（中文保持可读），写失败只记 warning。读回同样逐行解析、
坏行跳过——文件可能被手工改过，也可能是旧版本代码写的。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import SpanRecord
from .base import BackendUnavailableError

logger = logging.getLogger(__name__)

TRACE_FILE_PREFIX = "traces-"
TRACE_FILE_SUFFIX = ".jsonl"


class JsonlBackend:
    """按天切片的 JSONL 追加落盘后端。

    构造时确认目录可用，使「磁盘不可写」在启动阶段就暴露，而不是等到第一次
    记录时才发现——那时已经降级到内存后端、数据已经丢了。
    """

    name = "jsonl"

    def __init__(
        self,
        directory: Path,
        *,
        prefix: str = TRACE_FILE_PREFIX,
        reason: str | None = None,
    ) -> None:
        """初始化并确认目录可写。

        Args:
            directory: 落盘目录，不存在则递归创建。
            prefix: 文件名前缀。
            reason: 若本后端是「远端不可用后退化下来的」，记录原因，便于探针
                展示为什么没走目标后端。

        Raises:
            BackendUnavailableError: 目录无法创建或不可写。

        可写性用「写一个探针文件再删掉」来验证，而不是提前把当天的
        ``traces-*.jsonl`` 建出来——后者会让零流量的进程也留下一个空文件，
        给「今天到底有没有产生 trace」的判断添噪音。
        """
        self._directory = Path(directory)
        self._prefix = prefix
        self._reason = reason
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            probe = self._directory / f".{prefix}probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            msg = f"trace directory is not writable: {self._directory} ({exc})"
            raise BackendUnavailableError(msg) from exc

    @property
    def directory(self) -> Path:
        """落盘目录。"""
        return self._directory

    @property
    def degraded_reason(self) -> str | None:
        """降级原因。"""
        return self._reason

    def path_for(self, when: datetime) -> Path:
        """求给定时间所在当天的文件路径。"""
        return self._directory / f"{self._prefix}{when:%Y-%m-%d}{TRACE_FILE_SUFFIX}"

    def begin(self, record: SpanRecord) -> None:
        """空实现：落盘只在 :meth:`end` 做一次，避免同一 span 被写两遍。"""

    def end(self, record: SpanRecord) -> None:
        """追加一行 JSON；写失败只记日志，绝不影响业务链路。"""
        path = self.path_for(datetime.now(UTC))
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_payload(), ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("observability.jsonl_append_failed file=%s err=%s", path.name, exc)

    def flush(self) -> None:
        """无缓冲区，无需操作；保留方法以对齐后端契约。"""

    async def aclose(self) -> None:
        """无长驻句柄，无需释放。"""

    def read_all(self) -> list[dict[str, Any]]:
        """按文件名顺序读回全部记录。

        Returns:
            解析成功的记录字典列表。空行与解析失败的行被跳过，非字典的
            JSON 值（如裸数组）同样跳过。
        """
        payloads: list[dict[str, Any]] = []
        for path in sorted(self._directory.glob(f"{self._prefix}*{TRACE_FILE_SUFFIX}")):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("observability.jsonl_read_failed file=%s err=%s", path.name, exc)
                continue
            payloads.extend(_parse_lines(content))
        return payloads

    def read_records(self) -> list[SpanRecord]:
        """读回并还原为记录对象，结构不符的行跳过。"""
        records: list[SpanRecord] = []
        for payload in self.read_all():
            record = SpanRecord.from_payload(payload)
            if record is not None:
                records.append(record)
        return records


def _parse_lines(content: str) -> list[dict[str, Any]]:
    """把多行文本解析成字典列表，坏行跳过。"""
    payloads: list[dict[str, Any]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


__all__ = ["TRACE_FILE_PREFIX", "TRACE_FILE_SUFFIX", "JsonlBackend"]
