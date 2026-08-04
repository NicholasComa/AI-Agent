"""Day 13 —— DevAssistantAgent（研发助手代理）工厂（factory，把零散配置打包成一个可调用对象的构造器）。

为什么需要「函数：build_devassistant_agent（构造助手代理）」工厂函数
-------------------------------

LangChain v1 的「函数：create_agent（创建代理）」直接返回一个「类型：CompiledStateGraph（编译状态图，LangChain 把 Agent 编译成可执行的状态机图）」；
测试与生产环境都需要“组装好的 agent 代理 + 一致的 system_prompt 系统提示词（给模型的角色与约束说明）+ 注入 AppConfig 应用配置 + 挂上 Trace 追踪 / Safe 安全中间件”。
把这些散落的配置塞进单一函数，避免每个调用点都重复 boilerplate（样板代码，重复的初始化逻辑），也避免生产代码漏挂某个 middleware 中间件。

责任范围
--------

* 把 3 个「装饰器：@tool（工具装饰器，把普通函数注册成 Agent 可调用工具）」函数（``calculator`` 计算器 / ``read_text_file`` 读文本文件 / ``check_commit_message`` 校验提交信息）喂给「函数：create_agent（创建代理）」；
* 挂「类：TraceMiddleware（追踪中间件，可选）」与「类：SafeToolMiddleware（安全工具中间件，默认开启）」；
* 把「类：AppConfig（应用配置，保存模型名 / 目录等运行参数的配置对象）」中的 ``train_dir`` 训练数据目录 / ``max_file_bytes`` 单文件最大字节数 注入 ``devagent.tools.read_text_file``（「方法：configure（配置函数），设置模块级全局参数的函数」）；
* 暴露「常量：DEFAULT_RECURSION_LIMIT（默认递归上限）」与 ``system_prompt`` 系统提示词 供测试覆盖。

注意
----

``recursion_limit``（递归上限，限制 Agent 循环最大步数）不在「函数：create_agent（创建代理）」里设，而是作为
``agent.invoke(input, config={"recursion_limit": N})``（运行时参数，即调用时才传入的配置）传入
（这是 LangGraph（LangChain 的状态图编排库）的设计）。工厂函数返回的 agent 本身没有这个上限 —— 调用方必须显式传，这是有意的“显式优于隐式（Explicit is better than implicit，Python 设计哲学之一）”。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel

from config import AppConfig

from .middleware import SafeToolMiddleware, TraceMiddleware, TraceRecorder
from .tools import calculator, check_commit_message, read_text_file

DEFAULT_RECURSION_LIMIT = (
    8  # 默认递归上限：限制 Agent 循环（模型↔工具交互）的最大步数，超过即强制终止
)

SYSTEM_PROMPT = """你是 DevAssistantAgent（研发助手代理），一个面向研发场景的本地助手。

你可以使用以下三个工具（Tool，Agent 可调用的一段功能）:
1. calculator(expression)         —— 安全算术求值（白名单 + - * / %）
2. read_text_file(path)           —— 读取训练数据目录下的文本文件（只读 + 沙箱 sandbox，即受限的运行环境）
3. check_commit_message(message)  —— 校验 commit 消息（代码提交说明）是否符合团队规范

约束:
- 算术 / 文件内容 / commit 校验类问题，必须调用对应工具，**不要** 凭记忆回答。
- 工具返回的结构化错误（如 {"ok": False, "kind": "forbidden"} 信封 envelope，即统一格式的结果包裹）也是信息，
  据此调整路径或重试即可，不要编造结果。
- 你无法访问训练目录之外的文件、无法联网、无法执行 shell（命令行）。
- 回答简洁，直接给结论 + 必要的数据；不需要复述工具调用过程。
"""


def _inject_sandbox(config: AppConfig | None) -> None:
    """把 AppConfig 应用配置的 train_dir 训练数据目录 / max_file_bytes 单文件最大字节数 注入 read_text_file 模块。

    注意「方法：devagent.tools.read_text_file.configure（配置函数）」接受的是模块级全局（module-level globals，即文件顶层的全局变量，
    如 ``TRAIN_DIR`` / ``MAX_FILE_BYTES``），这是单进程（process，一个运行中的程序实例）内运行时配置的简易做法 ——
    真正的多进程 / 多线程（thread，轻量级并发执行单元）隔离不在本路线要求内。
    """
    if config is None:
        return
    rtf = sys.modules[read_text_file.__module__]
    rtf.configure(
        train_dir=Path(config.train_dir),
        max_file_bytes=config.max_file_bytes,
    )


def build_devassistant_agent(
    model: BaseChatModel,
    *,
    config: AppConfig | None = None,
    recorder: TraceRecorder | None = None,
    with_safe_tool: bool = True,
) -> Any:
    """构造一个配好 middleware 中间件 + tools 工具 + system_prompt 系统提示词 的 DevAssistantAgent（研发助手代理）。

    Args:
        model: LangChain「类型：BaseChatModel（聊天模型基类，所有对话模型都要继承它）」（生产 = 「函数：init_chat_model（初始化聊天模型）」；
            测试 = ``FakeToolCapableChatModel``（假模型，测试用、模拟真实模型但不联网的替身））。
        config: 可选「类：AppConfig（应用配置）」；若传入，则把 ``train_dir`` 训练数据目录 与 ``max_file_bytes`` 单文件最大字节数
            注入「方法：read_text_file.configure（配置函数）」。
        recorder: 可选「类：TraceRecorder（追踪记录器）」；若传入，挂上「类：TraceMiddleware（追踪中间件）」（同一 recorder 收集全部事件）。
        with_safe_tool: 是否挂「类：SafeToolMiddleware（安全工具中间件）」。默认 ``True``；
            测试中可临时关闭，验证“无 SafeTool 时异常会冒泡（bubble up，向上传播）”。

    Returns:
        LangGraph「类型：CompiledStateGraph（编译状态图）」，调用方式::

            agent.invoke(
                {"messages": [{"role": "user", "content": "..."}]},
                config={"recursion_limit": 8},
            )
    """
    _inject_sandbox(config)

    middleware: list[Any] = []  # 中间件列表，最终传给 create_agent
    if recorder is not None:
        middleware.append(TraceMiddleware(recorder=recorder))
    if with_safe_tool:
        middleware.append(SafeToolMiddleware())

    return create_agent(
        model=model,
        tools=[calculator, read_text_file, check_commit_message],
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
    )


__all__ = [  # 公开接口
    "build_devassistant_agent",
    "DEFAULT_RECURSION_LIMIT",
    "SYSTEM_PROMPT",
]
