"""RequirementAnalysisWorkflow 的运行配置。

配置项支持通过 ``JWIPC_GRAPH_*`` 环境变量覆盖，与 W07 的
:class:`src.jwipc_dev_mcp_server.config.McpServerConfig` 风格保持一致：
环境变量名 = 前缀 + 字段大写。解析失败（如非数字）时静默回退到默认值，
不抛异常，保证配置缺失也能启动。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_PREFIX = "JWIPC_GRAPH_"
"""环境变量前缀；例如歧义阈值读 ``JWIPC_GRAPH_AMBIGUITY_THRESHOLD``。"""


@dataclass(frozen=True)
class WorkflowConfig:
    """工作流的可调参数，全部有合理默认值。

    Attributes:
        ambiguity_threshold: 置信度低于该值（或产出澄清问题）即判定歧义，
            进入 clarify 节点人工确认。
        max_clarify_rounds: 最多允许的澄清轮次，超出后强制继续。
        max_retries: 单个 LLM 节点失败后最大重试次数（周三挂 RetryPolicy 用）。
        rag_top_k: 检索召回条数。
        rag_min_score: 检索分数下限，低于该值的片段不进入上下文。
    """

    ambiguity_threshold: float = 0.5
    max_clarify_rounds: int = 2
    max_retries: int = 3
    rag_top_k: int = 3
    rag_min_score: float = 0.0

    @classmethod
    def from_env(cls) -> WorkflowConfig:
        """按 ``JWIPC_GRAPH_*`` 环境变量覆盖字段，解析失败回退默认值。"""
        env_map = {
            "AMBIGUITY_THRESHOLD": (float, "ambiguity_threshold"),
            "MAX_CLARIFY_ROUNDS": (int, "max_clarify_rounds"),
            "MAX_RETRIES": (int, "max_retries"),
            "RAG_TOP_K": (int, "rag_top_k"),
            "RAG_MIN_SCORE": (float, "rag_min_score"),
        }
        overrides: dict[str, float | int] = {}
        for env_key, (cast, field) in env_map.items():
            raw = os.getenv(ENV_PREFIX + env_key)
            if raw is None:
                continue
            try:
                overrides[field] = cast(raw)
            except ValueError:
                # 非法值静默忽略，沿用默认，避免配置错误中断启动。
                continue
        return cls(**overrides)  # type: ignore[arg-type]
