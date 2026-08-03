"""Day 12 — read_text_file 工具（路径穿越防护 + 只读）。

设计目标
--------

* **唯一**允许读取 :data:`TRAIN_DIR` 及其子树下的文件;其它路径一律拒绝。
* 路径穿越防护:不依赖字符串前缀/正则,而是用
  :meth:`pathlib.Path.resolve` + :meth:`pathlib.Path.is_relative_to`
  —— 抵御 ``../``、符号链接、绝对路径、Windows 盘符切换等。
* 只读:不创建、不写、不删、不执行。
* 大小上限:读前先查 ``stat().st_size``,超过 :data:`MAX_FILE_BYTES`
  拒绝读(避免拉爆模型上下文)。
* 编码兜底:尝试 UTF-8 失败回退 ``errors="replace"``(给模型读 binary 文件
  也不会炸,但返回内容已损失 —— 模型能据此判断该文件非文本)。

契约
----

输入::

    path: str       —— 相对 :data:`TRAIN_DIR` 的路径(支持 ``../`` 但不能越界)
    max_bytes: int  —— 本次读取字节上限(默认 :data:`DEFAULT_MAX_BYTES`)

输出::

    {"ok": True, "content": "<utf-8 文本>", "bytes": <int>, "truncated": False}
    {"ok": False, "error": "<短描述>", "kind": "<分类>", "path": "<原始>"}

失败分类::

    "forbidden"  —— 路径在 TRAIN_DIR 之外(越界)
    "not_found"  —— 文件不存在
    "is_dir"     —— 路径是目录而非文件
    "too_large"  —— 文件超过字节上限
    "io"         —— 读取失败(权限、其他 OS 错误)
    "bad_path"   —— 路径不是字符串或包含 NUL 等非法字符

注意
----

``TRAIN_DIR`` 与 ``MAX_FILE_BYTES`` 默认值定义在本模块顶部;允许在
:func:`configure` 中覆盖(便于测试)。生产路径由 :mod:`src.config` 的
:class:`~src.config.AppConfig` 注入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 默认训练数据根目录(在测试/外部覆盖前生效);真实部署走 AppConfig.train_dir
TRAIN_DIR: Path = Path("./training_data").resolve()
# 默认单次读取字节上限
DEFAULT_MAX_BYTES: int = 50_000
MAX_FILE_BYTES: int = 50_000


def configure(*, train_dir: str | Path | None = None, max_file_bytes: int | None = None) -> None:
    """(测试/启动期)覆盖 TRAIN_DIR 与 MAX_FILE_BYTES。

    Args:
        train_dir: 训练数据根目录。``None`` 时保留现状。
        max_file_bytes: 单文件字节上限。``None`` 时保留现状。
    """
    global TRAIN_DIR, MAX_FILE_BYTES  # noqa: PLW0603 —— 单进程内运行时配置
    if train_dir is not None:
        TRAIN_DIR = Path(train_dir).resolve()
    if max_file_bytes is not None:
        MAX_FILE_BYTES = max_file_bytes


def _error(kind: str, msg: str, path: str) -> dict[str, Any]:
    return {"ok": False, "error": msg, "kind": kind, "path": path}


def _safe_resolve(raw: str, root: Path) -> Path:
    """把字符串路径解析为绝对路径。

    相对路径以 ``root`` 为锚点（而不是 cwd）—— 这样 ``read_text_file("a.txt")``
    永远落在沙箱根目录下，路径穿越防护才能生效。
    """
    if not isinstance(raw, str):
        raise ValueError("path must be a string")
    if "\x00" in raw:  # NUL 是 OS 层禁止的字符
        raise ValueError("path contains NUL")
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    return p.expanduser().resolve()


def read_text_file(path: str, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """在 :data:`TRAIN_DIR` 沙箱内安全读取一个文本文件。

    返回结构化信封,失败时**不**抛异常(模型调用栈需要可控错误)。
    """
    if max_bytes <= 0:
        return _error("bad_path", "max_bytes must be positive", path)

    try:
        target = _safe_resolve(path, TRAIN_DIR)
    except ValueError as exc:
        return _error("bad_path", str(exc), path)

    # 路径穿越防护 —— 必须 is_relative_to(TRAIN_DIR)
    if not target.is_relative_to(TRAIN_DIR):
        return _error(
            "forbidden",
            f"path outside TRAIN_DIR ({TRAIN_DIR})",
            path,
        )

    if not target.exists():
        return _error("not_found", f"no such file: {target}", path)
    if not target.is_file():
        return _error("is_dir", f"not a regular file: {target}", path)

    try:
        size = target.stat().st_size
    except OSError as exc:
        return _error("io", f"stat failed: {exc}", path)
    if size > max_bytes:
        return _error(
            "too_large",
            f"file is {size} bytes, exceeds max_bytes={max_bytes}",
            path,
        )

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _error("io", f"read failed: {exc}", path)

    return {
        "ok": True,
        "content": content,
        "bytes": size,
        "truncated": False,
        "path": str(target),
    }
