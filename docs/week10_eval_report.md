# 第十周 评测报告：三组参数对比

这份报告回答一个问题：**在同一份冻结数据集上，检索策略与生成提示词各自改动一次，数字
会不会动、往哪动、有没有引入退化。**

做法是逐组单跑、各组只动自己那一侧的设置，再把三组并排：`retrieval` 组严格单变量（只换
检索策略），`prompt` 组一次动了两处生成侧设置（提示词与阈值），后者的归因限制在 3.4 说明。
对比块由
`scripts/eval_week10_compare.py` 生成（下方有生成标记，重复执行只刷新数字，不动本章
其余文字）：

```bash
# Git Bash，项目根
cd /d/workspace/py_ai/week01_ai_basics
export PATH="/c/Users/Xsz/.local/bin:$PATH"
uv run python scripts/eval_week10_compare.py --baseline logs/eval/week10_eval_baseline.json --variant logs/eval/week10_eval_retrieval.json logs/eval/week10_eval_prompt.json --out docs/week10_eval_report.md
```

## 1 数据集构成与冻结口径

数据集 `data/golden/week10_golden.jsonl`，52 条，按场景与分组切分：

| 场景 | 分组 | 条数 | 这一组要看什么 |
| --- | --- | --- | --- |
| `knowledge_qa` | `spec` | 6 | 规格参数类事实问答（供电、内存、尺寸、接口） |
| | `cross_model` | 3 | 跨机型对比（VT1000 与 S102H 的同类项） |
| | `core` | 6 | 概念与方法类问答 |
| | `known_noise` | 3 | 语料里有近似但不命中的片段，用来暴露召回噪声 |
| | `refusal` | 4 | 语料中确实没有答案，应当拒答 |
| | 小计 | 22 | 18 条有答案、4 条无答案 |
| `requirement_analysis` | `normal` | 6 | 需求清晰 |
| | `ambiguous` | 4 | 表述含糊，应触发澄清 |
| | `missing_info` | 3 | 关键信息缺失 |
| | `irrelevant` | 3 | 与需求分析无关 |
| | `very_long` | 2 | 超长输入 |
| | 小计 | 18 | |
| `tool_call` | `allowed` | 5 | 白名单内调用应当成功 |
| | `denied` | 4 | 越界路径应当被策略拒绝 |
| | `gated` | 1 | 大文件读取应当被确认门拦住 |
| | `invalid` | 2 | 参数不合 schema 应当报参数错误 |
| | 小计 | 12 | |
| **合计** | | **52** | |

取材自三份既有样本与一份工具清单，**不新增语料**：

| 来源 | 用途 |
| --- | --- |
| `examples/qa_set.json` | 知识问答题干；复用条目会交叉核对标注来源，不一致即报错 |
| `examples/retrieval_set.json` | 期望来源标注 |
| `examples/requirement_samples.json` | 需求文本（`normal` / `ambiguous` / `missing_info` / `irrelevant` / `very_long`） |
| MCP 工具清单 | `tool_call` 的四个工具与参数形态 |

检索语料是 Qdrant 上的 `jwipc_v3`（51 个点，1024 维）：`VT1000用户手册` 16 条、
`S102H&S102HT 中英文简易使用指南` 10 条、六份 `rag_*.md` 概念文档 11 条、诗词与文学
14 条。**硬件手册确实在语料里**，这是 `spec` 与 `cross_model` 两组能成立的前提。

### 1.1 冻结机制

`src/evaluation/dataset.py::assert_dataset()` 每次执行前校验四项，任一不满足即非零退出：

1. 用例 id 唯一；
2. 每个场景的条数等于约定配额（22 / 18 / 12），总数不低于 50；
3. 每条用例的 `checks` 都能被 `metrics.parse_check` 解析，且属于该场景已注册的指标名；
4. 工具名在 `KNOWN_TOOLS` 之内。

数据集本身入库（`data/golden/` 未被 `.gitignore` 排除），报告与 trace 不入库（`logs/`
整体被排除），属于本地证据。

## 2 指标定义

全部为确定性计算，不含模型判定。要点匹配在比对前会去掉空白与中英文标点并统一大小写，
因此答案的排版差异不会影响判定。

| 检查项 | 适用场景 | 判定 |
| --- | --- | --- |
| `retrieval_hit@3` | 知识问答 | 期望来源至少一个出现在 Top-3 召回里 |
| `citation_source_hit` | 知识问答 | 答案的引用来源里至少一个命中期望来源 |
| `answer_point_coverage>=0.6` | 知识问答 | 标注要点被答案覆盖的比例达到阈值 |
| `refusal_correct` | 知识问答 | 标为有答案时必须作答，标为无答案时必须拒答 |
| `category_match` | 需求分析 | 分类结果与标注一致 |
| `clarification_expected` | 需求分析 | `needs_clarify` 与标注一致 |
| `functional_coverage>=0.6` | 需求分析 | 标注功能点被覆盖的比例达到阈值 |
| `schema_valid_rate` | 需求分析 | 产出结构符合工作流契约（每条用例只有这一项，恒计一次） |
| `tool_selected` | 工具调用 | 调用的工具与期望一致 |
| `args_schema_valid` | 工具调用 | 参数通过工具 schema 校验 |
| `outcome_match` | 工具调用 | 实际结局与期望结局一致（`ok` / `denied` / `invalid` / `needs_confirmation`） |
| `deny_enforced` | 工具调用 | 应当被拒的确实被拒，且拒绝原因属于策略拒绝（`forbidden` / `bad_path` / `argument_rejected`） |

延迟与用量口径：延迟取每条用例的墙钟耗时，再用与 `agent_service.metrics` 相同的分桶
（`LATENCY_BUCKETS_MS`）取分位数，因此数值是**分桶上界**而非原始采样值；跨场景求分位数
会把两类完全不同的负载混成一个没有含义的数，所以分位数一律按场景看，合计行只作参考。

## 3 三组对比

<!-- BEGIN generated: eval_week10_compare.py -->

### 各组参数与相对基线的差异

生成时间：2026-09-23T11:43:47+00:00

| 项 | baseline | retrieval | prompt |
| --- | --- | --- | --- |
| 报告文件 | `logs/eval/week10_eval_baseline.json` | `logs/eval/week10_eval_retrieval.json` | `logs/eval/week10_eval_prompt.json` |
| tag | `baseline` | `retrieval` | `prompt` |
| `retrieval_strategy` | `—` | `hybrid` | `vector` |
| `prompt_variant` | `—` | `default` | `grounded` |
| `min_score` | `—` | `用例自带` | `0.7` |
| `chat_mode` | `—` | `fake` | `fake` |
| `limit` | `—` | `不限` | `不限` |
| 用例条数 | 52 | 52 | 52 |
| 数据集 | `data/golden/week10_golden.jsonl` | `data/golden/week10_golden.jsonl` | `data/golden/week10_golden.jsonl` |
| 相对基线的改动 | — | （baseline 未记录 config，改动无法逐项核对） | （baseline 未记录 config，改动无法逐项核对） |

> 口径告警：`baseline` 的报告里没有 `config` 字段（生成于该字段引入之前），因此「相对基线的改动」只能依据运行命令与命名推断，脚本无法核对。要消除这条告警，用同一版脚本重跑该组。

### 固定对照维度

| 维度 | baseline | retrieval | prompt |
| --- | --- | --- | --- |
| `retrieval_hit@3` | 94.4% | 100.0% | 94.4% |
| `answer_point_coverage` | 72.2% | 61.1% | 61.1% |
| `refusal_correct` | 81.8% | 90.9% | 77.3% |
| `latency_p50` | 250 ms | 250 ms | 250 ms |
| `latency_p95` | 181,952 ms（182.0 s） | 168,518 ms（168.5 s） | 183,517 ms（183.5 s） |
| `total_tokens` | 0 | 0 | 0 |

> `total_tokens` 各列都是 0，说明本轮没有采集到用量：span 上的 `usage` 始终为空，聚合结果自然是 0。该维度本组不可用，不要当作「改动没有增加成本」读。

### 总体

| 指标 | baseline | retrieval | prompt |
| --- | --- | --- | --- |
| 用例数 | 52 | 52 | 52 |
| 通过 | 37 | 36 | 35 |
| 失败 | 15 | 16 | 17 |
| pass_rate | 71.2% | 69.2% | 67.3% |
| latency_p50 | 250 ms | 250 ms | 250 ms |
| latency_p95 | 181,952 ms（182.0 s） | 168,518 ms（168.5 s） | 183,517 ms（183.5 s） |

### 按场景 × 指标（通过率）

| 场景 | 指标 | baseline | retrieval | prompt |
| --- | --- | --- | --- | --- |
| `knowledge_qa` | `answer_point_coverage` | 72.2% | 61.1% | 61.1% |
| `knowledge_qa` | `citation_source_hit` | 77.8% | 88.9% | 72.2% |
| `knowledge_qa` | `refusal_correct` | 81.8% | 90.9% | 77.3% |
| `knowledge_qa` | `retrieval_hit@3` | 94.4% | 100.0% | 94.4% |
| `requirement_analysis` | `category_match` | 63.6% | 63.6% | 63.6% |
| `requirement_analysis` | `clarification_expected` | 83.3% | 83.3% | 83.3% |
| `requirement_analysis` | `functional_coverage` | 22.2% | 22.2% | 22.2% |
| `requirement_analysis` | `schema_valid_rate` | 100.0% | 100.0% | 100.0% |
| `tool_call` | `args_schema_valid` | 100.0% | 100.0% | 100.0% |
| `tool_call` | `deny_enforced` | 100.0% | 100.0% | 100.0% |
| `tool_call` | `outcome_match` | 100.0% | 100.0% | 100.0% |
| `tool_call` | `tool_selected` | 100.0% | 100.0% | 100.0% |

### 按场景的延迟（每格：单条中位 / 该场景合计）

| 场景 | baseline | retrieval | prompt |
| --- | --- | --- | --- |
| `knowledge_qa` | `102.2 s` / `47.0 min` | `97.7 s` / `45.1 min` | `101.8 s` / `46.1 min` |
| `requirement_analysis` | `0.1 s` / `3.5 s` | `0.1 s` / `3.6 s` | `0.1 s` / `3.5 s` |
| `tool_call` | `0.0 s` / `0.3 s` | `0.0 s` / `0.5 s` | `0.0 s` / `0.5 s` |

> 上面「总体」里的 `latency_p50` 被 `requirement_analysis` 与 `tool_call` 拉到了毫秒级：这两类用例走的是进程内直调图，节点用替身，和真实问答链路差三个数量级。看延迟一律以这张分场景表为准，合计按单条串行累加，代表一轮的实际墙钟成本。

### 逐条用例翻转

#### `retrieval`

翻转 20 项（变差 9、变好 11）；不变 47 条：两边都通过 34 条、两边都失败 13 条。

**变差**（改动引入的退化，逐条列出）

| 用例 | 场景 | 组 | 检查项 | 基线 | 本组 |
| --- | --- | --- | --- | --- | --- |
| `qa-003` | `knowledge_qa` | spec | `（整条用例）` | 通过 | 失败 |
| `qa-003` | `knowledge_qa` | spec | `citation_source_hit` | 通过 | 失败 |
| `qa-003` | `knowledge_qa` | spec | `answer_point_coverage` | 通过 | 失败 |
| `qa-003` | `knowledge_qa` | spec | `refusal_correct` | 通过 | 失败 |
| `qa-007` | `knowledge_qa` | cross_model | `（整条用例）` | 通过 | 失败 |
| `qa-007` | `knowledge_qa` | cross_model | `answer_point_coverage` | 通过 | 失败 |
| `qa-010` | `knowledge_qa` | core | `（整条用例）` | 通过 | 失败 |
| `qa-010` | `knowledge_qa` | core | `answer_point_coverage` | 通过 | 失败 |
| `qa-017` | `knowledge_qa` | known_noise | `answer_point_coverage` | 通过 | 失败 |

**变好**

| 用例 | 场景 | 组 | 检查项 | 基线 | 本组 |
| --- | --- | --- | --- | --- | --- |
| `qa-005` | `knowledge_qa` | spec | `（整条用例）` | 失败 | 通过 |
| `qa-005` | `knowledge_qa` | spec | `citation_source_hit` | 失败 | 通过 |
| `qa-005` | `knowledge_qa` | spec | `answer_point_coverage` | 失败 | 通过 |
| `qa-005` | `knowledge_qa` | spec | `refusal_correct` | 失败 | 通过 |
| `qa-006` | `knowledge_qa` | spec | `citation_source_hit` | 失败 | 通过 |
| `qa-006` | `knowledge_qa` | spec | `refusal_correct` | 失败 | 通过 |
| `qa-009` | `knowledge_qa` | cross_model | `（整条用例）` | 失败 | 通过 |
| `qa-009` | `knowledge_qa` | cross_model | `citation_source_hit` | 失败 | 通过 |
| `qa-009` | `knowledge_qa` | cross_model | `answer_point_coverage` | 失败 | 通过 |
| `qa-009` | `knowledge_qa` | cross_model | `refusal_correct` | 失败 | 通过 |
| `qa-017` | `knowledge_qa` | known_noise | `retrieval_hit@3` | 失败 | 通过 |

#### `prompt`

翻转 6 项（变差 6、变好 0）；不变 50 条：两边都通过 35 条、两边都失败 15 条。

**变差**（改动引入的退化，逐条列出）

| 用例 | 场景 | 组 | 检查项 | 基线 | 本组 |
| --- | --- | --- | --- | --- | --- |
| `qa-011` | `knowledge_qa` | core | `（整条用例）` | 通过 | 失败 |
| `qa-011` | `knowledge_qa` | core | `citation_source_hit` | 通过 | 失败 |
| `qa-011` | `knowledge_qa` | core | `answer_point_coverage` | 通过 | 失败 |
| `qa-011` | `knowledge_qa` | core | `refusal_correct` | 通过 | 失败 |
| `qa-018` | `knowledge_qa` | known_noise | `（整条用例）` | 通过 | 失败 |
| `qa-018` | `knowledge_qa` | known_noise | `answer_point_coverage` | 通过 | 失败 |

**变好**

无变好用例。

<!-- END generated: eval_week10_compare.py -->

### 3.1 基线配置的确认方式

`baseline` 组产出于 `--tag baseline`，未加任何开关，因此对应
`retrieval_strategy=vector`、`prompt_variant=default`、`min_score` 取用例自带值。这一组
的报告生成于 `config` 字段引入之前，脚本无法逐项核对，上述对应关系依据两点：
一是运行命令无开关；二是该组的 `retrieval_hit@3` 为 94.4%，与本周独立测得的向量策略
召回率一致。

要消除这条告警，用同一版脚本重跑即可（约 47 分钟，绝大部分耗在知识问答的模型推理上）：

```bash
# Git Bash，项目根
uv run python scripts/eval_week10_run.py --tag baseline
```

### 3.2 拒答阈值的口径统一

本周之前有两处阈值不一致：`scripts/rag_week6_qa.py` 的 `--min-score` 默认 `0.3`，而图
配置 `RAG_MIN_SCORE` 默认 `0.0`。本次实验统一按图配置口径执行：`baseline` 与
`retrieval` 两组都取用例自带的 `0.0`，只有 `prompt` 组显式改成 `0.7`。改动值写在报告
的 `config` 里，避免出现「同一个指标、两套数字」。

`0.7` 不是随手取的：实测有答案题与无答案题的 Top-1 相似度区间完全重叠（前者
0.683~0.859，后者 0.702~0.757），把阈值放在重叠区中部，才能让这一组真正检验「阈值
能不能把该拒答和该作答分开」。这一点在第 6 节展开。

### 3.3 检索策略会影响分数量纲

不同策略返回的 `score` **不是同一个量**：`vector` 是余弦相似度（0~1），`hybrid` 是
RRF 融合分（同样落在 0~1，但含义由名次融合得出，实测可到 1.0）。因此：

- 阈值只在同一策略内可比，**不能把 `vector` 上调好的 `0.7` 直接搬到 `hybrid` 上**；
- `retrieval_hit@3` 只看来源与名次、不看分数，所以跨策略可比。

这也是三组按单变量设计的原因之一：策略与阈值一起改，得到的数字既说不清是谁的功劳，
也踩上了「分数量纲不同」这个问题。

### 3.4 怎么读这三组数字

生成块里是数字，这一节是判断。三组用的是同一份 52 条数据集、同一个 `chat_mode=fake`，
所以组间可比；`retrieval` 组只动检索策略，`prompt` 组只动生成侧。

**`retrieval`（`hybrid`）得了三项、丢了一项。**

| 维度 | baseline | retrieval | 读法 |
| --- | --- | --- | --- |
| `retrieval_hit@3` | 94.4% | 100.0% | 召回满了，向量漏掉的那条被 RRF 补回来 |
| `citation_source_hit` | 77.8% | 88.9% | 召回更全，引用落在正确文档上的比例上升 |
| `refusal_correct` | 81.8% | 90.9% | 生成侧 `has_answer=true` 由 14 条升到 16 条（数据集里 `expect_found=true` 共 18 条），无谓拒答减少 |
| `answer_point_coverage` | 72.2% | 61.1% | **唯一退化项**：召回变全，喂给生成侧的上下文也变杂，答案更散 |
| `pass_rate` | 71.2% | 69.2% | 上面四项一涨一跌，合力略降 |

逐条翻转是**变差 9 项、变好 11 项**，两边的用例不是同一批，所以不能只用总数相减作结论。
最值得看的是 `qa-017`：同一条用例上 `retrieval_hit@3` 由失败转通过，而
`answer_point_coverage` 由通过转失败——**召回对了不等于答对**。这条正好是「只看总体通过率
会掩盖退化」的实例：`retrieval_hit@3` 从 94.4% 涨到 100% 很好看，掩盖了它带来的覆盖退化。

结论：`hybrid` 在这份语料上是**得失并存**，不能按「通过率降了 2 个百分点」一句话否掉，也不能
只看召回涨了就采用。取舍取决于更在意召回还是更在意答案紧凑度。

**`prompt`（`grounded` + `min_score=0.7`）是明确的负结果。**

翻转 **变差 6 项、变好 0 项**，`pass_rate` 由 71.2% 降到 67.3%，退化集中在知识问答的四项
指标上：`answer_point_coverage` 72.2% → 61.1%、`citation_source_hit` 77.8% → 72.2%、
`refusal_correct` 81.8% → 77.3%。生成侧 `has_answer=true` 由 14 条降到 13 条（理想值 18 条），
拒答反而变多——阈值 `0.7` 落在有答案题与无答案题相似度区间的重叠中部（见 3.2），把本该作答
的题一起拒掉了。`qa-011` 是 `core` 组用例，四项指标同时变差，是最完整的一条退化样本。

有一个自查点：这一组的 `retrieval_hit@3` 与基线**完全相同**（94.4%）。改动只落在生成侧的提示词
与阈值上，检索环节没被碰到，这一列不该变——它没变，说明改动确实只作用在预期的那一层。

**归因限制（必须说明）。** 这一组按计划一次动了两处——加严提示词与上调阈值——因此它只能回答
「生成侧这一整套加严有没有用」，**不能分辨是提示词还是阈值造成的**。严格单变量地分清，需要
再跑一组只改提示词、阈值维持用例自带值：

```bash
# Git Bash，项目根；只改提示词，阈值不动
uv run python scripts/eval_week10_run.py --tag promptonly --prompt grounded
```

本轮没有跑这一组，原因是一次问答组约 46 分钟，当天时间预算不够；这里如实记为未做的对照。另外，
生成块的「多变量」告警这一轮**没有触发**（要对比的基线报告里没有 `config` 字段，脚本无从判断
改动项数），这条限制只能靠本节人工记录。

**耗时。** 三组一轮的 `knowledge_qa` 墙钟分别是 47.0 / 45.1 / 46.1 分钟，差在两分钟以内，
改动没有带来可感知的耗时变化。注意「总体」里的 `latency_p50` 是 250 ms——那是被
`requirement_analysis` 与 `tool_call` 两类替身节点用例拉下来的，看延迟要看生成块里的分场景表。

## 4 LLM-as-a-Judge 的局限声明

Rubric 判定用同一个本地模型（`EVAL_JUDGE_MODEL`，缺省取 `MODEL_NAME`）对答案的表达质量
打 1~5 分，`EVAL_JUDGE_REPEATS=3` 次，取众数、平局取低分，并给出三次的一致率。

三条必须声明的边界：

1. **判定者与生成者是同一个模型，存在自洽偏好。** 同一个模型倾向于给自己的表达风格打
   高分，因此 Rubric 分数不适合用来横向比较不同模型的答案质量，只适合看同一模型在改动
   前后的相对变化。
2. **规则能判的绝不交给判定模型。** 事实是否正确、来源是否命中、该不该拒答，全部由第 2
   节的确定性指标判定，Rubric 只看表达质量（是否啰嗦、是否结构化、是否答非所问）。
   凡是规则指标与 Rubric 结论冲突的用例，以规则指标为准。
3. **一致率低时分数没有解释力。** 缺省 `--judge off`，正是因为判定不稳定时，先看一致率
   再决定是否采信。一致率低于 1.0 的用例会被标出，其分数只作为参考。

Rubric 判定调用同样会产出 `generation` span，因此「模型给这条答案打了几分」这件事本身
在 trace 里可复查。

## 5 成本说明

本地推理的单价按 0 计，成本口径按 token 折算（`src/observability/pricing.py` 的单价表）。

本轮 `total_tokens` 各列都是 0：`generation` span 上的 `usage` 字段恒为空，聚合结果自然
是 0。也就是说**这一个维度本轮不可用**，不能读成「改动没有增加成本」。根因见第 6 节。

真正的时间成本可以从延迟一栏读出来：知识问答每条 90 秒上下，且模型判
`has_answer=false` 时会按 `top_k × no_answer_retry` 重召重问，耗时翻倍。全量 52 条一轮
约 47 分钟。

## 6 已知问题与未解决项

**1. token 用量采集不到，`total_tokens` 维度为空。** 追踪层的用量探测只认被包对象上的
`usage` / `last_usage` 两个属性（`src/observability/instrument.py::_usage_of`），而当前
模型客户端的返回值不暴露这两个名字，于是所有 `generation` span 的 `usage` 都是 `None`。
修法在客户端返回值与探测属性之间对齐一次即可，但会影响此前所有报告的该字段，故本轮
只记录、不改动。

**2. 拒答阈值在本语料上先天不可行。** 无答案题的 Top-1 相似度落在 0.702~0.757，有答案题
落在 0.683~0.859，两个区间完全重叠。因此无论阈值定在哪里，都无法把「该拒答」与「该作答」
分开。这不是实现问题，是语料构成决定的——同一份文档既有讲供电电压的段落、也有讲安装
方式的段落，句向量对二者的区分度不足以支撑一个线性阈值。本周如实记录，不改语料、不拿
它当结论。

**3. 需求分析场景在缺省参数下不代表模型质量。** `--chat fake` 是缺省值，工作流节点走
确定性替身（返回固定的四个功能点、按关键词硬判分类），因此该场景的
`category_match` / `functional_coverage` 只说明评测链路跑通。要拿模型质量数据需显式加
`--chat real`，代价是每条用例都要等真实推理。这一组的可比价值在于「同一替身下，检索
策略与提示词的改动会不会改变工作流的行为」，而不是绝对水平。

**4. 工具调用的结局枚举依赖工具层先判成功。** `tool_outcome()` 先看工具层是否成功，
再看 `valid is False`，最后才按 `kind` 分流。未登记的错误类型一律归为 `failed` 而不是
`denied`，这是刻意的——否则一个内部异常会被算进「越权拦截成功率」，把安全指标做高。

**5. 一次运行内的随机性。** 知识问答的生成侧是真实模型，输出有随机性。同一份数据集连跑
两次，通过条数会差一两条。因此判断回归要看**同一场景的同一批次**，不能拿两次总数直接
相减；也不要把个位数的通过率波动当成改动带来的效果。延迟同理：本轮是在同一台开发机上
串行跑的单次采样、没有重复，`latency_p50` / `latency_p95` 只作趋势参考，不当性能结论。

**6. 两组对比运行的工具 trace 早于一次埋点修正。** 工具 span 的 `denied` / `is_error` 原先
一律取不到值（`TracedTool` 在 `FastMCP.call_tool` 的 `(content, structured)` 二元组上直接
`getattr`），修正发生在两组对比运行开始之后、且运行中的进程已加载旧模块，因此**这两批
trace 里 `tool` span 仍是 `denied=false`**。报告里工具场景的判定走的是评测层另一条路径，
数字不受影响；要看「被拒」的留痕证据用专门补跑一次工具场景的方式复现（单条命令、秒级）：

```bash
# Git Bash，项目根；只跑工具场景前三条，输出到独立目录
uv run python scripts/eval_week10_run.py --scenario tool_call --limit 3 --tag toolsdenied --trace-dir logs/eval/traces_toolcheck
```

**7. `prompt` 组一次改了两处，退化无法归因到单个旋钮。** 这一组同时改了加严提示词与拒答
阈值 `0.7`，因此只能说「生成侧这套加严组合是负收益」，不能说清是提示词还是阈值造成的。
要分辨需补跑只改提示词的一组（阈值维持用例自带值），命令见 3.4。本轮因单组约 46 分钟的
时间成本未跑。另外生成块的「多变量」告警**这一轮没能触发**：它要靠基线报告的 `config`
字段逐项作差，而本轮的基线报告生成于该字段引入之前，脚本只能给出「未记录 config」告警。
