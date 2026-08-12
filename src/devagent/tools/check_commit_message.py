"""Day 12 — check_commit_message 工具（团队 Conventional Commits 校验）。

设计目标
--------

* 实现一个**最小可执行**的 Conventional Commits 校验器,覆盖团队模板
  核心约束:

  - 形如 ``<type>(<scope>): <subject>`` 或 ``<type>: <scope>: <subject>``(scope 必填)
  - type 在白名单内:``func / feat / fix / docs / style / conf / perm /
    version / patch / other / refactor / perf / test / chore / build / ci / revert``
  - subject 不为空、整行长度 ≤ 72
  - body / footer 可选;**有** body 时各行 ≤ 100 字符

* 校验结果**结构化**:同时返回 ``valid`` / ``errors``(列表,可被模型直接
  回贴给用户) / ``parsed``(解析出的 type / scope / subject / breaking
  / has_body),便于上游做"重写提示"。

契约
----

输入::

    message: str —— 完整的 commit message(支持多行)

输出::

    {
        "ok": True,
        "valid": True,
        "errors": [],
        "parsed": {
            "type": "feat",
            "scope": "app" | None,
            "subject": "...",
            "breaking": False,
            "has_body": False,
        }
    }

    或:

    {
        "ok": True,
        "valid": False,
        "errors": ["<错误 1>", "<错误 2>", ...],
        "parsed": {...},  # 即使校验失败也尽量解析;字段尽量填,缺失为 None
    }

注意
----

失败时**不**抛异常 —— 与其它工具保持一致,模型/上层用信封决策。
"""

from __future__ import annotations

import re
from typing import Any

# 类型白名单(可按团队需要扩展)
ALLOWED_TYPES: frozenset[str] = frozenset(
    {
        "func",
        "feat",
        "fix",
        "docs",
        "style",
        "conf",
        "perm",
        "version",
        "patch",
        "other",
        "refactor",
        "perf",
        "test",
        "chore",
        "build",
        "ci",
        "revert",
    }
)

SUBJECT_MAX = 72
BODY_LINE_MAX = 100

# header: 仅支持两种团队风格（scope 必填,不允许 breaking 标记）
#   括号式（推荐,Conventional Commits 标准）：<type>(<scope>): <subject>
#   冒号式：<type>: <scope>: <subject>
#  - type 必填且在 ALLOWED_TYPES 内 (下文单独再校验)
#  - scope 必填：(scope) 或 ": scope" 两种写法等价;无 scope 视为非法
#  - 不支持 "!" breaking 标记,出现即判非法
#  - 最后 ": " + subject
_HEADER_RE = re.compile(
    r"^(?P<type>[A-Za-z]+)"
    r"(?:\((?P<pscope>[A-Za-z0-9_-]+)\)"
    r"|(?::\s*(?P<cscope>[A-Za-z0-9_-]+)))"
    r":\s(?P<subject>.+)$"
)


def _parse_header(line: str) -> dict[str, Any]:
    """解析首行 header,失败时所有字段返回 None。

    兼容括号式与冒号式两种 scope 写法,统一收敛到 ``scope`` 字段。
    """
    m = _HEADER_RE.match(line)
    if not m:
        return {"type": None, "scope": None, "subject": None, "breaking": None}
    scope = m.group("pscope") or m.group("cscope")
    return {
        "type": m.group("type").lower(),
        "scope": scope,
        "subject": m.group("subject").strip(),
        "breaking": False,
    }


def check_commit_message(message: str) -> dict[str, Any]:
    """按团队 Conventional Commits 模板校验 commit message。"""
    errors: list[str] = []
    if not isinstance(message, str):
        return {
            "ok": True,
            "valid": False,
            "errors": ["message must be a string"],
            "parsed": {
                "type": None,
                "scope": None,
                "subject": None,
                "breaking": None,
                "has_body": False,
            },
        }

    # 拆分首行 / body(首个空行之后即 body)
    if "\n" in message:
        header, _, rest = message.partition("\n")
    else:
        header, rest = message, ""
    header = header.rstrip("\r")
    rest = rest.replace("\r\n", "\n")

    parsed = _parse_header(header)
    has_body = bool(rest.strip())

    # ----- 校验 -----
    if parsed["type"] is None:
        errors.append(
            "header does not match `<type>(<scope>): subject` "
            "或 `<type>: <scope>: subject`（scope 必填）"
        )
    else:
        if parsed["type"] not in ALLOWED_TYPES:
            errors.append(
                f"type '{parsed['type']}' not allowed "
                f"(expected one of: {', '.join(sorted(ALLOWED_TYPES))})"
            )
    if parsed["subject"] is not None and not parsed["subject"].strip():
        errors.append("subject is empty")
    if len(header) > SUBJECT_MAX:
        errors.append(f"header line is {len(header)} chars, max {SUBJECT_MAX}")

    if has_body:
        for i, ln in enumerate(rest.splitlines(), start=2):
            if len(ln) > BODY_LINE_MAX:
                errors.append(f"body line {i} is {len(ln)} chars, max {BODY_LINE_MAX}")

    parsed_out = {
        "type": parsed["type"],
        "scope": parsed["scope"],
        "subject": parsed["subject"],
        "breaking": parsed["breaking"] if parsed["breaking"] is not None else False,
        "has_body": has_body,
    }
    return {
        "ok": True,
        "valid": len(errors) == 0,
        "errors": errors,
        "parsed": parsed_out,
    }
