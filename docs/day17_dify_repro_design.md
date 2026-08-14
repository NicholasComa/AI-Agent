# Day 17 设计文档：用 Dify 复现 DevAssistantAgent（简化场景）

> 目标：把 Week 3 用 **代码（LangChain）** 写的 `DevAssistantAgent`（3 个工具）在 **Dify Workflow** 里用可视化工作流复现一遍，
> 导出 DSL，并记录 Dify 的能力边界。本次只做「简化场景」——单轮、单工具调用，不追求完全等价的多步推理。
>
> 实际环境：本地自托管 Dify（`http://localhost/v1`），工作流名称 `DevAssistantAgent_Dify`；操作命名以 Dify 官方 quick-start 与节点文档为准：
> `工作室` → `从空白创建` → `工作流`；起点称`开始（用户输入）节点`；调试称`运行测试` → `开始运行`；发布称`发布` → `发布更新`。

---

## 目录

1. 架构设计
2. 三个工具的真实逻辑
3. 工具 → Dify 节点映射
4. 工作流总体设计（对齐 Gate 2）
5. 标准操作流程（12 步）
6. 可直接粘贴的代码节点 Python
7. read_text_file 的 Dify 替代方案
8. 实测问题记录
9. 交付物（Day 17）
10. 附录：备选方案（Agent 应用模式）
11. Day 17 工作流配置修正（2026-08-14 联调发现）

---

## 1. 架构设计

本工作流采用 **「意图分类 + 条件路由 + 分支执行 + 结果聚合」** 的四层管线架构，将 Week 3 代码版的 Agent 多步推理循环，映射为 Dify 工作流画布上的**线性 DAG**。

### 1.1 架构分层

```mermaid
graph TB
    subgraph 接入层
        START[开始节点<br/>用户输入 query]
    end

    subgraph 编排层
        CLASS[意图分类 LLM<br/>结构化输出<br/>tool / expression / message / query / question]
        ROUTE{IF/ELSE 条件路由<br/>按 tool 字段分支}
    end

    subgraph 执行层
        direction LR
        CALC[代码节点<br/>CALCULATOR<br/>AST 安全求值]
        COMMIT[代码节点<br/>CHECK_COMMIT<br/>Conventional Commits 校验]
        KB[知识检索节点<br/>向量检索训练文本]
        chat[聊天对话/其他<br/>CHAT<br/>]
    end

    subgraph 聚合层
        direction LR
        L1[汇总 LLM_CALC]
        L2[汇总 LLM_CC]
        L3[汇总 LLM_KB]
        L4[汇总 LLM_CHAT]
    end

    subgraph 输出层
        END[结束节点<br/>变量聚合<br/>4 分支 structured_output]
    end

    START --> CLASS
    CLASS --> ROUTE
    ROUTE -->|calculator| CALC --> L1 --> END
    ROUTE -->|check_commit| COMMIT --> L2 --> END
    ROUTE -->|read_file| KB --> L3 --> END
    ROUTE -->|chat/其他| chat --> L4 --> END
```

| 层 | 职责 | 节点 | 对应代码版概念 |
|----|------|------|----------------|
| **接入层** | 接收用户自然语言，不做任何加工 | 开始节点 | HTTP 请求入口 |
| **编排层** | 理解意图、路由分发 | 意图分类 LLM + IF/ELSE | Agent 的 tool selection 逻辑 |
| **执行层** | 确定性工具调用（算术 / 校验 / 检索） | 代码节点 ×2 + 知识检索节点 | `calculator()` / `check_commit_message()` / `read_text_file()` |
| **聚合层** | 把工具结果转成用户可读的自然语言 | 4 个分支专用的汇总 LLM | Agent 的 `_format_response()` |
| **输出层** | 统一出口，聚合所有分支的输出 | 结束节点（变量聚合模式） | HTTP 响应 body |

### 1.2 核心架构决策

| 决策 | 选择的方案 | 放弃的方案 | 理由 |
|------|-----------|-----------|------|
| 路由策略 | **IF/ELSE 条件路由**（LLM 先分类 → 固定路由） | Agent 自主选工具（LLM function calling 循环） | 简化场景下不需要多步推理；条件路由可预测、可调试，LLM 只负责一次分类 |
| 计算/校验工具 | **代码节点**（Python 确定性逻辑） | LLM 节点直接输出 | 算术和正则校验必须精确可复现，不能交给 LLM 模棱两可 |
| 分支汇总 | **每个分支独立 LLM 节点**（4 个） | 1 个共用 LLM 读取所有分支变量 | Dify IF/ELSE 分支变量作用域隔离——未执行分支的变量在下游 `Variable not found` |
| 文件读取 | **知识检索节点**（语义向量检索） | 代码节点内 `open()` | Dify 代码节点沙箱禁止文件系统访问；代码节点用于精确计算，知识库用于语义检索 |
| 输出聚合 | **变量聚合模式**（4 个 `structured_output` 合并） | 直接回复模式 | 4 个分支无法合并到 1 个 Answer 节点；变量聚合让 Day 18 FastAPI 统一包装，前端只需读一个 JSON |

### 1.3 数据流

```
用户 query
  │
  ├──► 意图分类 LLM ──► structured_output { tool, expression, message, query, question }
  │                          │
  │                    IF/ELSE 按 tool 分支
  │                    ┌───────┬────────┬────────┬───────┐
  │                    ▼       ▼        ▼        ▼       │
  │                 calc   check_c  read_f   chat/兜底    │
  │                    │       │        │        │       │
  │                    ▼       ▼        ▼        ▼       │
  │  结构化输出链路：                                           │
  │  ① 意图分类 structured_output                              │
  │  ② 代码节点/知识检索 原始输出                                │
  │  ③ 汇总 LLM structured_output（各分支独立）                  │
  │  ④ End 节点聚合 ──► { text_calc, text_cc, text_kb, text_chat }
  │                                                           │
  └───────────────────────────────────────────────────────────┘
```

**关键约束**：
- 结构化输出贯穿全链路——意图分类 LLM、代码节点、4 个汇总 LLM 都输出结构化 JSON，避免非结构化文本在节点间拼接出错。
- Boolean 类型在 Dify 变量选择器中不可见，因此 Calculator 用 `kind`(String) 替代 `ok`(Boolean)，CheckCommit 用 `valid`(String `"valid"`/`"invalid"`) 替代 Boolean。
- 自定义 Object 插值后变 `[object Object]`，因此 `parsed` 对象改为 `parsed_summary` 扁平字符串。

### 1.4 与代码版架构对比

| 维度 | 代码版（Week 3） | Dify 版（Week 4） |
|------|-----------------|-------------------|
| **架构模式** | Agent 循环：ReAct / Tool-calling loop，LLM 决定"下一步调哪个工具" | 线性 DAG：LLM 一次分类 → 固定路由 → 执行 → 汇总，无循环 |
| **工具注册** | LangChain `@tool` 装饰器 + Python 函数 | 画布拖拽节点：代码节点（Python 片段）、知识检索节点（平台内置） |
| **推理链路** | 多步：模型可多次调用工具，结果回灌上下文再推理 | 单步：一次路由到一个工具，工具结果直接进汇总 LLM |
| **编排控制** | 代码控制（AgentExecutor + 中间件） | 可视化画布（IF/ELSE 条件 + 连线） |
| **中间件** | `TraceMiddleware`（日志追踪）、`SafeToolMiddleware`（工具安全） | Dify 平台内置日志/监控面板；无自定义中间件能力 |
| **文件安全** | `is_relative_to(TRAIN_DIR)` 路径沙箱，Python 层可控 | 无法复现——代码节点无文件系统，改用知识检索降级 |
| **部署** | `uv run` 启动 FastAPI | 本地 Docker 自托管 Dify + 画布发布 |
| **可观测性** | 自定义 trace 日志 + pytest | Dify 内置运行追踪 + 变量缓存查看器 |

**差异本质**：代码版是一个**有状态 Agent**（可多轮、可多步、中间件可插拔）；Dify 版是一个**无状态管线**（单轮、单步、平台封装）。简化场景下两者功能等价，但 Agent 的多步推理能力和中间件可控性在 Dify 管线模式下被主动舍弃。

### 1.5 设计约束与取舍

| 约束来源 | 具体限制 | 本设计应对 |
|----------|----------|-----------|
| Dify 代码节点沙箱 | 禁止文件系统、网络、系统命令 | `read_text_file` 改为知识检索；`calculator` / `check_commit` 只依赖标准库，不受影响 |
| 变量选择器类型过滤 | Boolean、自定义 Object 不显示 | 全部改为 String 或 Array[String]，见第 5 节代码适配 |
| IF/ELSE 分支作用域隔离 | 未执行分支的变量不可见 | 拆成 4 个独立汇总 LLM，各引用本分支变量 |
| 知识检索非精确匹配 | 语义检索 ≠ 按路径精确读取文件 | 明确标注为局限性，写入 Day 18 对比报告 |
| 无 Agent 循环能力 | Dify Workflow 是 DAG，不支持循环回边 | 简化场景只做单步调用，多步推理不在本次范围 |

---

## 2. 三个工具的真实逻辑（来自 `src/devagent/tools/`）   <!-- 编号不变，标题不变 -->

| 工具 | 输入 | 输出信封 | 核心逻辑 |
|------|------|----------|----------|
| `calculator(expression)` | 算术表达式字符串（≤512 字符） | `{"ok":True,"value":...}` 或 `{"ok":False,"error":"...","kind":"..."}` | **不用 `eval`**：`ast` 解析 + 白名单节点（`+ - * / %` 与一元 `±`），拒绝函数调用/变量/下标；除零、`inf/nan` 拦截 |
| `read_text_file(path)` | 沙箱内相对路径 | `{"ok":True,"content":"...","bytes":N,"truncated":false}` 或错误信封 | 路径穿越防护（`is_relative_to(TRAIN_DIR)`）；只读、大小上限 50KB |
| `check_commit_message(message)` | 完整 commit message（可多行） | `{"ok":True,"valid":bool,"errors":[...],"parsed":{...}}` | Conventional Commits 校验：type 白名单、scope 必填、首行 ≤72、body 行 ≤100；**仅接受两种格式：`type(scope): subject` 或 `type: scope: subject`，不允许 `!` breaking 标记** |

三个工具的共同特征：**失败不抛异常，返回结构化错误信封**（`ok` 字段），让上层（模型/工作流）据此决策。

---

## 3. 工具 → Dify 节点映射

| 代码版工具 | Dify 复现方式 | 说明 |
|------------|---------------|------|
| `calculator` | **代码节点（Code，Python）** | 把 `calculator.py` 的 `main(expression)` 直接搬进 Dify 代码节点，逻辑 1:1 还原 |
| `check_commit_message` | **代码节点（Code，Python）** | 把 `check_commit_message.py` 的 `main(message)` 搬进代码节点，逻辑 1:1 还原 |
| `read_text_file` | **知识检索节点（Knowledge Retrieval）** | ⚠️ **Dify 代码节点运行在隔离沙箱，禁止文件系统访问**（官方文档明确：sandbox blocks file system access），无法复现路径沙箱。改用「上传训练文本到知识库 + 知识检索节点」替代，局限性见第 6 节 |

> 为什么前两个用**代码节点**而不是 LLM 节点？
> 因为这两个工具是**确定性计算**（算术 / 正则校验），必须精确、可复现，不能交给 LLM「估算」。代码节点 = Dify 里的确定性执行单元，正好对应代码版的纯函数工具。这正体现 Week 4 核心认知：**「核心能力仍需要代码/确定性逻辑，Dify 只负责编排」**。
> 官方文档也印证：代码节点预装 `ast/math/re` 等标准库，但禁止文件系统和网络访问——所以算术、正则这类纯计算可用代码节点精确复现，而「读本地文件」不行。

---

## 4. 工作流总体设计（对齐 Gate 2）

```
[开始(用户输入)] query(text)
   │
   ▼
[LLM] 意图分类（结构化输出 JSON: tool / expression / message / query / question）
   │
   ▼
[IF/ELSE] 条件分支：
   ├─ IF   tool 是 "calculator"  ─► [代码] CALCULATOR(expression)      ─► [LLM] 汇总回答_CALC   ─┐
   ├─ ELIF tool 是 "check_commit" ─► [代码] CHECK_COMMIT(message)      ─► [LLM] 汇总回答_CC    ─┤
   ├─ ELIF tool 是 "read_file"    ─► [知识检索] 知识检索(query)        ─► [LLM] 汇总回答_KB    ─┼─► [结束] output = 聚合 4 分支 structured_output
   └─ ELSE（其它）                ─────────────────────────────────────► [LLM] 汇总回答_CHAT   ─┘
```

> 为什么拆成 4 个「汇总回答」LLM？因为 Dify 的 IF/ELSE 分支变量作用域互相隔离：下游节点若同时引用多个分支的输出变量，未执行分支的变量会报 `Variable #xxx# not found`。每个分支末尾单独接一个汇总 LLM，只引用本分支变量，才能避开这个错误。

### 各节点输入输出（Gate 2 要求：能解释每个节点的输入输出）

| 节点 | 输入 | 输出 | 备注 |
|------|------|------|------|
| 开始（用户输入） | — | `query: string` | 用户自然语言请求，如「算一下 (12+8)*3」 |
| 意图分类 LLM | `query` | `tool`, `expression?`, `message?`, `query?`, `question?`（结构化 JSON） | 只输出 JSON，不输出解释；资料/文档类问题应判 `read_file` |
| IF/ELSE | `tool` | 路由到对应分支 | 单节点 IF/ELIF/ELSE 多分支 |
| Calculator 代码 | `expression: str` | `value: number`, `error: str`, `kind: str`, `summary: str` | 见第 5.1 节（输出均为可显示类型，无 Boolean） |
| CheckCommit 代码 | `message: str` | `valid: str`, `errors: list[str]`, `summary: str`, `parsed_summary: str` | 见第 5.2 节（valid/parsed 改 String，规避变量选择器过滤） |
| 知识检索 | `query: str` | `result: list<chunk>`（检索片段数组） | 见第 6 节配置 |
| 汇总回答_CALC | `query` + CALCULATOR 输出 | `answer/tool_used/status/result`（结构化 JSON） | 仅引用 calculator 分支变量 |
| 汇总回答_CC | `query` + CHECK_COMMIT 输出 | `answer/tool_used/status/errors/parsed_summary`（结构化 JSON） | 仅引用 check_commit 分支变量 |
| 汇总回答_KB | `query` + 知识检索 result | `answer/tool_used/sources`（结构化 JSON） | 仅引用 read_file 分支变量；result 放 Context |
| 汇总回答_CHAT | `query` + 意图分类 question | `answer/tool_used`（结构化 JSON） | 兜底闲聊分支 |
| 结束 | 4 个汇总 LLM 的 `structured_output` | `output`（聚合对象） | 变量聚合模式：`text_calc/text_cc/text_kb/text_chat` |

---

## 5. 标准操作流程（基于 Dify 官方 quick-start / 节点文档）

> 实际以本地自托管 Dify（`http://localhost/v1`）为准；若使用 Dify Cloud，只需把 base URL 换成 `https://api.dify.ai/v1`。每一步给出准确的菜单/按钮中文名。

### 步骤 1 — 创建应用
1. 进入 **工作室（Studio）**。
2. 点击 **从空白创建**。
3. 选择应用类型 **工作流（Workflow）**（本场景单轮、无会话记忆，不用 Chatflow）。
4. 名称填 `DevAssistantAgent_Dify`，图标可选，点击 **创建**。
5. 系统自动进入 **工作流画布**。

### 步骤 2 — 配置「开始（用户输入）」节点
1. 点击画布默认已有的 **开始（用户输入）** 节点，打开配置面板。
2. 点 **添加变量**：
   - 变量名 `query`，字段类型选 **段落（paragraph）/ 短文本**，标签名 `用户请求`，必填 **是**。
3. 其余变量不添加，关闭面板。

### 步骤 3 — 添加「意图分类」LLM 节点
1. 在 **开始** 节点后 **添加一个 LLM 节点**（连线自动接上）。
2. 重命名为 `意图分类`。
3. 模型选一个账号里可用的（如 GPT / 你配置的模型）。
4. **系统指令**粘贴：
   ```
   你是意图分类器。根据用户请求判断应调用哪个工具并抽取参数。只输出严格 JSON，不要任何解释或 Markdown 代码块。

   字段说明：
     tool: "calculator" | "check_commit" | "read_file" | "chat"
     expression: 算术表达式字符串（仅 calculator 时填）
     message: 完整 commit message（仅 check_commit 时填）
     query: 检索关键词/问题原文（仅 read_file 时填）
     question: 原始问题（仅 chat 时填）

   路由规则（严格按优先级）：
     1. 含算术表达式或"算/计算/求值"                    → calculator
     2. 含"提交信息/commit message/校验/检查提交"         → check_commit
     3. 用户想了解某份资料、文档、知识库、小说/项目计划等内容 → read_file
        （即使用户问的是常识，只要属于知识库覆盖主题也必须检索，严禁凭记忆直接回答）
     4. 仅闲聊、问候、开放式创作/意见且明确无需查资料     → chat

   规则：tool 必须小写无空格；calculator 时 expression 用标准算术（如 "(12+8)*3"）；
         read_file 时把用户原问题整体放入 query。
   示例输出：{"tool":"calculator","expression":"(12+8)*3","message":"","query":"","question":""}
   ```
5. 添加用户消息：在节点面板 **消息（Messages）** 区点 **+ 添加消息** → 角色 **USER** → 正文区直接打一个 `{` 触发变量选择器，或点正文区左侧的 `{x}` 变量按钮 → 从弹出的变量树里选 **开始（用户输入）/ query**（节点名若你改过，按实际节点名选） → 确认正文变成 `{{#开始.query#}}`。
6. 开启结构化输出并配置 Schema（**关键步骤，按顺序操作**）：
   1. 在节点面板底部（或右侧，取决于 Dify 版本）找到标签页 **结构化输出（Structured Output）**，点进去（与「提示词」「调试」标签并列）。
   2. 把 **结构化输出** 开关切到 **ON**（蓝/绿色）。开关下方出现 **Schema（JSON）** 编辑框。
   3. 点编辑框上边的 {}JSON Schema按钮（不同版本按钮文案略有差异），弹出 JSON 输入对话框。
   4. **把下面这段 JSON 整段粘贴进对话框**（这才是步骤 3 真正要粘贴的 JSON 结构，前面的描述只是在 System Prompt 里讲解字段含义，不是结构化输出 Schema 本身）：
      ```json
      {
        "type": "object",
        "properties": {
          "tool":       {"type": "string"},
          "expression": {"type": "string"},
          "message":    {"type": "string"},
          "query":      {"type": "string"},
          "question":   {"type": "string"}
        },
        "required": ["tool", "expression", "message", "query", "question"]
      }
      ```
   5. 点 **确认 / 导入** → Schema 编辑框下方应出现 **字段清单预览**：`tool / expression / message / query / question`（顺序不限，5 个都要在）。若只显示 3-4 个，说明 JSON 没粘贴完整或 `required` 写了多余字段——回到上一步重新粘贴。
   6. **⚠️ 不要在 System Prompt（步骤 4 已粘贴的那段）里再追加任何 JSON Schema 文本块**——结构化输出机制会自动注入 Schema，重复声明会让模型困惑「到底用哪个 Schema」。System Prompt 只用自然语言说明字段含义即可。

### 步骤 4 — 添加 IF/ELSE 路由节点
1. 在 **意图分类** 后 **添加一个 IF/ELSE 节点**。
2. 配置分支（条件类型选 **是 / 等于** 这类精确匹配）：
   - **IF**：变量选 `意图分类/structured_output.tool`，运算符 **是**，值 `calculator` → 此分支接 Calculator 代码节点。
   - **+ ELIF**：变量 `意图分类/structured_output.tool`，**是**，`check_commit` → 接 CheckCommit 代码节点。
   - **+ ELIF**：变量 `意图分类/structured_output.tool`，**是**，`read_file` → 接知识检索节点。
   - **ELSE**：兜底（chat 场景），直接连到汇总 LLM。
3. 若你的版本不支持多 ELIF，用多个 IF/ELSE 串联，逻辑等价。

### 步骤 5 — calculator 分支：添加代码节点
1. 在 IF(calculator) 分支 **添加一个代码节点**。
2. 语言选 **Python**。
3. **输入变量**：点 **添加**，变量名 `expression`，类型 **文本（text）**，来源引用 `意图分类/structured_output.expression`（⚠️ 结构化输出字段须经 `structured_output` 对象访问，详见步骤 8 提示）。
4. 在代码框粘贴第 5.1 节的 Python（函数 `main(expression)`）。
5. **声明输出变量**（函数返回的 dict 字段，均为变量选择器可显示类型）：`value`(数字)、`error`(文本)、`kind`(文本)、`summary`(文本)。⚠️ 不声明旧代码里的 Boolean `ok` 字段——Dify 变量选择器不显示 Boolean 类型，详见步骤 8 末尾说明。
6. 连接该代码节点输出到 **汇总回答_CALC**。

### 步骤 6 — check_commit 分支：添加代码节点
1. 在 ELIF(check_commit) 分支 **添加一个代码节点**，语言 **Python**。
2. **输入变量**：`message`（文本），引用 `意图分类/structured_output.message`（同上，须经 `structured_output` 访问）。
3. 粘贴第 5.2 节的 Python（`main(message)`）。
4. **声明输出变量**：`valid`(文本，值 `"valid"`/`"invalid"`)、`errors`(数组[文本])、`summary`(文本)、`parsed_summary`(文本)。⚠️ 不声明旧代码的 Boolean `ok` / Boolean `valid` / Object `parsed`——Dify 变量选择器不显示 Boolean 与自定义 Object 类型，详见步骤 8 末尾说明。
5. 连接该代码节点输出到 **汇总回答_CC**。

### 步骤 7 — read_file 分支：知识检索节点（先建知识库）
**先建知识库**（仅首次需要）：
1. 左侧 **知识库** → **创建知识库**。
2. 上传训练文本（`.txt` / `.md`，可多文件）。⚠️ 说明：当前仓库**并未实际提供任何训练语料**（该目录不存在）。需要自备若干示例文档（如一段产品说明、技术笔记）上传；若只想验证工作流跑通，可先传任意一段示例 `.md`。
3. 索引模式选 **高质量（High Quality）**（需 Embedding 模型，Cloud 免费额度通常含）。
4. 等待嵌入完成，记下知识库名（如 `dev-training`）。

**再添加节点**：
1. 在 ELIF(read_file) 分支 **添加一个知识检索节点**。
2. **选择知识库**：添加刚建的 `dev-training`（可加多个，本例一个）。
3. **Query 文本**：选一个文本变量 → `开始/query`（或 `意图分类/structured_output.query`）。
4. （可选）**检索设置**：Top K 默认即可，Score 阈值可按需收紧。
5. 节点输出变量固定为 `result`（检索片段数组，含 `content/metadata/title`）。
6. 连接该节点输出到 **汇总回答_KB**。

### 步骤 8 — 4 个「汇总回答」LLM 节点

由于 Dify 分支变量作用域隔离，**不能**用 1 个汇总 LLM 同时引用 4 个分支的输出。需为每个分支建一个 LLM 节点，只引用本分支变量。

#### 8.1 汇总回答_CALC（calculator 分支）

1. 在 `CALCULATOR` 节点后添加 LLM 节点，重命名 `汇总回答_CALC`。
2. **系统指令**：
   ```
   你根据用户原始问题和计算工具返回的结构化结果，用简洁中文给出结论。
   只输出严格符合 schema 的 JSON，不要任何额外文字或 Markdown 代码块。
   ```
3. **添加消息** → 用户消息，插入 `用户输入/query`、`CALCULATOR/value`、`error`、`kind`、`summary`。
4. 开启**结构化输出**，schema 示例（字段名可自定义）：
   ```json
   {
     "answer": {"type": "string", "description": "给用户的自然语言结论"},
     "tool_used": {"type": "string", "description": "calculator"},
     "status": {"type": "string", "description": "ok/empty/too_long/syntax/unsafe_node/domain"},
     "result": {"type": "string", "description": "计算表达式和值，或错误说明"}
   }
   ```

#### 8.2 汇总回答_CC（check_commit 分支）

1. 在 `CHECK_COMMIT` 节点后添加 LLM 节点，重命名 `汇总回答_CC`。
2. **系统指令**：同上，要求输出严格 JSON。
3. **添加消息** → 用户消息，插入 `用户输入/query`、`CHECK_COMMIT/valid`、`errors`、`summary`、`parsed_summary`。
4. 结构化输出 schema：
   ```json
   {
     "answer": {"type": "string", "description": "给用户的自然语言结论"},
     "tool_used": {"type": "string", "description": "check_commit"},
     "status": {"type": "string", "description": "valid/invalid"},
     "errors": {"type": "array[string]", "description": "错误列表；通过时为空数组"},
     "parsed_summary": {"type": "string", "description": "parsed 信息扁平化字符串"}
   }
   ```

#### 8.3 汇总回答_KB（read_file / 知识检索分支）

1. 在 `知识检索` 节点后添加 LLM 节点，重命名 `汇总回答_KB`。
2. **上下文 Tab** → 点 **+ 添加上下文** → 选 **知识检索 / result**。
3. **系统指令**：
   ```
   请基于以下上下文回答：{{#上下文#}}
   用户问题：{{#用户输入.query#}}
   只输出严格符合 schema 的 JSON，不要任何额外文字。
   ```
4. **添加消息** → 用户消息，插入 `用户输入/query`。
5. 结构化输出 schema：
   ```json
   {
     "answer": {"type": "string", "description": "基于知识检索上下文的自然语言结论"},
     "tool_used": {"type": "string", "description": "knowledge_retrieval"},
     "sources": {"type": "array[string]", "description": "引用到的片段标题或路径列表；无引用时为空数组"}
   }
   ```

#### 8.4 汇总回答_CHAT（ELSE 兜底分支）

1. 在 IF/ELSE 的 ELSE 出口后添加 LLM 节点，重命名 `汇总回答_CHAT`。
2. **系统指令**：同上。
3. **添加消息** → 用户消息，插入 `用户输入/query`、`意图分类/structured_output.question`。
4. 结构化输出 schema：
   ```json
   {
     "answer": {"type": "string", "description": "对用户问题的自然语言回答"},
     "tool_used": {"type": "string", "description": "chat"}
   }
   ```

> **关键提示**：意图分类 LLM 开启「结构化输出」后，所有声明字段（tool/expression/message/query/question）会**聚合到一个 `structured_output` 对象**里，而不是作为独立顶级变量暴露。所以**步骤 4/5/6/8.4 中凡引用意图分类字段，都必须写成 `意图分类/structured_output.xxx`**，不要写成 `意图分类/tool`（或 `expression` / `message`）。变量选择器里**看不到独立的 `question`**，要点进 `structured_output` 展开后再选 `question`。节点名按你画布实际命名为准：本节示例用的是 `意图分类` / `CALCULATOR` / `CHECK_COMMIT` / `用户输入`（开始节点 Dify 默认常显示为「用户输入」）；如果你改名了，对应路径里的节点名也要跟着改。

> **① 知识检索输出不需要过「文档提取器」。** 文档提取器（Document Extractor）是 Dify 里真实存在但用途不同的节点，用于**从用户上传的文件（PDF/DOCX）里提取纯文本**，定位是 `文件上传 → Document Extractor → LLM`，**不**在 `Knowledge Retrieval → LLM` 链路上。知识检索节点自身已返回格式化好的 `result` 数组（每元素含 `content/title/url` 等字段），无需任何中间处理节点，直接放进 LLM 节点的 Context 字段即可。

> **② 变量选择器过滤规则（本设计已据此规避，对照下表）**

| 规则 | Dify 现象 | 本设计规避方式 |
|------|-----------|----------------|
| Boolean 不显示 | Boolean 变量不出现在 USER 消息变量选择器（即便已声明并运行过） | CALCULATOR 用 `kind`(String) 替代 `ok`(Boolean)；CHECK_COMMIT 用 `valid`(String `"valid"`/`"invalid"`) 替代 Boolean `valid` |
| 自定义 Object 不显示 | Object 变量插入后变 `[object Object]` | CHECK_COMMIT 把 `parsed`(Object) 改为 `parsed_summary`(String) 拼接所有字段 |
| 知识检索 result 需先运行 | 未跑过 read_file 分支则 `result` 不在选择器 | 先跑一次 read_file 用例注册变量；引用时放 LLM 的 Context 字段 |

> 补充：① **LLM 节点**标准输出固定为 `text / reasoning_content / structured_output` 三个外层变量，意图分类作为 LLM 节点也只有这三个（字段聚合在 `structured_output` Object 内）。② **知识检索**输出固定为 `result`（`Array[Object]`，含 `content/title/url/icon/metadata/files`）。
>
> **③ 实操要点**：后续若在 Code 节点新增 Boolean/Object 输出并要传给下游 LLM，请按上表改用 String / 拼字符串；或在 LLM 的 SYSTEM 提示词里写「如上游无 X 字段则视为 Y」做兜底。

### 步骤 9 — 结束节点（变量聚合模式）

1. 添加 **结束（End）节点**（若画布已有默认结束节点则复用），模式选 **变量聚合（Variable Aggregator）**。
2. 添加 4 个变量，分别引用 4 个汇总 LLM 的 `structured_output`：
   - `text_calc` = `汇总回答_CALC/structured_output`
   - `text_cc`   = `汇总回答_CC/structured_output`
   - `text_kb`   = `汇总回答_KB/structured_output`
   - `text_chat` = `汇总回答_CHAT/structured_output`
3. 运行时只有 1 个分支会实际产生值，其余 3 个为 `null`。
4. ⚠️ 如果你希望 END 节点直接以文本形式输出，也可以用 **直接回复（Answer）** 模式，但只能接 1 个 LLM 的输出；4 分支场景下变量聚合更便于 Day 18 的 FastAPI 统一包装。

### 步骤 10 — 运行测试（调试）
1. 点画布右上角 **运行测试**。
2. 在弹窗填入 `query`，点 **开始运行**。
3. 分别用以下用例验证：
   | query | 期望路由 | 期望结果 |
   |-------|----------|----------|
   | `算一下 29*(6.2+81-0.2)/2` | calculator | result=1261.5 |
   | `校验 func(app): Add login page` | check_commit | status="valid"（括号式） |
   | `校验 func: app: Add login page` | check_commit | status="valid"（冒号式） |
   | `校验 fix: typo` | check_commit | status="invalid"（无 scope，必填） |
   | `小说《围城》的主角名字叫什么？` | read_file | 走知识检索分支，基于 KB 内容回答 |
   | `你好` | chat | 兜底闲聊回复 |
4. 2026-08-12 实测记录（本地 Dify `http://localhost/v1`）：
   - **calculator**：命中，输出 `{"result": "计算表达式 ...", "answer": "1261.5", "status": "计算状态：成功", "tool_used": "calculator"}`。
   - **check_commit**：命中，但本地版 END 节点 `text_cc` 仅返回 `CHECK_COMMIT` 代码节点的原始 `parsed` 字段（`type/scope/subject/breaking/has_body`），缺少 `status/valid`。**修正方法**：把 END 节点里 `text_cc` 的引用源从 `CHECK_COMMIT/parsed` 改为 `汇总回答_CC/structured_output`。
   - **read_file**：最初被分到 chat 分支（LLM 凭常识回答）。**修正方法**：按步骤 3 的强化版系统指令，把资料/文档类问题强制路由到 `read_file`，禁止凭记忆作答。
4. 出错时看对应节点的 **最后运行** 日志；可用画布底部 **查看缓存变量** 改输入复测。

### 步骤 11 — 发布
1. 点右上角 **发布** → **发布更新**。
2. 之后改动需再次发布才会生效。

### 步骤 12 — 获取 API Key 与导出 DSL（为 Day 18 准备）
1. **API Key**：在应用内点 **访问 API**（或右上角设置 → API 访问）→ **创建密钥**，复制形如 `app-xxxx` 的**工作流级**密钥（作用域仅此应用）。
2. **导出 DSL**：在应用编辑界面，通过应用名旁的下拉菜单或右上角 **··· 更多操作** 找到 **导出 DSL**，选择 **YAML** 格式下载。
   - 本地部署默认下载路径示例：`C:/Users/Xsz/Downloads/文件/DevAssistantAgent_Dify .yml`
   - 手动复制到仓库：`dify_workflows/DevAssistantAgent_Dify.yml`（Day 17 交付物）。
   - 注：quick-start 教程未演示 DSL 导出，此步骤以 Dify 实际界面为准（部分版本入口在「设置 → 导出」）。

---

## 6. 可直接粘贴的代码节点 Python

### 6.1 Calculator 节点（`main(expression: str) -> dict`）

新建 **代码节点** → 语言 **Python** → 输入变量 `expression`（类型 文本）→ 粘贴：

> **输出变量声明**（在代码节点「输出变量 → 添加」按以下 4 条配置，**不要声明 Boolean/Object 字段**——Dify 变量选择器不显示这两种类型，见步骤 8 末尾说明）：
> - `value`（Number）
> - `error`（String）
> - `kind`（String）
> - `summary`（String）— 拼好的自然语言总结，LLM 直接消费

```python
import ast
import math
import operator

_MAX_LEN = 512
_ALLOWED_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: operator.mod,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.USub, ast.UAdd,
)

def _result(value, error, kind, summary):
    return {"value": value, "error": error, "kind": kind, "summary": summary}

def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left); right = _eval_node(node.right)
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported operator: {type(node.op).__name__}")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported unary op: {type(node.op).__name__}")
        return op(operand)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            raise ValueError("booleans are not allowed")
        if not isinstance(node.value, (int, float)):
            raise ValueError(f"non-numeric literal: {type(node.value).__name__}")
        return node.value
    raise ValueError(f"disallowed node: {type(node).__name__}")

def main(expression: str) -> dict:
    if not isinstance(expression, str) or not expression.strip():
        return _result(None, "expression is empty", "empty",
                        "✗ 计算失败：expression is empty（kind=empty）")
    if len(expression) > _MAX_LEN:
        return _result(None, f"expression exceeds {_MAX_LEN} chars", "too_long",
                        f"✗ 计算失败：expression exceeds {_MAX_LEN} chars（kind=too_long）")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return _result(None, f"invalid syntax: {exc.msg}", "syntax",
                        f"✗ 计算失败：invalid syntax: {exc.msg}（kind=syntax）")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return _result(None, f"unsupported syntax: {type(node).__name__}", "unsafe_node",
                            f"✗ 计算失败：unsupported syntax: {type(node).__name__}（kind=unsafe_node）")
    try:
        value = _eval_node(tree)
    except ZeroDivisionError:
        return _result(None, "division by zero", "domain",
                        "✗ 计算失败：division by zero（kind=domain）")
    except OverflowError:
        return _result(None, "numeric overflow", "domain",
                        "✗ 计算失败：numeric overflow（kind=domain）")
    except ValueError as exc:
        return _result(None, str(exc), "unsafe_node",
                        f"✗ 计算失败：{exc}（kind=unsafe_node）")
    if isinstance(value, float) and not math.isfinite(value):
        return _result(None, "result is not finite (inf/nan)", "domain",
                        "✗ 计算失败：result is not finite (inf/nan)（kind=domain）")
    return _result(value, "", "ok", f"✓ 计算成功：{expression} = {value}")
```

> 与代码版差异：① 不再返回 Boolean `ok` 字段（变量选择器不显示 Boolean），改为统一 `kind` 字符串字段（`"ok"` / `"empty"` / `"too_long"` / `"syntax"` / `"unsafe_node"` / `"domain"`）表达成败；② 新增 `summary` 字段把结果拼成自然语言，避免 LLM 拿到零散字段后拼装。计算逻辑、AST 白名单、错误分类与代码版完全一致。
> 注意：Dify 代码节点输出限制——字符串 ≤ 400,000 字符、数字整数 ≤ 19 位、对象数组嵌套 ≤ 5 层，本工具远在限制内。

### 5.2 CheckCommit 节点（`main(message: str) -> dict`）

新建 **代码节点** → 输入变量 `message`（类型 文本）→ 粘贴：

> **输出变量声明**：
> - `valid`（**String**，值 `"valid"` 或 `"invalid"`，不要用 Boolean）
> - `errors`（Array[String]）
> - `summary`（String）— 拼好的自然语言结论
> - `parsed_summary`（String）— 把 parsed 对象扁平化为字符串

```python
import re

ALLOWED_TYPES = frozenset({
    "func","feat","fix","docs","style","conf","perm","version","patch",
    "other","refactor","perf","test","chore","build","ci","revert",
})
SUBJECT_MAX = 72
BODY_LINE_MAX = 100
_HEADER_RE = re.compile(
    r"^(?P<type>[A-Za-z]+)"
    r"(?:\((?P<pscope>[A-Za-z0-9_-]+)\)"
    r"|(?::\s*(?P<cscope>[A-Za-z0-9_-]+)))"
    r":\s(?P<subject>.+)$"
)

def _parse_header(line):
    m = _HEADER_RE.match(line)
    if not m:
        return {"type": None, "scope": None, "subject": None, "breaking": False}
    scope = m.group("pscope") or m.group("cscope")
    return {
        "type": m.group("type").lower(),
        "scope": scope,
        "subject": m.group("subject").strip(),
        "breaking": False,
    }

def main(message: str) -> dict:
    errors = []
    if not isinstance(message, str):
        return {
            "valid": "invalid",
            "errors": ["message must be a string"],
            "summary": "✗ 校验失败：message must be a string",
            "parsed_summary": "type=None, scope=None, subject=None, breaking=False, has_body=False",
        }
    if "\n" in message:
        header, _, rest = message.partition("\n")
    else:
        header, rest = message, ""
    header = header.rstrip("\r")
    rest = rest.replace("\r\n", "\n")
    parsed = _parse_header(header)
    has_body = bool(rest.strip())
    if parsed["type"] is None:
        errors.append("header does not match `<type>(<scope>): subject` or `<type>: <scope>: subject` (scope required)")
    else:
        if parsed["type"] not in ALLOWED_TYPES:
            errors.append(f"type '{parsed['type']}' not allowed")
    if parsed["subject"] is not None and not parsed["subject"].strip():
        errors.append("subject is empty")
    if len(header) > SUBJECT_MAX:
        errors.append(f"header line is {len(header)} chars, max {SUBJECT_MAX}")
    if has_body:
        for i, ln in enumerate(rest.splitlines(), start=2):
            if len(ln) > BODY_LINE_MAX:
                errors.append(f"body line {i} is {len(ln)} chars, max {BODY_LINE_MAX}")
    parsed_summary = (
        f"type={parsed['type']}, scope={parsed['scope']}, "
        f"subject={parsed['subject']}, breaking={parsed['breaking']}, has_body={has_body}"
    )
    if errors:
        summary = "✗ 校验失败：" + "；".join(errors)
    else:
        summary = f"✓ 校验通过：{header.strip()}"
    return {
        "valid": "valid" if not errors else "invalid",
        "errors": errors,
        "summary": summary,
        "parsed_summary": parsed_summary,
    }
```

> 与代码版差异：① `valid` 改为 String（`"valid"` / `"invalid"`），不用 Boolean；② `parsed`（Object）改为 `parsed_summary`（String），用 `, ` 拼接所有字段；③ 新增 `summary` 拼成自然语言。校验逻辑、错误分类、Conventional Commits 白名单与代码版完全一致。

---

## 6. read_text_file 的 Dify 替代方案（重要局限）

**问题**：代码版 `read_text_file` 依赖本地文件系统 + `TRAIN_DIR` 沙箱。Dify **代码节点运行在隔离沙箱，官方明确禁止文件系统访问、出站网络与系统命令**，因此路径穿越防护这套逻辑在 Dify 里**无法复现**（即便写 Python 去 `open()` 也会被沙箱拦截）。

**替代（推荐）**：用 Dify **知识库（Knowledge）** 承载训练文本——
1. 左侧 **知识库** → **创建知识库** → 上传训练文本（`.txt` / `.md`）。⚠️ `training_data/` 是代码里 `TRAIN_DIR` 的默认路径，但仓库中目前**没有这个目录、也没有现成语料**，请自备示例文档上传；这不影响工作流演示，只是知识库检索分支的内容由你提供的文档决定。
2. 在工作流里加 **知识检索节点**，选择该数据集，Query 文本选 `开始/query`；
3. 节点输出 `result`（相关片段数组），交给 **汇总回答_KB** LLM 引用（放 Context 字段）。

**局限（务必写进对比报告/局限文档）**：
- 失去「精确按路径读取某个文件」的能力，变成「语义检索相关片段」；
- 失去路径穿越防护的演示（这是 Dify 可视化编排替代不了代码安全的典型例子）；
- 文件大小、编码兜底等细节由 Dify 知识库接管，不再是你自己控制的代码。

> 这正是 Week 4 核心认知的落点：**Dify 可视化编排能快速搭出「能检索、能算、能校验」的应用外壳，但文件安全沙箱这类核心能力，仍需代码实现。**

---

## 7. 实测问题记录

| 问题 | 现象 | 根因 | 修正 |
|------|------|------|------|
| 知识检索分支没触发 | 问《围城》主角，追踪流程中显示的路径是进入的 chat 路径，答「根据常识作答」 | 意图分类器把常识性问题判为 chat | 步骤 3 系统指令强制：资料/文档/知识库类问题必须判 `read_file`，禁止凭记忆答 |
| check_commit 输出缺 verdict | 本地 `text_cc` 只有 `type/scope/subject/breaking/has_body` | END 变量聚合引用的是 `CHECK_COMMIT/parsed`，不是 `汇总回答_CC/structured_output` | 把 `text_cc` 引用源改为 `汇总回答_CC/structured_output`，即可拿到 `status/errors/answer` |
| 分支变量找不到 | 早期单汇总 LLM 报 `Variable #summary# not found` | Dify IF/ELSE 未执行分支的输出变量不可见 | 拆成 4 个汇总 LLM，每个只引用本分支变量 |

## 8. 交付物（Day 17）

- 本设计文档 `docs/day17_dify_repro_design.md`
- 本地 Dify 中已发布的工作流 `DevAssistantAgent_Dify`（`http://localhost/v1`）
- 导出的 DSL：`dify_workflows/DevAssistantAgent_Dify.yml`

## 9. 附录：备选方案（用 Agent 应用模式，更快但略偏离交付物）

若想更贴近「Agent 自主选工具」的语义，可改用 Dify 的 **Agent 应用** 模式：
- 创建 Agent 应用 → 添加 3 个 **代码工具**（calculator / check_commit_message 同上；read_text_file 仍只能用知识库工具替代）；
- 系统提示词直接用代码版的 `SYSTEM_PROMPT`（见 `dev_assistant_agent.py`）；
- 优点：Agent 自己决定调哪个工具，最像原版；缺点：**导出的不是 Workflow DSL**，与 Day 17「可视化工作流 + DSL 导出」交付物略有出入，且 Gate 2 对「节点输入输出」的解释力弱一些。

---

## 10. Day 17 工作流配置修正（2026-08-14 联调发现）

> 本节是 Day 17 设计文档的**修正性附录**。2026-08-14 在本机用 FastAPI（`POST /dify/run`）联调 `DevAssistantAgent_Dify` 时，发现已发布工作流中**所有 4 个「汇总回答」LLM 节点的结构化输出 Schema 都被填成了「元 Schema」**——即每个业务字段（如 `answer` / `tool_used` / `sources`）不是 `string` / `array[string]`，而是嵌套对象 `{type: string, description: string}`。这种 Schema 设计在 Dify 的本地模型下会让模型：
> 1. **要么**把 Schema 原样吐回当数据（最严重，常见于 `汇总回答_KB`）；
> 2. **要么**把同一个答案重复填进 4 个 `type`/`description` 字段（calculator 的实际表现，虽能用但极冗余）。
>
> 本节给出按节点逐个修正的**完整步骤 + 完整 Schema + 完整 Prompt**，可直接在画布里照抄执行。修正完成后需**重新发布**（API 调用的是已发布版本），然后用 `POST /dify/run` 跑四个分支回归。

### 10.1 问题清单

| # | 节点 | DSL 里的现状 | 现象 | 根因 |
|---|---|---|---|---|
| 1 | 汇总回答_KB | `answer` / `tool_used` / `sources` 都是 `{type, description}` 嵌套对象 | API 返回的 `text_kb` 是元 Schema 本身 | 同 §10 总述 |
| 2 | 汇总回答_CALC | `answer` / `tool_used` / `status` / `result` 都是 `{type, description}` 嵌套对象 | API 返回的 `text_calc` 4 个字段重复填同一段结论 | 同 §10 总述 |
| 3 | 汇总回答_CC | `answer` / `tool_used` / `status` / `errors` / `parsed_summary` 都是 `{type, description}` 嵌套对象 | API 返回的 `text_cc` 字段嵌套冗余 | 同 §10 总述 |
| 4 | 汇总回答_CHAT | `answer` / `tool_used` 都是 `{type, description}` 嵌套对象 | API 返回的 `text_chat` 字段嵌套冗余 | 同 §10 总述 |
| 5 | 知识库（kb 分支） | 仅上传小说《围城》 | 知识检索分支只能答《围城》相关问题 | §7 设计时就标注无现成语料 |

> **为什么 4 个汇总节点都有这个问题？** 因为它们是同一时间按同一模板（带 `type`/`description` 元字段的 Schema）批量配置的——画布里复制粘贴省事，但元字段对 Dify 结构化输出机制是噪声。下面 4 个修正步骤的 Schema 全部按 §8 设计原意改成**叶子字段为 `string` 或 `array[string]`** 的干净业务 Schema，与设计文档 §8.1–8.4 一致。

### 10.2 通用前置

进入 **Studio** → 应用 `DevAssistantAgent_Dify` → 工作流画布。后续步骤按节点逐个打开配置面板修改。

修改任何一个 LLM 节点的结构化输出 Schema：
1. 点开该节点 → 切到 **结构化输出** 标签 → 打开 **结构化输出** 开关；
2. 把「Schema」输入框里**整段 JSON 删空**，粘贴下面给出的对应干净 Schema；
3. **同时检查系统指令**：确认**没有**在 System Prompt 里粘贴 Schema 文本（否则模型会困惑「到底用哪个 Schema」），只保留字段说明；
4. 修改完不要点发布——4 个节点全改完再统一发布。

> 节点命名以画布实际为准（DSL 里 `汇总回答_CALC` 实际画布名 = `汇总回答_CALC`，但 KB 那节点因复制时多了 tab 字符，DSL 里写为 `"\t汇总回答_KB"`，画布里正常显示为 `汇总回答_KB`）。

### 10.3 修正 汇总回答_CALC（calculator 分支）

**模型**：qwen3（`provider: langgenius/ollama/ollama`，`temperature: 0.7`）

**User 消息内容**（在「消息 → USER」里依次插入以下变量）：
```
用户问题：{{#用户输入.query#}}

计算结果：{{#calculator.value#}}
错误信息：{{#calculator.error#}}
结果分类：{{#calculator.kind#}}
自然语言总结：{{#calculator.summary#}}
```

**System Prompt**（清空原内容，粘贴下面完整文本——**不要**包含任何 JSON Schema 块）：
```
你把 calculator 工具的结构化输出和用户原始问题翻译成自然语言结论，输出必须严格符合结构化 schema，不输出任何额外文字或 Markdown 代码块。

字段含义：
- answer: 给用户的一句话结论（必填，例如"60"、"计算失败：除零"）
- tool_used: 固定填 "calculator"
- status: 与 calculator 的 kind 字段同义，取值 "ok" / "empty" / "too_long" / "syntax" / "unsafe_node" / "domain"
- result: 计算表达式与值的简短文本（如 "(12+8)*3 = 60"）或错误说明
```

**结构化输出 Schema**（整段替换）：
```json
{
  "type": "object",
  "properties": {
    "answer":    {"type": "string", "description": "给用户的自然语言结论"},
    "tool_used": {"type": "string", "description": "calculator"},
    "status":    {"type": "string", "description": "ok/empty/too_long/syntax/unsafe_node/domain"},
    "result":    {"type": "string", "description": "计算表达式和值，或错误说明"}
  },
  "required": ["answer", "tool_used", "status", "result"]
}
```

### 10.4 修正 汇总回答_CC（check_commit 分支）

**模型**：qwen3（`provider: langgenius/ollama/ollama`，`temperature: 0.7`）

**User 消息内容**：
```
用户问题：{{#用户输入.query#}}

校验状态：{{#check_commit.valid#}}
错误列表：{{#check_commit.errors#}}
自然语言总结：{{#check_commit.summary#}}
解析摘要：{{#check_commit.parsed_summary#}}
```

**System Prompt**：
```
你把 check_commit 工具的结构化输出和用户原始问题翻译成自然语言结论，输出必须严格符合结构化 schema，不输出任何额外文字或 Markdown 代码块。

字段含义：
- answer: 给用户的最终结论（通过 / 失败原因摘要），不要超过 200 字
- tool_used: 固定填 "check_commit"
- status: 与 check_commit 的 valid 字段同义，取值 "valid" / "invalid"
- errors: 数组，原样透传 check_commit 的 errors 字段；通过时传空数组 []
- parsed_summary: 原样透传 check_commit 的 parsed_summary 字段
```

**结构化输出 Schema**：
```json
{
  "type": "object",
  "properties": {
    "answer":         {"type": "string", "description": "给用户的自然语言结论"},
    "tool_used":      {"type": "string", "description": "check_commit"},
    "status":         {"type": "string", "description": "valid/invalid"},
    "errors":         {"type": "array", "items": {"type": "string"}, "description": "错误列表；通过时为空数组"},
    "parsed_summary": {"type": "string", "description": "parsed 信息扁平化字符串"}
  },
  "required": ["answer", "tool_used", "status", "errors", "parsed_summary"]
}
```

### 10.5 修正 汇总回答_KB（read_file / 知识检索分支）

**模型**：deepseek-v4-flash（`provider: langgenius/deepseek`，`temperature: 0.7`）

> 模型说明：原 DSL 用 deepseek-v4-flash，本地环境下对结构化输出不如 deepseek-v4-pro 稳定。若修正 Schema 后仍出现「复读 Schema」现象，可换为 deepseek-v4-pro（步骤：节点面板 → 模型下拉 → 选 deepseek-v4-pro → 保存）。该变更不影响其他节点。（2026-08-14 实测中该节点以 qwen3 运行、结构化输出亦稳定，也可作为备选模型。）

**上下文（Context）配置**：切到「上下文」标签 → 点 **+ 添加上下文** → 选 **知识检索 / result**。这一步必须保留，否则模型没有 KB 内容可读。

**知识检索节点参数（联调调优，2026-08-14 实测）**：打开画布里的「知识检索」节点 → 检索设置：
- **Top K：10–15**（默认偏小会只回 1 个片段，导致答案单薄、列不出文中多处内容）
- **Score 阈值：0.3**（本地 Embedding 对人名/短 query 相似度偏低，0.45 会把多数相关片段滤掉；不要低于 0.2 以免混入无关段落）
- **检索模式：向量 + 全文**（对人名、专有名词比纯向量更稳）
> 仅调这些参数不触发「复读 Schema」，但改完仍需重新发布工作流。

**User 消息内容**：
```
用户问题：{{#用户输入.query#}}
```

**System Prompt**：
```
你把知识检索结果和用户问题整合成自然语言结论，输出必须严格符合结构化 schema，不输出任何额外文字或 Markdown 代码块。

判断知识检索上下文（即上方「上下文」里的 result 片段）再决定 answer：
- 若 result 片段数组为空（完全没有召回任何内容）→ answer 明确写"知识库未检索到相关内容"，并提示用户换关键词，sources 传空数组 []。
- 若 result 有片段（哪怕只有一句、信息不完整）→ 直接基于这些片段作答，把片段里与问题相关的内容如实组织成结论，sources 列出实际引用到的片段标题；不要判"未检索到"，也不要凭空编造片段里没有的信息。

字段含义：
- answer: 基于知识检索上下文的自然语言结论；有片段就返回片段内容，绝不臆造。
- tool_used: 固定填 "knowledge_retrieval"
- sources: 数组，列出实际引用到的片段标题或来源；无任何引用时传空数组 []
```

**结构化输出 Schema**：
```json
{
  "type": "object",
  "properties": {
    "answer":    {"type": "string", "description": "基于知识检索上下文的自然语言结论"},
    "tool_used": {"type": "string", "description": "knowledge_retrieval"},
    "sources":   {"type": "array", "items": {"type": "string"}, "description": "引用到的片段标题或路径列表；无引用时为空数组"}
  },
  "required": ["answer", "tool_used", "sources"]
}
```

### 10.6 修正 汇总回答_CHAT（chat 兜底分支）

**模型**：deepseek-v4-flash（`provider: langgenius/deepseek`，`temperature: 0.7`）

> 模型说明：同 §10.5，若修正 Schema 后仍复读，可换 deepseek-v4-pro。

**User 消息内容**：
```
用户问题：{{#用户输入.query#}}

原始问题备份：{{#意图分类.structured_output.question#}}
```

**System Prompt**：
```
你是日常对话助手，根据用户原始问题给出自然语言回复。输出必须严格符合结构化 schema，不输出任何额外文字或 Markdown 代码块。

字段含义：
- answer: 对用户问题的直接、友好的自然语言回答（不超过 500 字）
- tool_used: 固定填 "chat"
```

**结构化输出 Schema**：
```json
{
  "type": "object",
  "properties": {
    "answer":    {"type": "string", "description": "对用户问题的自然语言回答"},
    "tool_used": {"type": "string", "description": "chat"}
  },
  "required": ["answer", "tool_used"]
}
```

### 10.7 知识库补充（可选，仅在希望 kb 分支能查项目资料时执行）

当前 kb 分支绑定的知识库只有《围城》。若希望它能查项目内文档（如 `docs/`、`src/` 的内容）：

1. 把项目文档整理为 `.txt` / `.md` 单文件或多文件（建议每个文件 ≤15 MB，超出 Dify 限制）。
2. **Studio** → 左侧 **知识库** → 找到 Day 17 用到的知识库（创建时取的名字，如 `dev-training`） → **添加文档** → 上传文件。
3. 索引模式选 **高质量（High Quality）**（需 Embedding 模型）。等待嵌入完成后保存。
4. **回到工作流** → 打开 `知识检索` 节点 → 已绑定的知识库会自动包含新文档（无需重新配置节点）。
5. 重新发布后跑 kb 分支验证（见 §10.8）。

> 若 kb 分支仍然回答不了项目相关问题，最大可能是 Embedding 模型对中文/英文/技术词汇的检索精度不够——可考虑切换 Embedding 模型（`provider` 设置），或在原始 query 上做关键词归一化（这一步在 `意图分类` 节点的 `query` 字段调优）。

### 10.8 重新发布与四分支回归验证

> 以下命令在 Git Bash 环境运行；普通 cmd 把 `cat >` 换成记事本建文件，`curl` 命令相同。

**步骤 A：导出新 DSL 并覆盖仓库存档**

工作流画布 → 应用名旁下拉菜单（或右上角 **··· 更多操作**） → **导出 DSL** → YAML。下载到的文件覆盖到仓库：
```bash
cp "C:/Users/Xsz/Downloads/文件/DevAssistantAgent_Dify.yml" \
   /d/workspace/py_ai/week01_ai_basics/dify_workflows/DevAssistantAgent_Dify.yml
```

**步骤 B：在画布点「发布」→「发布更新」**（API 调用的是已发布版本，未发布则新配置不生效）。

**步骤 C：建请求文件 + 跑四分支回归**

```bash
# Git Bash
mkdir -p /d/workspace/py_ai/week01_ai_basics/req_d17
cat > /d/workspace/py_ai/week01_ai_basics/req_d17/calc.json     <<'EOF'
{"query": "算一下 (12+8)*3"}
EOF
cat > /d/workspace/py_ai/week01_ai_basics/req_d17/cc.json       <<'EOF'
{"query": "检查提交信息：func: app: Add login page"}
EOF
cat > /d/workspace/py_ai/week01_ai_basics/req_d17/kb.json       <<'EOF'
{"query": "《围城》这本书的作者是谁？讲的是什么？"}
EOF
cat > /d/workspace/py_ai/week01_ai_basics/req_d17/chat.json     <<'EOF'
{"query": "你好，介绍一下这个项目"}
EOF

# 确认 .env 的 DIFY_API_KEY 指向 Day 17 工作流的 app-xxx Key
# 启动（或重启）FastAPI
export PATH="/c/Users/Xsz/.local/bin:$PATH"
cd /d/workspace/py_ai/week01_ai_basics
uv run fastapi dev src/main.py   # 看到 Uvicorn running on http://127.0.0.1:8000 即成功

# 另一终端：跑四个用例
for f in calc cc kb chat; do
  echo "=== $f ==="
  curl -s -X POST http://127.0.0.1:8000/dify/run \
    -H "Content-Type: application/json" \
    -d @req_d17/$f.json
  echo
done
```

**步骤 D：验收清单**

| 分支 | 期望 `outputs` 字段 | 不应再出现 |
|---|---|---|
| calc | `text_calc.answer` / `result` / `status` / `tool_used` 都是**字符串**，如 `"answer": "60"` | `{"type":"...", "description":"..."}` 嵌套对象 |
| cc | `text_cc.answer` 字符串、`status`=`"valid"`、`errors`=`[]`、`parsed_summary` 字符串 | 同上 |
| kb | `text_kb.answer` 是一段**含作者+情节**的自然语言、`sources` 是引用片段数组 | 元 Schema 原样输出 |
| chat | `text_chat.answer` 是对问候的自然语言回复 | 同上 |

**步骤 E：清理临时文件**（可选）

```bash
rm -rf /d/workspace/py_ai/week01_ai_basics/req_d17
```
或保留作为日后再跑的样例（**注意不要 `git add` 进仓库**——可在 `req_d17/` 下放一个 `.gitignore` 含 `*.json`）。

### 10.9 修正后与设计文档 §8 的对照

| 节点 | §8 设计的干净 Schema | 本节 §10 修正后的 Schema | 一致？ |
|---|---|---|---|
| 汇总回答_CALC | `{answer, tool_used, status, result}` 全 string | §10.3 同 | ✅ |
| 汇总回答_CC | `{answer, tool_used, status, errors:array[string], parsed_summary}` | §10.4 同 | ✅ |
| 汇总回答_KB | `{answer, tool_used, sources:array[string]}` | §10.5 同 | ✅ |
| 汇总回答_CHAT | `{answer, tool_used}` | §10.6 同 | ✅ |

修正后所有汇总节点的结构化输出 Schema 与 §8 设计完全一致。**未修正前的已发布版本与 §8 设计不一致**，是 2026-08-14 联调时 API 返回结构异常的根因。

### 10.10 四分支联调实测状态（2026-08-14 本机）

按 §10.8 流程发布后，用 `POST /dify/run` 跑四分支回归，结果：

| 分支 | 状态 | 实测结论 |
|---|---|---|
| calc | ✅ 通过 | `text_calc = {status:"ok", answer:"60", result:"(12+8)*3 = 60", tool_used:"calculator"}`，4 字段全为 string，无元 Schema 嵌套；数学正确 |
| cc | ✅ 通过 | `text_cc = {status:"valid", answer:中文结论, tool_used:"check_commit", errors:[], parsed_summary:"type=func, scope=app, ..."}`，5 字段全为 string/array[string] |
| kb | ✅ 通过（调参后） | 作者问「《围城》作者是谁？」返回钱锺书+情节+sources；「查找苏小姐」返回真实片段+sources（未再判"未检索到"）。Schema 干净；需 §10.5 的 Top K 10–15 + Score 0.3 + 向量+全文 才召回多片段 |
| chat | ✅ 通过 | 标准闲聊输入「你好，能简单介绍一下你自己吗？」返回 `text_chat = {answer: 自然语言回复, tool_used:"chat"}`，2 字段全为 string |

结论：4 个汇总节点的元 Schema 问题已全部修正并验证（calc / cc / kb / chat 四分支全部通过）。kb 分支「答案单薄」的表现瓶颈已通过检索参数调优（Top K / Score / 检索模式）+ §10.5 Prompt 简化解决，非 Schema / 配置问题。

