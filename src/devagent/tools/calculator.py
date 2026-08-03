"""Day 12 — calculator 工具（安全的算术表达式求值）。

设计目标
--------

* **不** ``eval`` / ``exec`` 表达式 —— 杜绝代码注入。
* 用 :mod:`ast` 解析 + **白名单** 节点集合:只允许
  ``Expression / BinOp / UnaryOp / Constant`` 以及运算符节点
  (``Add / Sub / Mult / Div / Mod / USub / UAdd``)。变量(``Name``)、
  函数调用(``Call``)、属性访问(``Attribute``)、下标(``Subscript``)、
  比较(``Compare``)、布尔运算(``BoolOp``)、位运算、乘方(``**``)等
  一律拒绝。
* 只支持 ``+ - * / %`` 与一元 ``- +``;括号由 AST 自身表达,不需显式支持。
* 拒绝非常数字面量(字符串、列表、字典等),把一切非常量节点都归为"非法"。
* 长度上限(512 字符)防止超大输入拖慢解析。

契约
----

输入::

    expression: str —— 待求值的算术表达式(不超过 512 字符)

输出::

    {"ok": True, "value": <int|float>}                # 求值成功
    {"ok": False, "error": "<短描述>", "kind": "<分类>"}  # 求值失败

失败分类(用于让模型/上层决定下一步)::

    "empty"        —— 输入为空
    "too_long"     —— 超过 512 字符
    "syntax"       —— AST 解析失败
    "unsafe_node"  —— 出现不在白名单的节点(调用/属性/下标/Name 变量等)
    "non_numeric"  —— 字面量不是数字
    "domain"       —— 数学域错误(除零、值过大)
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

_MAX_LEN = 512
_ALLOWED_OPS: dict[type[ast.operator | ast.unaryop], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
# 白名单 = 结构性节点 + 运算符节点；任何不在此集合的 AST 节点（Call /
# Attribute / Subscript / Compare / Name / ...）一律拒绝。
_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.USub,
    ast.UAdd,
)


def _error(kind: str, msg: str) -> dict[str, Any]:
    return {"ok": False, "error": msg, "kind": kind}


def _eval_node(node: ast.AST) -> int | float:
    """递归求值 AST 节点(只接受白名单类型)。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:  # pragma: no cover —— 白名单兜底
            raise ValueError(f"unsupported operator: {type(node.op).__name__}")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:  # pragma: no cover
            raise ValueError(f"unsupported unary op: {type(node.op).__name__}")
        return op(operand)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):  # bool 是 int 的子类,显式拒绝
            raise ValueError("booleans are not allowed")
        if not isinstance(node.value, (int, float)):
            raise ValueError(f"non-numeric literal: {type(node.value).__name__}")
        return node.value
    # 不在白名单 —— 拒绝一切可疑节点
    raise ValueError(f"disallowed node: {type(node).__name__}")


def calculator(expression: str) -> dict[str, Any]:
    """安全地求值一个算术表达式并返回结构化结果。

    失败时返回 ``{"ok": False, "error", "kind"}``;**不抛异常**(模型/LLM
    调用栈需要可控的错误信封,而非异常冒泡)。
    """
    if not isinstance(expression, str):
        return _error("empty", "expression must be a string")
    if not expression or not expression.strip():
        return _error("empty", "expression is empty")
    if len(expression) > _MAX_LEN:
        return _error("too_long", f"expression exceeds {_MAX_LEN} chars")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return _error("syntax", f"invalid syntax: {exc.msg}")
    # 白名单节点检查
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return _error(
                "unsafe_node",
                f"unsupported syntax: {type(node).__name__}",
            )
    try:
        value = _eval_node(tree)
    except ZeroDivisionError:
        return _error("domain", "division by zero")
    except OverflowError:
        return _error("domain", "numeric overflow")
    except ValueError as exc:
        return _error("unsafe_node", str(exc))
    # float 溢出 / 未定义结果以 inf / nan 形式流出而非抛异常 —— 显式拦截,
    # 否则下游会把 inf 误读为"很大的数"、nan 则完全无意义。
    if isinstance(value, float) and not math.isfinite(value):
        return _error("domain", "result is not finite (inf/nan)")
    return {"ok": True, "value": value}
