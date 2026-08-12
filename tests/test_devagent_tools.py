"""Day 12 — 三个工具的单元测试。

覆盖目标(每工具 ≥ 3 条,见 Day 11 验收):

* calculator:正常求值 / 边界 / 非法输入 / 拒绝危险节点
* read_text_file:正常读取 / 路径穿越防护 / 大小上限 / 越界拒绝
* check_commit_message:合法 / 非法 type / 长度超限 / body 超限

另含 Day 11 终止条件 B(模型 tool_call → 工具结果 → final answer)。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让 ``from devagent.tools import ...`` 在 pytest 下也工作
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import sys  # noqa: E402

from devagent.tools import (  # noqa: E402 —— sys.path 调整后 import
    calculator,
    check_commit_message,
    read_text_file,
)
from devagent.tools import read_text_file as rtf_fn  # noqa: E402 —— 函数本身

# 通过 sys.modules 直接拿模块对象（避免 ``import X.Y.Z as W`` 被 ``from X.Y import Z`` 遮蔽）
rtf_mod = sys.modules[rtf_fn.__module__]

# ===========================================================================
# calculator
# ===========================================================================


class TestCalculator:
    def test_basic_addition(self) -> None:
        r = calculator("2 + 3")
        assert r == {"ok": True, "value": 5}

    def test_operator_precedence(self) -> None:
        r = calculator("2 + 3 * 4")
        assert r == {"ok": True, "value": 14}

    def test_parentheses(self) -> None:
        r = calculator("(2 + 3) * 4")
        assert r == {"ok": True, "value": 20}

    def test_unary_minus(self) -> None:
        r = calculator("-5 + 3")
        assert r == {"ok": True, "value": -2}

    def test_division_with_float(self) -> None:
        r = calculator("10 / 4")
        assert r == {"ok": True, "value": 2.5}

    def test_modulo(self) -> None:
        r = calculator("10 % 3")
        assert r == {"ok": True, "value": 1}

    def test_empty_input(self) -> None:
        r = calculator("")
        assert r["ok"] is False
        assert r["kind"] == "empty"

    def test_invalid_syntax(self) -> None:
        r = calculator("2 +")
        assert r["ok"] is False
        assert r["kind"] == "syntax"

    def test_function_call_rejected(self) -> None:
        r = calculator("__import__('os').system('ls')")
        assert r["ok"] is False
        assert r["kind"] == "unsafe_node"

    def test_attribute_access_rejected(self) -> None:
        r = calculator("().__class__")
        assert r["ok"] is False
        assert r["kind"] == "unsafe_node"

    def test_name_reference_rejected(self) -> None:
        # ``x`` 不是常量,也不是白名单节点 —— 拒绝
        r = calculator("x + 1")
        assert r["ok"] is False
        assert r["kind"] == "unsafe_node"

    def test_string_literal_rejected(self) -> None:
        r = calculator("'hello'")
        assert r["ok"] is False
        assert r["kind"] == "unsafe_node"

    def test_boolean_rejected(self) -> None:
        # ``True`` 在 Python AST 里是 ``Constant(value=True)``,白名单只接受数字
        r = calculator("True + 1")
        assert r["ok"] is False
        assert r["kind"] == "unsafe_node"

    def test_division_by_zero(self) -> None:
        r = calculator("1 / 0")
        assert r["ok"] is False
        assert r["kind"] == "domain"

    def test_too_long(self) -> None:
        r = calculator("1+" * 300)
        assert r["ok"] is False
        assert r["kind"] == "too_long"

    def test_non_string(self) -> None:
        r = calculator(123)  # type: ignore[arg-type]
        assert r["ok"] is False
        assert r["kind"] == "empty"


# ===========================================================================
# read_text_file
# ===========================================================================


@pytest.fixture
def isolated_train_dir(tmp_path, monkeypatch):
    """隔离的 TRAIN_DIR —— 每个用例一个空目录。"""
    train = tmp_path / "training_data"
    train.mkdir()
    monkeypatch.setattr(rtf_mod, "TRAIN_DIR", train)
    return train


class TestReadTextFile:
    def test_read_existing_file(self, isolated_train_dir: Path) -> None:
        p = isolated_train_dir / "a.txt"
        p.write_text("hello world", encoding="utf-8")
        r = read_text_file("a.txt")
        assert r["ok"] is True
        assert r["content"] == "hello world"
        assert r["bytes"] == len("hello world")

    def test_read_nested_file(self, isolated_train_dir: Path) -> None:
        sub = isolated_train_dir / "sub"
        sub.mkdir()
        (sub / "b.md").write_text("# hi", encoding="utf-8")
        r = read_text_file("sub/b.md")
        assert r["ok"] is True
        assert r["content"] == "# hi"

    def test_path_traversal_blocked(self, isolated_train_dir: Path) -> None:
        # 在训练目录外建一个文件
        outside = isolated_train_dir.parent / "secret.txt"
        outside.write_text("top secret", encoding="utf-8")
        r = read_text_file("../secret.txt")
        assert r["ok"] is False
        assert r["kind"] == "forbidden"

    def test_absolute_path_outside_blocked(self, isolated_train_dir: Path) -> None:
        # 直接给绝对路径指到训练目录外
        outside = isolated_train_dir.parent / "evil.txt"
        outside.write_text("x", encoding="utf-8")
        r = read_text_file(str(outside))
        assert r["ok"] is False
        assert r["kind"] == "forbidden"

    def test_not_found(self, isolated_train_dir: Path) -> None:
        r = read_text_file("missing.txt")
        assert r["ok"] is False
        assert r["kind"] == "not_found"

    def test_is_dir(self, isolated_train_dir: Path) -> None:
        r = read_text_file(".")  # TRAIN_DIR 本身
        assert r["ok"] is False
        assert r["kind"] == "is_dir"

    def test_too_large(self, isolated_train_dir: Path) -> None:
        p = isolated_train_dir / "big.txt"
        p.write_text("a" * 200, encoding="utf-8")
        r = read_text_file("big.txt", max_bytes=100)
        assert r["ok"] is False
        assert r["kind"] == "too_large"

    def test_max_bytes_must_be_positive(self, isolated_train_dir: Path) -> None:
        r = read_text_file("any", max_bytes=0)
        assert r["ok"] is False
        assert r["kind"] == "bad_path"

    def test_nul_in_path_rejected(self, isolated_train_dir: Path) -> None:
        r = read_text_file("ok\x00bad.txt")
        assert r["ok"] is False
        assert r["kind"] == "bad_path"

    def test_non_string_rejected(self, isolated_train_dir: Path) -> None:
        r = read_text_file(None)  # type: ignore[arg-type]
        assert r["ok"] is False
        assert r["kind"] == "bad_path"


# ===========================================================================
# check_commit_message
# ===========================================================================


class TestCheckCommitMessage:
    def test_valid_message(self) -> None:
        msg = "feat: app: add new endpoint"
        r = check_commit_message(msg)
        assert r["ok"] is True
        assert r["valid"] is True
        assert r["errors"] == []
        assert r["parsed"]["type"] == "feat"
        assert r["parsed"]["scope"] == "app"
        assert r["parsed"]["subject"] == "add new endpoint"
        assert r["parsed"]["breaking"] is False

    def test_bang_marker_rejected(self) -> None:
        # 不允许 breaking 标记 !：出现即判非法格式
        msg = "feat(app)!: drop legacy endpoint"
        r = check_commit_message(msg)
        assert r["valid"] is False
        assert any("header" in e for e in r["errors"])

    def test_no_scope_rejected(self) -> None:
        # scope 必填：无 scope 的写法（如 fix: typo）视为非法格式
        r = check_commit_message("fix: typo")
        assert r["valid"] is False
        assert r["parsed"]["scope"] is None
        assert any("scope" in e or "header does not match" in e for e in r["errors"])

    def test_unknown_type_rejected(self) -> None:
        # scope 必填，故未知 type 也需带 scope 才能进入 type 校验
        r = check_commit_message("wat(app): hello")
        assert r["valid"] is False
        assert any("type" in e for e in r["errors"])

    def test_header_too_long(self) -> None:
        long_subject = "x" * 80
        r = check_commit_message(f"feat(app): {long_subject}")
        assert r["valid"] is False
        assert any("max 72" in e for e in r["errors"])

    def test_header_too_long_with_scope(self) -> None:
        # header 全长超 72,但 type/scope/subject 都合规
        msg = "feat: app: " + "y" * 70
        r = check_commit_message(msg)
        assert r["valid"] is False
        assert any("max 72" in e for e in r["errors"])

    def test_body_line_too_long(self) -> None:
        msg = "feat(app): short subject\n\n" + "z" * 120
        r = check_commit_message(msg)
        assert r["valid"] is False
        assert any("body line" in e for e in r["errors"])

    def test_invalid_format(self) -> None:
        r = check_commit_message("not a commit message at all")
        assert r["valid"] is False
        assert r["errors"]  # 至少一条错误

    def test_empty_subject(self) -> None:
        r = check_commit_message("feat(app):   ")
        assert r["valid"] is False

    def test_with_body_ok(self) -> None:
        msg = "feat(app): add X\n\nThis is a body\nwith multiple lines."
        r = check_commit_message(msg)
        assert r["valid"] is True
        assert r["parsed"]["has_body"] is True

    def test_non_string_input(self) -> None:
        r = check_commit_message(123)  # type: ignore[arg-type]
        assert r["valid"] is False
        assert r["ok"] is True  # 工具层 ok,只是 valid=False


# ===========================================================================
# Day 12 终止条件 B —— 调工具 → 工具结果回填 → 模型给 final answer
# ===========================================================================


class TestAgentLoopScenarioB:
    def test_tool_call_then_final_answer(self, isolated_train_dir: Path) -> None:
        """模型先调工具 → 工具执行 → 模型再给 final answer，两步终止。"""
        from langchain.agents import create_agent
        from langchain_core.messages import AIMessage, ToolCall

        from fake_models import FakeToolCapableChatModel

        # 准备训练数据
        (isolated_train_dir / "data.txt").write_text("alpha", encoding="utf-8")

        # 第 1 步:模型调 read_text_file("data.txt")
        # 第 2 步:模型给 final answer
        model = FakeToolCapableChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            name="read_text_file",
                            args={"path": "data.txt"},
                            id="call_1",
                        )
                    ],
                ),
                AIMessage(content="file says: alpha"),
            ]
        )
        agent = create_agent(
            model=model,
            tools=[read_text_file],
            system_prompt="You read files.",
        )
        result = agent.invoke({"messages": [{"role": "user", "content": "what's in data.txt?"}]})
        msgs = result["messages"]
        types = [m.type for m in msgs]
        # human → ai(tool_call) → tool → ai(final)
        assert types == ["human", "ai", "tool", "ai"]
        # 最终回答来自第 2 次模型调用
        assert msgs[-1].content == "file says: alpha"
        assert msgs[-1].tool_calls == []
