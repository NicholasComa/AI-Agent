"""Qdrant 向量库配置与封装。

支持两种运行模式：

- ``local`` — 进程内嵌的 Qdrant（``qdrant_client.QdrantClient(path=...)`` 或
  ``":memory:"``）；无需额外服务，适合本地原型与单元测试。
- ``docker`` — 通过 ``host:port`` 连接独立运行的 Qdrant 服务。

对外暴露：

- :class:`QdrantConfig` — 连接与集合配置（不可变 dataclass）。
- :func:`connect` — 构造一个 :class:`QdrantClient`。
- :func:`ensure_collection` — 如果集合不存在则创建。
- :func:`upsert_points` / :func:`search_points` — 数据写入与检索。
- :func:`build_point` — 构造一个 :class:`PointStruct`。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from qdrant_client import QdrantClient
from qdrant_client.http import exceptions as qdrant_exceptions
from qdrant_client.http.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    ScoredPoint,
    VectorParams,
)

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

QdrantMode = Literal["local", "docker"]

DEFAULT_VECTOR_SIZE = 1024  # 与 EmbeddingClient 默认维度对齐


class QdrantConfigError(ValueError):
    """Qdrant 配置或参数错误。"""


class QdrantConnectionError(RuntimeError):
    """Qdrant 连接/操作失败。"""


@dataclass(frozen=True)
class QdrantConfig:
    """Qdrant 连接与集合配置。

    字段语义与 ``.env`` / ``AppConfig`` 对齐，**不在此处写默认值以外的平台信息**。
    """

    mode: QdrantMode = "local"
    path: str = ":memory:"  # local 模式的持久化路径，":memory:" 表示内存
    host: str = "localhost"  # docker 模式使用
    port: int = 6333  # docker 模式使用
    collection_name: str = "rag_chunks"
    vector_size: int = DEFAULT_VECTOR_SIZE  # 必须与 Embedding 输出一致
    distance: Distance = Distance.COSINE

    def __post_init__(self) -> None:
        if self.mode not in ("local", "docker"):
            msg = f"unsupported Qdrant mode: {self.mode!r} (expected 'local' or 'docker')"
            raise QdrantConfigError(msg)
        if self.vector_size <= 0:
            msg = f"vector_size must be > 0, got {self.vector_size}"
            raise QdrantConfigError(msg)
        if not self.collection_name:
            msg = "collection_name must not be empty"
            raise QdrantConfigError(msg)
        if self.mode == "docker" and not (1 <= self.port <= 65535):
            msg = f"port must be in 1..65535, got {self.port}"
            raise QdrantConfigError(msg)

    @classmethod
    def from_app_config(cls, app: AppConfig) -> QdrantConfig:
        """从 :class:`AppConfig` 构造 Qdrant 配置。

        ``AppConfig``（``config.py``）是项目统一的配置源，本方法把其中的
        ``qdrant_*`` 字段映射到本类，使修改 ``.env`` 的 ``QDRANT_*`` 即可
        改变 Qdrant 运行模式 / 集合 / 维度，无需改业务代码。
        """
        from config import AppConfig

        if not isinstance(app, AppConfig):
            msg = f"expected AppConfig, got {type(app).__name__}"
            raise TypeError(msg)
        return cls(
            mode=app.qdrant_mode,
            path=app.qdrant_path,
            host=app.qdrant_host,
            port=app.qdrant_port,
            collection_name=app.qdrant_collection_name,
            vector_size=app.qdrant_vector_size,
        )


def get_qdrant_config(env_file: str = ".env") -> QdrantConfig:
    """从 ``.env``（+ 进程环境变量）读取 ``QDRANT_*`` 构造 Qdrant 配置。

    与 :func:`rag.embeddings.get_embedding` 对称：修改 ``.env`` 即改变
    Qdrant 行为。注意 ``AppConfig`` 要求 ``API_BASE_URL`` / ``MODEL_NAME``
    也存在于配置中（本项目的 ``.env`` 已包含）。
    """
    from config import AppConfig

    return QdrantConfig.from_app_config(AppConfig.from_env_file(env_file))


def connect(cfg: QdrantConfig) -> QdrantClient:
    """按 ``cfg.mode`` 构造 :class:`QdrantClient`。

    Raises:
        QdrantConnectionError: Docker 模式无法连接。
    """
    try:
        if cfg.mode == "local":
            client = QdrantClient(path=cfg.path)
        else:  # docker
            client = QdrantClient(host=cfg.host, port=cfg.port)
    except Exception as exc:
        msg = f"Qdrant connect failed (mode={cfg.mode!r}, host={cfg.host}:{cfg.port}): {exc}"
        raise QdrantConnectionError(msg) from exc
    logger.info(
        "qdrant.connected mode=%s collection=%s vector_size=%d",
        cfg.mode,
        cfg.collection_name,
        cfg.vector_size,
    )
    return client


def ensure_collection(client: QdrantClient, cfg: QdrantConfig) -> None:
    """若集合不存在则创建（``vector_size`` 与 Embedding 维度一致），已存在则跳过。"""
    try:
        exists = client.collection_exists(cfg.collection_name)
    except qdrant_exceptions.UnexpectedResponse as exc:
        msg = f"qdrant: failed to check collection existence: {exc}"
        raise QdrantConnectionError(msg) from exc
    if exists:
        logger.info("qdrant.collection.exists name=%s", cfg.collection_name)
        return
    client.create_collection(
        collection_name=cfg.collection_name,
        vectors_config=VectorParams(size=cfg.vector_size, distance=cfg.distance),
    )
    logger.info(
        "qdrant.collection.created name=%s vector_size=%d distance=%s",
        cfg.collection_name,
        cfg.vector_size,
        cfg.distance.value,
    )


def upsert_points(
    client: QdrantClient,
    cfg: QdrantConfig,
    points: Iterable[PointStruct],
) -> int:
    """向集合写入或更新一批点；返回写入数量。"""
    batch = list(points)
    if not batch:
        return 0
    try:
        client.upsert(collection_name=cfg.collection_name, points=batch, wait=True)
    except qdrant_exceptions.UnexpectedResponse as exc:
        msg = f"qdrant: upsert failed: {exc}"
        raise QdrantConnectionError(msg) from exc
    logger.info("qdrant.upsert collection=%s count=%d", cfg.collection_name, len(batch))
    return len(batch)


def search_points(
    client: QdrantClient,
    cfg: QdrantConfig,
    query_vector: list[float],
    top_k: int = 3,
    source_filter: str | None = None,
) -> list[ScoredPoint]:
    """检索与 ``query_vector`` 最相似的 Top-K 个点。

    Args:
        client: :class:`QdrantClient` 实例。
        cfg: 配置。
        query_vector: 已向量化好的查询向量。
        top_k: 返回条数；``<= 0`` 抛 :class:`QdrantConfigError`。
        source_filter: 可选的 ``source`` Payload 过滤（精确匹配）。

    Returns:
        Qdrant 返回的 :class:`ScoredPoint` 列表（按 score 降序）。
    """
    if top_k <= 0:
        msg = f"top_k must be > 0, got {top_k}"
        raise QdrantConfigError(msg)
    if len(query_vector) != cfg.vector_size:
        msg = f"query_vector dim {len(query_vector)} != collection vector_size {cfg.vector_size}"
        raise QdrantConfigError(msg)
    query_filter: Filter | None = None
    if source_filter is not None:
        query_filter = Filter(
            must=[FieldCondition(key="source", match=MatchValue(value=source_filter))]
        )
    try:
        response = client.query_points(
            collection_name=cfg.collection_name,
            query=query_vector,
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
        )
    except qdrant_exceptions.UnexpectedResponse as exc:
        msg = f"qdrant: search failed: {exc}"
        raise QdrantConnectionError(msg) from exc
    results = list(response.points)
    logger.info(
        "qdrant.search collection=%s top_k=%d returned=%d filter=%s",
        cfg.collection_name,
        top_k,
        len(results),
        source_filter,
    )
    return results


def build_point(
    *,
    point_id: str | int,
    vector: list[float],
    payload: dict[str, Any],
) -> PointStruct:
    """构造一个 :class:`PointStruct`。

    ID 转换规则：

    - ``int`` / 数字字符串 → 直接用作 ID。
    - 其它字符串 → 哈希成确定性 UUID（Qdrant 本地模式要求 ID 为 UUID 或整数）。
    """
    if isinstance(point_id, int):
        pid: str | int = point_id
    elif isinstance(point_id, str) and point_id.isdigit():
        pid = int(point_id)
    else:
        pid = str(uuid.uuid5(uuid.NAMESPACE_DNS, str(point_id)))
    return PointStruct(id=pid, vector=vector, payload=payload)
