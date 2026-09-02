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


class CommandPolicyError(Exception):
    """命令白名单校验失败。

    Attributes:
        kind: 错误类别，固定为 ``argument_rejected``（参数/子命令不在白名单）。
        message: 面向调用者的可读说明。
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


_ALLOWED_GIT_SUBCOMMANDS = frozenset({"log", "rev-list", "status", "show", "diff", "ls-files"})
"""允许的 git 只读子命令集合。"""

_ALLOWED_GIT_ARG_PREFIXES = (
    "-n",
    "-1",
    "--date",
    "--pretty",
    "--format",
    "--oneline",
    "--max-count",
    "--no-pager",
)
"""允许的 git 参数前缀；``-n20``、``--date=iso`` 这类合并写法按前缀放行。"""

_ALLOWED_GIT_ARG_EXACT = ("-C", "--")
"""只能精确匹配的 git 参数；``--`` 是分隔符，不能按前缀放行 ``--all`` 这类选项。"""


class GitCommandPolicy:
    """git 子命令与参数白名单：只允许只读操作，防参数注入。

    用法：把拼好的 ``git`` argv 交给 :meth:`validate`，不合法即抛
    :class:`CommandPolicyError`（``kind="argument_rejected"``）。
    路径本身仍由 :class:`SandboxRoot` 约束，本策略是第二道闸。
    """

    def __init__(
        self,
        subcommands: frozenset[str] = _ALLOWED_GIT_SUBCOMMANDS,
        arg_prefixes: tuple[str, ...] = _ALLOWED_GIT_ARG_PREFIXES,
        arg_exact: tuple[str, ...] = _ALLOWED_GIT_ARG_EXACT,
    ) -> None:
        self._subcommands = subcommands
        self._arg_prefixes = arg_prefixes
        self._arg_exact = arg_exact

    def validate(self, argv: list[str]) -> None:
        """校验 argv；不合法抛 :class:`CommandPolicyError`。

        先跳过全局选项（如 ``-C <路径>``），再定位子命令，
        最后逐项校验子命令之后的参数。
        """
        if not argv or argv[0] != "git":
            raise CommandPolicyError("argument_rejected", f"不是合法的 git 命令: {argv!r}")

        index = 1
        while index < len(argv) and argv[index].startswith("-"):
            token = argv[index]
            name = token.split("=", 1)[0]
            if name not in self._arg_exact and not any(
                name.startswith(prefix) for prefix in self._arg_prefixes
            ):
                raise CommandPolicyError("argument_rejected", f"不允许的 git 全局参数: {token!r}")
            if name == "-C" and "=" not in token:
                index += 1  # 跳过 -C 后面的路径值
            index += 1

        if index >= len(argv) or argv[index] not in self._subcommands:
            raise CommandPolicyError("argument_rejected", f"不允许的 git 子命令: {argv[index:]!r}")
        index += 1

        for token in argv[index:]:
            if not token.startswith("-"):
                continue  # 参数值（路径、条数等），不做关键字匹配
            name = token.split("=", 1)[0]
            if name in self._arg_exact:
                continue
            if any(name.startswith(prefix) for prefix in self._arg_prefixes):
                continue
            raise CommandPolicyError("argument_rejected", f"不允许的 git 参数: {token!r}")


class ConfirmationGate:
    """危险操作的人工确认门：未确认的操作先返回 ``needs_confirmation``。

    状态只存在进程内存中，确认一次后同一 key 后续放行；:meth:`revoke`
    用于取消确认。key 由 :meth:`key` 按 ``种类:值`` 生成，保证可预测。
    """

    def __init__(self) -> None:
        self._confirmed: set[str] = set()

    @staticmethod
    def key(kind: str, value: str) -> str:
        """按 ``种类:值`` 生成确认门 key。"""
        return f"{kind}:{value}"

    def require(self, key: str) -> str:
        """查询确认状态：已确认返回 ``confirmed``，否则 ``needs_confirmation``。"""
        return "confirmed" if key in self._confirmed else "needs_confirmation"

    def confirm(self, key: str) -> None:
        """确认一个 key，后续同名操作直接放行。"""
        self._confirmed.add(key)

    def revoke(self, key: str) -> None:
        """撤销确认，让该 key 回到需确认状态。"""
        self._confirmed.discard(key)
