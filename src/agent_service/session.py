"""会话与幂等存储。

两块内容共用同一个落盘目录 ``AGENT_SERVICE_SESSION_DIR``：

- **会话**：``session_id`` 到工作流线程 ``thread_id`` 的绑定，以及每个会话的
  轮次统计。工作流在等待人工补充信息时会挂起，恢复时必须回到同一个线程，
  所以绑定关系必须持久化——进程重启后仍然能续跑。
- **幂等**：以 ``(幂等键, 请求体指纹)`` 为键缓存已完成的响应。检索与生成
  都是有代价的调用，客户端重试时不应该重复执行。

存储格式用 JSONL 追加写：每次变更追加一行，读取时后写的记录覆盖先写的。
相比每次都重写整个文件，追加写在进程被强杀时最多丢最后一行，不会把文件写坏。

并发说明：路由在单个事件循环里执行，字典操作不需要额外加锁；文件写入是
短小的追加写，不做批处理。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SESSIONS_FILE = "sessions.jsonl"
"""会话绑定记录文件名。"""

IDEMPOTENCY_FILE = "idempotency.jsonl"
"""幂等响应缓存文件名。"""

DEFAULT_IDEMPOTENCY_TTL_SECONDS = 600.0
"""幂等缓存保留时长，默认 10 分钟。"""

MAX_SESSION_ID_LENGTH = 64
"""会话 ID 长度上限，避免客户端用超长字符串把记录撑爆。"""


def _now() -> float:
    """当前 Unix 时间戳（秒）。用墙上时间而不是单调时钟，便于跨重启比较过期。"""
    return time.time()


def body_fingerprint(body: Any) -> str:
    """计算请求体指纹。

    ``sort_keys`` 保证同一份内容的不同键顺序得到同一指纹；关掉 ``ensure_ascii``
    让中文按 UTF-8 参与哈希，与直接对原始文本做哈希的直觉一致。
    """
    dumped = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class SessionRecord:
    """一个会话的当前状态。

    Attributes:
        session_id: 客户端提供的会话标识。
        thread_id: 当前绑定的工作流线程，用于挂起后的续跑。
        created_at: 会话创建时间戳。
        updated_at: 最近一次更新时间戳。
        turns: 已执行的轮次数。
    """

    session_id: str
    thread_id: str
    created_at: float
    updated_at: float
    turns: int

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": self.turns,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SessionRecord | None:
        """从一行 JSON 还原记录；字段缺失或类型不对时返回 ``None`` 并跳过。"""
        try:
            return cls(
                session_id=str(payload["session_id"]),
                thread_id=str(payload["thread_id"]),
                created_at=float(payload["created_at"]),
                updated_at=float(payload["updated_at"]),
                turns=int(payload["turns"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class IdempotentEntry:
    """一条已缓存的响应。

    Attributes:
        key: 客户端提供的幂等键。
        body_hash: 请求体指纹；与键一起构成唯一标识。
        status_code: 首次执行时的 HTTP 状态码。
        payload: 首次执行时的响应体。
        stored_at: 写入时间戳，用于过期判断。
    """

    key: str
    body_hash: str
    status_code: int
    payload: dict[str, Any]
    stored_at: float

    @property
    def fingerprint(self) -> str:
        """键与请求体指纹的组合，缓存字典的键。"""
        return f"{self.key}:{self.body_hash}"

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "body_hash": self.body_hash,
            "status_code": self.status_code,
            "payload": self.payload,
            "stored_at": self.stored_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> IdempotentEntry | None:
        try:
            body = payload["payload"]
            if not isinstance(body, dict):
                return None
            return cls(
                key=str(payload["key"]),
                body_hash=str(payload["body_hash"]),
                status_code=int(payload["status_code"]),
                payload=body,
                stored_at=float(payload["stored_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


class SessionStore:
    """会话绑定与幂等缓存的落盘实现。

    构造时会建目录并从既有文件恢复状态，因此也可以当作目录可写性的探活点。

    Args:
        directory: 落盘目录，不存在时自动创建。
        idempotency_ttl_seconds: 幂等缓存有效期；``<= 0`` 表示不过期。

    Example:
        ::

            store = SessionStore(Path("data/agent_service_sessions"))
            thread_id = store.thread_id_for("sess-1")
            store.record_turn("sess-1", thread_id=thread_id)
    """

    def __init__(
        self,
        directory: Path,
        *,
        idempotency_ttl_seconds: float = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
    ) -> None:
        self._directory = Path(directory)
        self._ttl = idempotency_ttl_seconds
        self._sessions: dict[str, SessionRecord] = {}
        self._idempotent: dict[str, IdempotentEntry] = {}
        self._directory.mkdir(parents=True, exist_ok=True)
        self._load()

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def idempotency_ttl_seconds(self) -> float:
        return self._ttl

    # ----- 会话 -----

    def thread_id_for(self, session_id: str) -> str:
        """取会话当前的线程 ID；会话不存在时新建绑定。"""
        self._validate_session_id(session_id)
        record = self._sessions.get(session_id)
        if record is not None:
            return record.thread_id
        return self._bind(session_id, thread_id=uuid.uuid4().hex, turns=0)

    def rotate_thread(self, session_id: str) -> str:
        """给会话换一个全新线程并返回。

        开启一次新需求时使用：LangGraph 的线程会保留上一次的状态，沿用旧线程
        会让新需求读到上一轮的功能点与风险。
        """
        self._validate_session_id(session_id)
        previous = self._sessions.get(session_id)
        return self._bind(
            session_id,
            thread_id=uuid.uuid4().hex,
            turns=previous.turns if previous else 0,
        )

    def record_turn(self, session_id: str, *, thread_id: str) -> SessionRecord:
        """记录一次已完成的轮次。"""
        self._validate_session_id(session_id)
        previous = self._sessions.get(session_id)
        return self._bind(
            session_id,
            thread_id=thread_id,
            turns=(previous.turns if previous else 0) + 1,
        )

    def session(self, session_id: str) -> SessionRecord | None:
        """读取会话记录，不存在时返回 ``None``。"""
        return self._sessions.get(session_id)

    def sessions(self, *, limit: int = 50) -> list[SessionRecord]:
        """按最近更新时间倒序列出会话。"""
        ordered = sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True)
        return ordered[:limit]

    # ----- 幂等 -----

    def lookup(self, key: str, body: Any) -> IdempotentEntry | None:
        """查幂等缓存；键或请求体不匹配、已过期都视为未命中。"""
        entry = self._idempotent.get(f"{key}:{body_fingerprint(body)}")
        if entry is None:
            return None
        if self._expired(entry):
            self._idempotent.pop(entry.fingerprint, None)
            return None
        return entry

    def store(
        self,
        key: str,
        body: Any,
        *,
        status_code: int,
        payload: dict[str, Any],
    ) -> IdempotentEntry:
        """写入一条幂等缓存。"""
        entry = IdempotentEntry(
            key=key,
            body_hash=body_fingerprint(body),
            status_code=status_code,
            payload=payload,
            stored_at=_now(),
        )
        self._idempotent[entry.fingerprint] = entry
        self._append(IDEMPOTENCY_FILE, entry.to_json())
        return entry

    def stats(self) -> dict[str, int]:
        """供指标接口使用的计数摘要。"""
        return {
            "sessions": len(self._sessions),
            "idempotency_entries": len(self._idempotent),
        }

    # ----- 内部实现 -----

    def _bind(self, session_id: str, *, thread_id: str, turns: int) -> str:
        """写入一次会话绑定并返回线程 ID。"""
        previous = self._sessions.get(session_id)
        record = SessionRecord(
            session_id=session_id,
            thread_id=thread_id,
            created_at=previous.created_at if previous else _now(),
            updated_at=_now(),
            turns=turns,
        )
        self._sessions[session_id] = record
        self._append(SESSIONS_FILE, record.to_json())
        return thread_id

    def _expired(self, entry: IdempotentEntry) -> bool:
        if self._ttl <= 0:
            return False
        return _now() - entry.stored_at > self._ttl

    def _append(self, filename: str, payload: dict[str, Any]) -> None:
        """追加一行 JSON；写失败只记日志，不影响本次请求的成败。"""
        path = self._directory / filename
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("session store append failed file=%s err=%s", filename, exc)

    def _load(self) -> None:
        """从落盘文件恢复状态；坏行跳过，过期幂等项丢弃。"""
        for payload in self._read_lines(SESSIONS_FILE):
            record = SessionRecord.from_json(payload)
            if record is not None:
                self._sessions[record.session_id] = record
        for payload in self._read_lines(IDEMPOTENCY_FILE):
            entry = IdempotentEntry.from_json(payload)
            if entry is not None and not self._expired(entry):
                self._idempotent[entry.fingerprint] = entry

    def _read_lines(self, filename: str) -> list[dict[str, Any]]:
        path = self._directory / filename
        if not path.exists():
            return []
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("session store read failed file=%s err=%s", filename, exc)
            return []
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

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        """校验会话 ID：非空且不超长，让异常输入在入口处失败。"""
        if not session_id:
            msg = "session_id must not be empty"
            raise ValueError(msg)
        if len(session_id) > MAX_SESSION_ID_LENGTH:
            msg = f"session_id too long: {len(session_id)} > {MAX_SESSION_ID_LENGTH}"
            raise ValueError(msg)
