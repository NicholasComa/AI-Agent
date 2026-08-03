"""DevAssistantAgent 包（Week 3 交付物）。

包名选择 ``devagent`` 而非 ``agents`` 的原因：避免与 OpenAI Agents SDK
（``pip install openai-agents``）的顶层包 ``agents`` 同名冲突/遮蔽。
LangChain 本身安全（顶层包是 ``langchain``，子包 ``langchain.agents``）。

Week 3 产物全部落点：

* ``devassistant_agent.py`` — :func:`create_agent` 组装
* ``middleware.py``           — TraceMiddleware / SafeToolMiddleware
* ``tools/``                  — calculator / read_text_file / check_commit_message
"""
