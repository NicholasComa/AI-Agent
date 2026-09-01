"""沙箱路径白名单：把所有文件访问限制在配置的根目录内部。"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath


class SandboxError(Exception):
    """沙箱路径校验失败。

    Attributes:
        kind: 错误类别，``bad_path``（空路径/绝对路径/穿越等写法问题）
            或 ``forbidden``（解析后越出沙箱，含符号链接逃逸）。
        message: 面向调用者的可读说明。
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class SandboxRoot:
    """把相对路径安全地解析到沙箱根目录内部。

    拒绝绝对路径、Windows 盘符、UNC 路径、空字节与 ``..`` 穿越；
    解析后再复核一次，符号链接逃逸同样会被拦下。
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, rel_path: str) -> Path:
        """解析沙箱内相对路径；越界一律抛 :class:`SandboxError`。"""
        if not rel_path or not rel_path.strip():
            raise SandboxError("bad_path", "路径为空")
        if "\x00" in rel_path:
            raise SandboxError("bad_path", "路径含非法空字节")

        pure = PureWindowsPath(rel_path)
        if pure.is_absolute() or pure.drive or pure.root:
            raise SandboxError("bad_path", f"不允许绝对路径: {rel_path!r}")
        if ".." in pure.parts:
            raise SandboxError("forbidden", f"不允许路径穿越(..): {rel_path!r}")

        candidate = (self.root / rel_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise SandboxError("forbidden", f"路径越出沙箱: {rel_path!r}")
        return candidate
