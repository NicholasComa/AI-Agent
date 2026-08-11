# Day 16 — Dify 核心概念与 Quickstart 复盘

> Week 4 · Day 16（周一）
> 目标：理解 Dify 平台能力边界，跑通最小工作流，确认 REST API 可用。
> 参考官方文档：` Dify 30 分钟 Quickstart`（30 分钟快速入门）、`Dify Key Concepts`（核心概念）

## 1. 今日完成情况

| 任务 | 状态 | 说明 |
|------|------|------|
| 注册 Dify Cloud 账号 | ✅ | cloud.dify.ai，Sandbox 计划（200 AI 消息额度，一次性） |
| 阅读官方核心概念文档 | ✅ | Workflow / Chatflow / 变量 / 领域特定语言（DSL） |
| 完成 30min 教程工作流 | ✅ | 「多平台内容生成器」已搭建并测试通过 |
| 用 Dify API 调用工作流 | 🔧 | 见第 6 节，脚本 `scripts/call_dify_workflow.py` 已就绪，待填入 workflow API Key 运行 |
| 探索 Chatflow / Agent 模式差异 | ✅ | 见第 4 节对比表 |

---

## 2. Dify 核心概念边界

来源：`Dify Key Concepts`。Dify 提供 **5 种应用类型**，底层都跑同一套工作流引擎。

### 2.1 两种主推类型（Studio 创建时选择）

| 类型 | 触发方式 | 适用场景 | 记忆/会话 |
|------|----------|----------|-----------|
| **Workflow** | 单轮任务，由**用户输入或 API 调用**触发；也可由**触发器**（定时/外部事件）自动启动 | 多任务批处理、一次性生成、可被代码服务调用 | 无（每次运行独立） |
| **Chatflow** | 对话**每一轮**都被触发 | 多轮对话助手 | 有（会话变量 + 对话历史 + 流式输出） |

> 关键认知：**Workflow 是所有其他应用类型的基础**。Chatflow 只是 Workflow 之上加了「对话状态」能力。

### 2.2 三种基础类型（传统简单界面）

- **聊天机器人**：最简对话界面
- **Agent**：以推理/工具调用为核心的自主代理
- **文本生成器**：单轮文本生成

### 2.3 其他核心概念

| 概念 | 作用 | 与代码版映射 |
|------|------|--------------|
| **Knowledge（知识库）** | 文档检索增强（RAG 基础） | 类比 LangChain `VectorStore` + `Retriever` |
| **Tool（工具）** | 工作流/ Agent 可调用的外部能力（HTTP、函数等） | 类比 Week 3 的 `@tool` 函数 |
| **Plugin（插件）** | 打包后的 Tool / 模型供应商扩展 | 类比可安装的 Python 包 |
| **DSL（领域特定语言）** | 所有应用可导出为 YAML，可再导入复现 | 类比 IaC（Terraform）、Docker Compose |
| **变量** | 输入变量 / 节点输出变量 / 环境变量 / 会话变量（仅 Chatflow） | 类比函数参数与返回值 |

**变量体系要点（Gate 2 相关）**：
- **输入变量**：在「用户输入」节点定义，运行开始时设置，**不可更新**。
- **输出变量**：每个节点产生一个或多个输出，可在后续节点引用，**不可更新**。
- **环境变量**：存敏感信息（API Key），与 DSL 分离，分享 DSL 不泄露密钥。
- **会话变量**：仅 Chatflow，跨多轮持续，可用「变量分配器」节点更新。

---

## 3. Dify 节点类型速查表（通用）

> 来源：Dify 官方节点文档索引（`docs.dify.ai/.../nodes/*`）。Dify 画布由「节点」构成，每个节点负责一类确定职责。下表按功能分组，供 Day 17/18 选型，以及 Gate 2「解释每个节点输入输出」使用。

### 3.1 起点 / 输入节点
| 节点 | 作用 | 关键输出 |
|------|------|----------|
| 用户输入 (User Input) | Workflow/Chatflow 起点，定义最终用户输入变量（字段、类型、必填） | 各输入变量（如 `draft`/`platform`） |
| 开始 (Start) | 传统起点节点，定义输入变量；新版推荐用「用户输入」 | 输入变量 |
| 触发器 (Trigger) | 自动启动：定时 / Webhook / 插件事件（Workflow 专属，Chatflow 无） | — |

### 3.2 模型与推理
| 节点 | 作用 | 常见输出 |
|------|------|----------|
| LLM | 调用大模型，支持 system prompt、用户消息、Vision、结构化输出 | `text` / `structured_output` |
| 参数提取器 (Parameter Extractor) | 用 LLM 从自然语言提取结构化参数（如平台名数组） | 提取出的结构化变量 |
| 问题分类器 (Question Classifier) | 用 LLM 对输入分类，路由到不同分支 | 分类标签 |
| Agent（节点） | 在 workflow 内嵌入自主 Agent 子图（感知→推理→工具） | Agent 结果 |

### 3.3 检索 / 数据
| 节点 | 作用 | 常见输出 |
|------|------|----------|
| 知识库检索 (Knowledge Retrieval) | 从知识库召回相关文档片段（RAG 前奏，Week 5/6 深入） | 检索到的文档块 |
| 文档提取器 (Doc Extractor) | 上传文档（PDF/DOCX）转纯文本，供模型处理 | `text` |
| 列表操作器 (List Operator) | 过滤 / 排序 / 切片数组（如按 `type` 分图像/文档） | 过滤后的数组 |

### 3.4 逻辑 / 分支
| 节点 | 作用 | 常见输出 |
|------|------|----------|
| IF/ELSE | 基于条件表达式路由分类（输入校验、提前终止） | IF 分支 / ELSE 分支 |
| 迭代 (Iteration) | 遍历数组，对每个元素跑子流程，可并行（max parallelism） | `output`（数组） |
| 循环 (Loop) | 按条件重复执行子流程 | 循环结果 |
| 变量聚合器 (Variable Aggregator) | 合并多个分支变量为单一变量 | 聚合变量 |
| 变量分配器 (Variable Assigner) | 给会话/环境变量赋值（仅 Chatflow） | — |
| 人工输入 (Human Input) | 暂停等待人工填写表单（human-in-the-loop） | 人工填入值 |

### 3.5 代码 / 集成
| 节点 | 作用 | 常见输出 |
|------|------|----------|
| 代码 (Code) | 沙箱运行 Python/Node.js（如 calculator、正则校验） | 脚本返回值 |
| 模板 (Template) | Jinja2 渲染变量为文本，零 token 稳定格式化 | `output` |
| 工具 (Tool) | 调用已配置工具/插件（HTTP、函数、第三方） | 工具返回 |
| HTTP 请求 (HTTP Request) | 发起任意外部 HTTP 调用 | 响应体 |

### 3.6 终点节点
| 节点 | 作用 | 适用 |
|------|------|------|
| 输出 (Output) | Workflow 终点，把结果返回调用方/API | Workflow |
| 回答 (Answer) | Chatflow 终点，向用户流式返回消息 | Chatflow |

> 与代码版对应：LLM≈`LlmClient`、Code≈`@tool`/纯函数、IF/ELSE≈`if/else` 早返回、迭代≈`asyncio.gather`、模板≈f-string/Jinja、知识库检索≈`Retriever`、工具≈MCP/Week 3 工具。
> 30 分钟教程已实战其中 8 类：用户输入、参数提取器、IF/ELSE、列表操作器、文档提取器、LLM、迭代、模板、输出。

---

## 4. 30 分钟教程工作流复盘（节点级输入输出）

教程工作流「多平台内容生成器」：接受草稿文本 + 参考材料 + 语音语调 + 目标平台 + 语言，生成分平台的社交媒体帖子。

> Gate 2 要求：**能解释每个节点的输入输出**。下表逐节点记录。

| # | 节点 | 类型 | 输入（引用上游变量） | 输出（下游可引用） | 作用 |
|---|------|------|----------------------|---------------------|------|
| 1 | 用户输入 | 用户输入节点 | — | `draft` / `user_file` / `voice_and_tone` / `platform` / `language` | 收集 5 个输入字段，成为全局变量 |
| 2 | 参数提取器 | 参数提取器节点 | `User Input/platform` | `platform`（Array[String]） | 把自由文本平台名标准化为数组，无效时输出固定错误消息 |
| 3 | IF/ELSE | 分支节点 | `Parameter Extractor/platform` | IF 分支 / ELSE 分支 | 检测错误消息，无效则走 IF→输出节点提前结束 |
| 4 | 图像（列表操作器） | 列表操作器节点 | `User Input/user_file` | `Image/result`（图片子集） | 按 `type ∈ Image` 过滤上传文件 |
| 5 | 文档（列表操作器） | 列表操作器节点 | `User Input/user_file` | `Document/result`（文档子集） | 按 `type ∈ Doc` 过滤上传文件 |
| 6 | 文档提取器 | 文档提取器节点 | `Document/result` | `Doc Extractor/text` | 把文档转纯文本（模型不能直接读 PDF/DOCX） |
| 7 | 整合信息 | LLM 节点 | `Doc Extractor/text` + `User Input/draft` + `Image/result`（视觉） | `Integrate Info/text` | 汇总草稿+文档+图像为综合上下文 |
| 8 | 迭代 | 迭代节点 | `Parameter Extractor/platform` | `Iteration/output`（数组） | 遍历平台列表，每个平台跑子工作流 |
| 8a | ├─ 识别风格 | LLM 节点（迭代内） | `Current Iteration/item` | `Identify Style/text` | 分析平台风格指南 |
| 8b | └─ 创建内容 | LLM 节点（迭代内） | `item` + `language` + `Identify Style/text` + `Integrate Info/text` + `voice_and_tone` | `Create Content/structured_output`（`{platform_name, post_content}`） | 生成平台定制帖子（结构化输出） |
| 9 | 模板 | 模板节点 | `Iteration/output` | `Template/output` | 用 Jinja2 把数组渲染成可读 Markdown |
| 10 | 输出 | 输出节点 | `Template/output` | —（返回给用户/API） | 返回最终结果 |

**关键设计点**：
- **参数提取器**把不可控的自然语言输入变成结构化数组 —— 这正是 Week 1 `RequirementAnalysis` 结构化输出的低代码版本。
- **IF/ELSE 提前终止**：无效平台直接路由到输出节点，避免浪费 token —— 等价于代码里的「输入校验 + 早返回」。
- **迭代节点并行模式**（max parallelism=10）：一个数组展开成多个并发子流程 —— 等价于代码里的 `asyncio.gather`。
- **模板节点做格式化而非 LLM**：规则化格式化零 token 成本、输出稳定 —— 对应「知道何时不用 LLM」的工程判断。
- **结构化输出**（从 JSON 导入 schema）：保证 `post_content` 可被下一步稳定引用 —— 等价于 Pydantic schema。

> 这 10 个节点正好覆盖了路线图要求的节点类型：**输入变量 / 参数提取 / IF-ELSE / LLM / 模板 / 输出**，外加教程特有的列表操作器、文档提取器、迭代。

---

## 5. 三种应用模式对比（Workflow / Chatflow / Agent）

| 维度 | Workflow | Chatflow | Agent |
|------|----------|----------|-------|
| 触发 | 单轮 / API / 触发器 | 每轮对话 | 自主循环（感知→推理→行动） |
| 状态 | 无（每次独立） | 会话变量 + 历史 | 内部推理轨迹 + 工具调用历史 |
| 适用 | 批处理、生成、被代码调用 | 对话助手 | 需要自主决策/多步工具调用的场景 |
| 与代码映射 | 函数 pipeline | 带 session 的对话 handler | `create_agent` 编译的 `CompiledStateGraph` |

**编排 与 推理**：
- **Workflow/Chatflow** = 编排（orchestration）：节点和流向是**显式固定**的，开发者完全掌控。
- **Agent** = 推理（reasoning）：下一步行动由**模型自主决定**，更灵活但更难预测/调试。

> Week 4 的重点是**用 Workflow 理解编排思想**

---

## 6. Dify REST API 调用（验证 REST API 可用）

### 6.1 端点与认证

```
POST {DIFY_BASE_URL}/workflows/run
Authorization: Bearer {WORKFLOW_API_KEY}
Content-Type: application/json
```

- `DIFY_BASE_URL`：Dify Cloud 默认 `https://api.dify.ai/v1`
- `WORKFLOW_API_KEY`：**工作流级密钥**（非账号级）。获取路径：Studio 打开应用 → **发布** → **API 访问** → 复制密钥。

### 6.2 请求体

```json
{
  "inputs": {
    "draft": "我们刚发布了一款 AI 写作助手，帮团队把内容创作速度提升 10 倍。",
    "platform": "Twitter and LinkedIn",
    "language": "简体中文",
    "voice_and_tone": "友好且热情，但保持专业",
    "user_file": []
  },
  "response_mode": "blocking",
  "user": "day16-verify"
}
```

- `response_mode`：`blocking`（同步等待结果）或 `streaming`（SSE 流式）。
- `user`：终端用户标识，用于追踪/隔离。
- `inputs` 的字段名必须与工作流「用户输入」节点定义的变量名一致。

### 6.3 响应结构（blocking）

```json
{
  "workflow_run_id": "run-xxxx",
  "task_id": "task-xxxx",
  "data": {
    "id": "xxxx",
    "workflow_id": "wf-xxxx",
    "status": "succeeded",
    "outputs": { "output": "# 📱 Twitter\n..." },
    "error": null,
    "elapsed_time": 1.23,
    "total_tokens": 1234
  }
}
```

- 成功：`status == "succeeded"`，结果在 `data.outputs` 中。
- 失败：`status == "failed"`，`data.error` 含错误描述（需映射为 `DifyWorkflowError`）。

### 6.4 快速验证（curl）

```bash
# Git Bash / bash
curl -s -X POST 'https://api.dify.ai/v1/workflows/run' \
  -H 'Authorization: Bearer '"$DIFY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "inputs": {
      "draft": "We launched an AI writing assistant.",
      "platform": "Twitter and LinkedIn",
      "language": "English",
      "voice_and_tone": "Friendly and professional",
      "user_file": []
    },
    "response_mode": "blocking",
    "user": "day16-verify"
  }' | python -m json.tool
```

> 在 `.env` 中设置 `DIFY_API_KEY=（工作流密钥）` 后，bash 用 `export $(grep -v '^#' .env | xargs)` 注入，再运行上述命令。

### 6.5 Python 验证脚本

已创建 `scripts/call_dify_workflow.py`，运行：

```bash
uv run python scripts/call_dify_workflow.py
```

脚本读取 `.env` 的 `DIFY_API_KEY` / `DIFY_BASE_URL`，调用教程工作流并打印 `workflow_run_id` 与最终输出。

---

## 7. 关键认知（Dify 工作流 vs 代码实现）

1. **Dify 工作流适合快速验证场景**：30 分钟搭出一个多平台内容生成器，无需写一行后端。
2. **核心能力仍需理解代码与接口**：节点背后是变量、schema、分支逻辑、API —— 这些和代码版一一对应。
3. **不把整个逻辑藏在一个超长 Prompt**：教程把「整合信息 / 识别风格 / 创建内容」拆成独立 LLM 节点，每个有清晰输入输出 —— 这正是 Gate 2 的核心要求。
4. **知道何时不用 LLM**：模板节点做格式化（零 token、稳定），对应代码里「规则能解决就别调模型」。
5. **API 是 Dify 工作流与代码世界的桥**：Dify 工作流发布后通过 REST API 被 FastAPI 包装（Day 19），Dify 工作流产物直接进入代码服务体系。

---

## 8. 下一步（Day 17）

用 Dify 可视化界面复现 Week 3 的 **DevAssistantAgent** 简化场景（3 个工具）：
- `calculator` → Code 节点（Python）
- `read_text_file` → 知识库节点（替代，Dify Cloud 不能读本地文件）
- `check_commit_message` → Code 节点（Python）

并导出 DSL 到 `dify_workflows/dev_assistant_agent.yml`，记录映射与局限到 `docs/day17_dify_mapping.md`。

---

## 参考

- 官方文档（本地副本）：`C:\Users\Xsz\Desktop\文本\6week\30min.md`
- 官方文档（本地副本）：`C:\Users\Xsz\Desktop\文本\6week\核心概念.md`
- Dify DSL 文档索引：https://docs.dify.ai/llms.txt
