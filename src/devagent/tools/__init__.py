"""DevAssistantAgent 的三个工具。

聚合导出,便于::

    from devagent.tools import calculator, read_text_file, check_commit_message

    agent = create_agent(model=..., tools=[calculator, read_text_file, check_commit_message])

注意:这里 ``import`` 而**不** ``from . import ...``,因为下文需要把
函数本身喂给 :func:`langchain.agents.create_agent`;函数直接导出即可。
"""

from __future__ import annotations

from .calculator import calculator
from .check_commit_message import check_commit_message
from .read_text_file import read_text_file

__all__ = ["calculator", "check_commit_message", "read_text_file"]
