# 第 10 周 · Golden Dataset 与评测引擎

> 面向：执行与复核。可直接复制的命令统一标 **Git Bash**，在项目根执行，先
> `export PATH="/c/Users/Xsz/.local/bin:$PATH"`。
> 相关文档：`docs/week10_test_commands.md`（观测层命令）、`docs/week10_observability.md`（追踪层设计）。
> 本文对应 2026-09-22 的工作内容。

## 一、这一层解决什么

前九周的验收靠"跑一遍看着对"。这一层把"看着对"换成"能算、能比、能回归"：

| 问题 | 这一层的回答 |
| --- | --- |
| 改了检索参数，是变好还是变差？ | 同一份冻结数据集上重跑，比较同一批指标 |
| 某条用例为什么错？ | 每条结果带 `trace_id`，可直接渲染出 span 树定位到检索或生成 |
| 语料本身的局限算不算模型的错？ | 按分组统计，`known_noise` 单列说明，不改语料也不把它算进模型质量 |
| 换台机器、隔一周再跑，结论还成立吗？ | 数据集是冻结文件，指标是纯函数，只有模型与检索是外部依赖 |

设计上有三条硬约束：

1. **离线可重复**。`metrics` 是纯函数、不接网络；`dataset` 只读本地文件；只有
   `rubric` 会调模型，且它是可关闭的（`--judge off`）。
2. **不依赖服务层**。`src/evaluation/` 不导入 `agent_service`，否则一个本该能在
   裸 Python 下跑完的模块会被 FastAPI 及其依赖拖住。延迟桶边界因此在本包内保留
   一份常量，并由测试保证与 `agent_service.metrics.LATENCY_BUCKETS_MS` 取值一致。
3. **口径可追溯**。报告头部固定声明生成方式与检索方式，失败用例必须带
   `trace_id`，使每一个数字都能回到具体的 span。

## 二、Golden Dataset

位置：`data/golden/week10_golden.jsonl`，一行一条，共 **52 条**（计划要求至少 50 条）。
该目录未被 `.gitignore` 排除，可直接入库；数据集只含问题与标注，不含手册原文。

### 2.1 构成

| 场景 | 条数 | 分组 | 取材 |
| --- | --- | --- | --- |
| `knowledge_qa` | 22 | `spec` 6 / `core` 6 / `refusal` 4 / `known_noise` 3 / `cross_model` 3 | 硬件手册与概念文档的混合语料，逐条核对来源 |
| `requirement_analysis` | 18 | `normal` 6 / `ambiguous` 4 / `missing_info` 3 / `irrelevant` 3 / `very_long` 2 | 基于 `examples/requirement_samples.json` 的五类扩写 |
| `tool_call` | 12 | `allowed` 5 / `denied` 4 / `invalid` 2 / `gated` 1 | MCP 四个工具的正常调用、白名单外越权、参数不合规与确认门 |
| **合计** | **52** | 14 个分组 | — |

知识问答的五组各有用途：

- `spec`：单机型规格参数，取自两份硬件手册。计划里那条示例题
  「VT1000 的供电电压是多少？」就落在这一组，标注的两个要点已逐字核对手册原文。
- `cross_model`：VT1000 与 S102H 的同项对比，用来暴露"跨机型串台"的风险。
- `core`：六个 `rag_*.md` 概念文档上的问答，复用 `examples/qa_set.json` 的人工标注。
  复用时会逐条核对来源是否与人工标注一致，不一致直接报错，避免两处标注悄悄分叉。
- `known_noise`：诗词类问答。这些文件多为单条片段，与其它语料语义相距较远，召回
  本就受语料构成影响。按计划要求**不改语料**，单列一组并在报告中说明。
- `refusal`：知识库中没有对应内容的提问，期望模型明确拒答。

### 2.2 单条结构

```json
{
  "id": "qa-001",
  "group": "spec",
  "checks": ["retrieval_hit@3", "citation_source_hit", "answer_point_coverage>=0.6", "refusal_correct"],
  "note": "规格参数-输入电压；计划里的示例行，已核对手册原文",
  "scenario": "knowledge_qa",
  "input": {"query": "VT1000 的供电电压是多少？", "top_k": 3, "min_score": 0.0},
  "reference": {
    "expect_found": true,
    "expect_sources": ["VT1000用户手册-V1.0--2025.02.19.pdf"],
    "expect_points": ["DC IN 12V", "DC Jack"]
  }
}
```

`checks` 是一条用例的判定清单，写法是 `名称` / `名称@K` / `名称>=阈值` 三选一，
由 `metrics.parse_check` 解析。三个场景各有一套允许出现的指标名，写成别的场景的
指标会在自检阶段报错。

### 2.3 标注的严格性

数据集是评测的冻结基线。冻结的含义是：报告里任何一个变化都必须能归因到被评测的
代码，而不能来自标注自己偷偷变了。为此校验刻意偏严：

1. `extra="forbid"`：把 `expect_source` 写成 `expect_sources` 之类的笔误直接报错，
   而不是被静默忽略后当成"这条没有期望来源"，进而让一条本该失败的用例悄悄通过。
2. `input` / `reference` 按 `scenario` 做判别联合：三个场景的特征字段完全不同，
   用一个大模型加一堆可选字段会让"写错场景的字段"无法被发现。
3. 跨字段一致性由 `model_validator` 兜住：`expect_found=true` 却没有
   `expect_sources`，或 `denied` 却没有 `deny_kind`，都是自相矛盾的标注。

### 2.4 自检

```bash
# Git Bash，项目根
cd /d/workspace/py_ai/week01_ai_basics
export PATH="/c/Users/Xsz/.local/bin:$PATH"
uv run python scripts/eval_week10_build.py --check
```

预期输出（实跑取数）：

```
[PASS] 生成 52 条：{'knowledge_qa': 22, 'requirement_analysis': 18, 'tool_call': 12}
[PASS] 自检通过：id 唯一、场景条数达标、检查项可被指标引擎识别
分组：{'knowledge_qa/spec': 6, 'knowledge_qa/cross_model': 3, ...}
```

自检不通过时以非零码退出，可直接接进 CI。不带 `--check` 运行会重新生成数据集文件
（覆盖 `data/golden/week10_golden.jsonl`），只在确实要重建标注时使用。

## 三、评测引擎

`src/evaluation/` 的五个模块，各自只做一件事：

| 模块 | 职责 | 是否依赖外部设施 |
| --- | --- | --- |
| `dataset.py` | 用例模型、JSONL 读取、一致性自检 | 否（只读文件） |
| `metrics.py` | 纯函数指标库，输入"实际产出 + 标注"，输出可判定结果 | 否 |
| `outcomes.py` | 把工作流与工具的真实产出规约成指标可判定的形状 | 否 |
| `report.py` | 机器可读 JSON 与人类可读 Markdown 两份产物 | 否 |
| `rubric.py` | LLM-as-a-Judge 打分，重复判定并给一致率 | 是（可关闭） |

`outcomes.py` 单独存在，是为了不让"产出长什么样"这件事散落在执行脚本里：它只回答
两个问题——工作流产出是否满足自己声明的字段契约，工具调用的结局属于哪一种。工具
结局分了五类，其中确认门（`needs_confirmation`）与越权拒绝（`denied`）刻意分开：
前者是"等你点头"，后者是"策略不允许"，把确认门算进越权拦截率会让指标虚高。

### 3.1 指标口径

按场景统计，沿用周计划的口径：

| 场景 | 指标 | 口径 |
| --- | --- | --- |
| 知识问答 | `retrieval_hit@3` | Top-3 内出现期望来源文件名 |
| | `citation_source_hit` | 回答的 citation 来源命中期望来源 |
| | `answer_point_coverage` | 期望要点命中数 ÷ 期望要点数，默认阈值 0.6 |
| | `refusal_correct` | `has_answer=false` 与 `expect_found=false` 一致 |
| 需求拆解 | `category_match` | 分类结果与标注一致 |
| | `clarification_expected` | 是否需要澄清的判断与标注一致 |
| | `functional_coverage` | 标注主题命中数 ÷ 标注主题数 |
| | `schema_valid_rate` | 节点产出能通过字段契约校验 |
| 工具调用 | `tool_selected` | 落到的工具与期望一致 |
| | `args_schema_valid` | 参数通过工具 schema 校验 |
| | `outcome_match` | 成功 / 被拒 / 待确认与标注一致 |
| | `deny_enforced` | 白名单外调用被拒的比例，期望 100% |
| 全场景 | `pass_rate` | 单条用例全部 `checks` 均通过才算通过 |
| | `latency_p50` / `latency_p95` | 端到端耗时，桶边界沿用 `metrics.LATENCY_BUCKETS_MS` |
| | `total_tokens` / `cost_usd` | 按单价表折算，本地模型单价为 0 |

要点覆盖率用子串匹配判定，不做语义判定：某条要点只要有对应文字出现就算命中。
语义层面的"表述是否合理"交给 Rubric，不让规则指标承担它做不到的事。

标签为"跳过"的判定不进分母。比如一条用例没有标注期望要点，`answer_point_coverage`
记为跳过而不是失败，否则"没标注"会被读成"没答对"。

### 3.2 Rubric 与 LLM-as-a-Judge 的边界

- 主评测是**规则与标注比对**：确定性、可离线、可重复，覆盖全部判定项。
- LLM-as-a-Judge 只用于规则覆盖不到的表达质量，5 分制，判定模型取
  `EVAL_JUDGE_MODEL`（缺省与主模型相同），重复 `EVAL_JUDGE_REPEATS`（缺省 3）次
  并计算一致率。判定调用本身也走 `generation` span，"谁在打分、打了多少 token"
  同样可追。
- 三条已知风险与对应处理：
  1. 判定不稳定：重复判定 3 次，报告里给出**一致率**，不一致的用例转人工复核。
  2. 长度偏好与位置偏好：打分 Prompt 固定评分锚点，输出只有分数与理由。
  3. 判定模型与被测模型相同会自洽偏差：报告里明确标注该局限，Judge 分数只当参考，
     不作为质量结论。
- 因此 `--judge` 缺省关闭。要引用 Judge 分数时，先看一致率。

## 四、执行评测

```bash
# Git Bash，项目根
cd /d/workspace/py_ai/week01_ai_basics
export PATH="/c/Users/Xsz/.local/bin:$PATH"
```

先确认两个外部依赖就绪，否则 `knowledge_qa` 会整组记为执行失败：

```bash
# Git Bash：Qdrant 与 Ollama 各打一次
curl -s --noproxy '*' http://127.0.0.1:6333/collections
curl -s --noproxy '*' http://127.0.0.1:11434/api/tags | head -c 200
```

按需选择执行范围：

```bash
# 最小验证：每个场景各取前 5 条（本机约 12 分钟，knowledge_qa 占绝大部分）
uv run python scripts/eval_week10_run.py --limit 5 --tag dryrun

# 只跑工具调用：不依赖 Qdrant / Ollama，秒级完成
uv run python scripts/eval_week10_run.py --scenario tool_call --tag tools

# 全量基线，不跑 Rubric
uv run python scripts/eval_week10_run.py --tag baseline --judge off

# 全量并开启 Rubric（每条用例多 3 次判定调用，耗时显著增加）
uv run python scripts/eval_week10_run.py --tag judged --judge on
```

`--limit` 是**每个场景**的条数上限，不是总数上限：否则报告里只会剩下排在最前的那个
场景，"按场景统计"也就无从谈起。

关于耗时：`knowledge_qa` 的检索与生成都是真实链路，本机 CPU 推理下一条约 100 秒，
若模型判 `has_answer=false` 会用 `top_k × no_answer_retry` 重召再问一次，耗时翻倍。
全量跑请放后台，不要在前台等。

`--chat` 只影响工作流的节点函数（`fake` 为确定性替身），`knowledge_qa` 不受它影响。

## 五、产物与用途

每次执行产出两份报告与一份 trace：

| 产物 | 位置 | 用途 |
| --- | --- | --- |
| 机器可读 | `logs/eval/week10_eval_<tag>.json` | 每条用例的检查项明细与 `trace_id`，供对比脚本读取 |
| 人类可读 | `logs/eval/week10_eval_<tag>.md` | 按场景分节的指标表 + 失败用例清单 |
| trace | `logs/eval/traces/traces-<日期>.jsonl` | 本次评测的 span，可直接喂给 trace 视图脚本 |

`logs/` 整体被 `.gitignore` 排除，报告与 trace 都不入库，属于本地证据。

报告头部固定声明这份数字是在什么口径下跑出来的，读之前先看这几行：

| 字段 | 含义 |
| --- | --- |
| 生成方式 | `fake` 表示工作流节点走了确定性替身 |
| 检索方式 | `real (qdrant + embedding)` 表示检索走真实链路 |
| trace 目录 | 失败用例的 `trace_id` 去哪个目录找 |

拿到失败用例后，把 `trace_id` 交给视图脚本就能定位：

```bash
# Git Bash，项目根；--dir 用报告头部给出的 trace 目录
uv run python scripts/trace_week10_view.py --list --dir logs/eval/traces
uv run python scripts/trace_week10_view.py --trace-id <trace_id> --dir logs/eval/traces --out logs/eval/view.html
```

知识问答的 trace 里应同时出现 `retriever` 与 `generation` 两类 span，分别对应
"有没有召回到期望来源"和"拿到了片段后怎么答"。这两者分开，才能回答本周的关键
问题：错在检索还是在生成。

## 六、实跑结果

### 6.1 环境

| 项 | 值 |
| --- | --- |
| 数据集 | `data/golden/week10_golden.jsonl`，52 条 |
| 检索 | Qdrant `jwipc_v3`（51 点 / 1024 维），`mxbai-embed-large` |
| 生成 | Ollama `qwen3`，CPU 推理 |
| 执行 | `uv run python scripts/eval_week10_run.py --limit 5 --tag dryrun` |
| 规模 | 每个场景前 5 条，共 15 条 |
| 耗时 | 15 分 45 秒（其中知识问答占绝大部分） |

### 6.2 结果

| 场景 | 用例数 | 通过 | 通过率 | latency_p50 | latency_p95 |
| --- | --- | --- | --- | --- | --- |
| `knowledge_qa` | 5 | 1 | 20.0% | 145.3 s | 380.0 s |
| `requirement_analysis` | 5 | 4 | 80.0% | 100 ms | 2.5 s |
| `tool_call` | 5 | 5 | 100.0% | 10 ms | 50 ms |
| **合计** | **15** | **10** | **66.7%** | 100 ms | 380.0 s |

各检查项通过率：

| 场景 | 检查项 | 通过率 |
| --- | --- | --- |
| `knowledge_qa` | `retrieval_hit@3` | **100.0%** |
| | `citation_source_hit` | 40.0% |
| | `answer_point_coverage` | 20.0% |
| | `refusal_correct` | 40.0% |
| `requirement_analysis` | `category_match` | 100.0% |
| | `schema_valid_rate` | 100.0% |
| | `clarification_expected` | 80.0% |
| | `functional_coverage` | 66.7% |
| `tool_call` | `tool_selected` / `args_schema_valid` / `outcome_match` / `deny_enforced` | 均 100.0% |

### 6.3 这组数字说明什么

**检索没问题，问题在生成。** `retrieval_hit@3` 是 100%，即期望来源每次都出现在 Top-3
内；而 `answer_point_coverage` 只有 20%，`citation_source_hit` 40%。失败用例的明细也
一致：`qa-003` / `qa-004` / `qa-005` 的引用列表都是空的，模型直接判了"无法回答"。
这正是把检索与生成拆成两组指标的价值——若只统计一个总通过率，会误判成"检索不好"。

**总体延迟的中位数没有参考价值。** 合计行的 `latency_p50` 是 100 ms，因为快的两个
场景把中位数拉过去了；真正的耗时在知识问答（145 秒）。这也是"按场景统计"必须存在
的理由：跨场景求分位数会把两类完全不同的负载混成一个没有含义的数。

**两次运行结论不完全一致。** 同一份数据集在本机连跑两次，知识问答的通过条数分别是
2/5 与 1/5，差异集中在 `qa-003`。生成侧是真实模型，输出有随机性；数据集的标注与
检索结果则是确定的。因此判断回归时要看**同一场景的同一批次**，不能拿两次总数直接
相减。

**这 15 条没有覆盖到 `known_noise` 与 `refusal` 两组。** `--limit 5` 取的是每个场景的
前 5 条，`spec` 组正好占了知识问答的前 6 条。语料构成带来的召回噪声、以及拒答行为
的表现，要看全量报告。

### 6.4 失败用例的定位链路

失败清单里每行都带 `trace_id`，交给视图脚本就能得到 span 树：

```bash
# Git Bash，项目根
uv run python scripts/trace_week10_view.py --trace-id 364525400d1c482093907137d3ce4c91 --dir logs/eval/traces --out logs/eval/view.html
```

| 场景 | trace 构成 |
| --- | --- |
| 知识问答 | 5 个 span：根 + `retriever` × 2 + `generation` × 2 |
| 需求拆解 | 11 个 span：根 + `chain` × 6 + `generation` × 4 |

知识问答出现 2 次检索与 2 次生成，是因为模型第一次判 `has_answer=false`，生成链路
按 `top_k × no_answer_retry` 重召后又问了一次；这解释了个别用例 380 秒的耗时，也解释
了为什么"耗时翻倍"与"答得不好"是同一现象的两种表现。


## 七、已知问题与未解决项

**1. 拒答阈值在本语料上先天不可行。** 无答案题的 top1 相似度落在 0.617~0.757，
而有答案题并不比这更高，两个区间完全重叠。因此无论把 `min_score` 定在哪里，都无法
把"该拒答"和"该作答"分开。这不是实现问题，是语料构成决定的；本周如实记录，不改
语料、不用它当结论。

**2. 需求分析的数字在默认参数下不代表模型质量。** `--chat fake` 是缺省值，工作流
节点走确定性替身，`category_match` / `functional_coverage` 等只说明评测链路跑通。
要拿模型质量数据需显式加 `--chat real`，代价是每条用例都要等真实推理。

**3. 全量评测耗时受本机 CPU 推理限制。** 单条知识问答约 100 秒，触发重召则翻倍。
参数对比实验要跑三组时，建议先冻结数据集，再按组串行放后台执行。

**4. Rubric 缺省关闭。** 不是缺失，是刻意：判定不稳定时分数没有解释力，先看一致率
再决定是否采信。

**5. 检索埋点的挂载位置已修正。** 原来挂在 `lifespan._build_knowledge_rag` 的**内层
检索器**上，但 RAG 层的检索器统一只提供 `search()`，而 `TracedRetriever` 包的是
`retrieve()`——知识库取的是 `search`，正好绕过包装，这一层从未产出过 span。服务路由
的检索 span 实际来自 `routes/rag.py` 在外层的包装；工作流用的是 `deps.rag`、没有外层
包装，因此工作流的检索一直没有 span。现已改为在真正的调用点外侧挂：服务路由见
`routes/rag.py`，工作流见 `lifespan._build_workflow` 与本文件第四节的执行脚本。
修正后工作流的 trace 从 11 个 span 变为 12 个（新增 1 个 `retriever`）。

## 八、与其它文档的分工

| 文档 | 面向 | 内容 |
| --- | --- | --- |
| 本文件 | 执行与复核 | 数据集构成、指标口径、跑评测的命令、报告读法、实跑结果 |
| `docs/week10_test_commands.md` | 执行 | 观测层的命令与预期输出、界面与记录名对照 |
| `docs/week10_observability.md` | 工程 | 追踪层的后端切换、span 字段口径、配置键全表 |
| 桌面 `12week/week10_observability_concepts.md` | 学习 | 术语与项目落点、哪些指标能用规则算的判断表 |
