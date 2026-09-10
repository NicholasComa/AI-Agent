# Week 08 状态字段说明：WorkflowState

> 状态定义：`src/graph/state.py`（`TypedDict`，`total=False`）。
> 架构与节点职责见 `docs/week08_architecture.md`。

`total=False` 让调用方只需传入 `requirement_text` 即可启动；其余字段由节点
逐步写入。每个节点只回写自己负责的字段（局部 patch），列表型字段先读旧值
再追加。

---

## 字段总览

| 字段 | 类型 | 写入方 | 读取方 | 失败 / 缺省取值 |
|---|---|---|---|---|
| `requirement_text`        | `str`        | 调用方（入口）     | classify、functional_points、rag_retrieve | 无缺省，缺失即调用方错误 |
| `category`                | `str`        | classify          | report                                   | 降级为 `"other"` |
| `confidence`              | `float`      | classify          | classify（歧义判定）、report               | 降级为 `0.0` |
| `needs_clarify`           | `bool`       | classify          | `route_after_classify`                   | 降级为 `False`（系统故障不误触发人工确认） |
| `clarification_questions` | `list[str]`  | classify          | clarify                                  | 降级为 `[]` |
| `human_answers`           | `list[str]`  | clarify           | functional_points、rag_retrieve          | 无人工补充时缺省 `[]` |
| `clarify_rounds`          | `int`        | clarify           | clarify（轮次递增）                       | 缺省 `0`，上限 `max_clarify_rounds` |
| `functional_points`       | `list[str]`  | functional_points | risk、test_points、report                | 降级为 `[]` |
| `rag_context`             | `list[dict]` | rag_retrieve      | risk、report                             | 降级为 `[]` |
| `rag_degraded`            | `bool`       | rag_retrieve      | risk、report                             | 无检索器或检索抛错时为 `True` |
| `risks`                   | `list[str]`  | risk              | test_points、report                      | 降级为单条「依据有限」风险 |
| `test_points`             | `list[str]`  | test_points       | report                                   | 降级为 `[]` |
| `report`                  | `str`        | report            | 调用方（出口）                            | 始终产出，降级时标注依据不足 |
| `errors`                  | `list[dict]` | 各 LLM 节点        | 调用方、测试                              | 无失败时该键缺省（用 `.get("errors", [])` 取值） |
| `trace`                   | `list[str]`  | 全部节点           | 调用方、测试                              | 始终写入，反映实际执行路径 |

---

## 复合字段结构

### `rag_context[i]`

| 键 | 类型 | 说明 |
|---|---|---|
| `chunk_id` | `str` | 片段标识，报告引用形如 `source#chunk_id` |
| `source` | `str` | 来源文件名 |
| `text` | `str` | 片段正文 |
| `score` | `float` | 相似度；低于 `rag_min_score` 的片段不进入上下文 |

### `errors[i]`

| 键 | 类型 | 说明 |
|---|---|---|
| `node` | `str` | 失败的节点名，用于定位 |
| `type` | `str` | 异常类名，如 `RuntimeError` / `ConnectionError` |
| `message` | `str` | 异常文本 |
| `attempts` | `int` | 已尝试次数，等于 `max_retries` 时表示重试耗尽 |

---

## 配置对状态的影响

配置定义见 `src/graph/config.py`，可用 `JWIPC_GRAPH_*` 环境变量覆盖。

| 配置项 | 默认值 | 影响的状态字段 |
|---|---|---|
| `ambiguity_threshold` | `0.5` | `confidence` 低于该值 → `needs_clarify=True` |
| `max_clarify_rounds` | `2` | `clarify_rounds` 上限 |
| `max_retries` | `3` | `errors[i]["attempts"]` 的上限 |
| `rag_top_k` | `3` | `rag_context` 的最大条数 |
| `rag_min_score` | `0.0` | `rag_context` 的过滤下限；过滤后为空则 `rag_degraded=True` |

---

## 一次执行的字段快照

正常需求（Fake ChatFn，未接知识库）：

```python
{
    "requirement_text": "我们公司要做一个面向中小型零售商的 B2C 电商网站……",
    "category": "web",
    "confidence": 0.9,
    "needs_clarify": False,
    "clarification_questions": [],
    "functional_points": ["支持用户登录与注册", "提供商品浏览与搜索", "实现购物车与订单管理", "接入多种支付方式"],
    "rag_context": [],
    "rag_degraded": True,
    "risks": ["支付链路需满足等保与资金安全合规", "高并发下需保证库存与订单一致性",
              "第三方登录依赖外部服务可用性", "内部资料检索不足，风险判断依据有限，仅供参考"],
    "test_points": ["正常：完整下单到支付成功", "异常：余额不足或支付失败回滚",
                    "边界：超长商品标题与特殊字符", "回归：历史订单查询不受影响"],
    "report": "# 需求分析报告（分类：web，置信度：0.90）……",
    "trace": ["classify", "functional_points", "rag_retrieve", "risk", "test_points", "report"],
}
```

歧义需求续跑后额外出现：

```python
{
    "clarify_rounds": 1,
    "human_answers": ["面向零售商的智能推荐系统"],
    "trace": ["classify", "clarify", "functional_points", "rag_retrieve", "risk", "test_points", "report"],
}
```
