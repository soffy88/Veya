# RFC-09: PR-10 Eval Harness — 分类现状 + 派生指标

> 状态：已执行（2026-08-24）
> 依据：docs/VEYA_10_OF_10_PLAN.md §16（Eval 系统做到 10/10）
> 范围：诚实分类既有 8 条用例 + 从已采集数据算派生指标；明确不做机械凑数。

## 1. 决策：不为了凑数量新写用例

计划 §16 建议按 `coding/research/tool_selection/long_task/recovery/safety/
context/delegation` 八个类目建 eval set，最终规模提到 50-100 条。`rfc-05`/
`rfc-06` 系列已经确立的纪律是：eval 用例要么有真实спec/场景来源（比如
§38.1-38.5 是直接取自用户给的原始 spec 文档），要么不写——"勉强凑一个用例只会
是形式主义"（rfc-05 §3 原话）。这次没有新的 spec 文档可以摘录，所以不新写用例，
只做两件不需要编造场景就能带来真实价值的事。

## 2. 做了什么

### 2.1 诚实分类现有 8 条用例

`server/agent_eval.py::AGENT_EVAL_CASES` 每条补了 `meta={"category": ...}`。
结果分布：

| 类目 | 用例数 |
|---|---:|
| tool_selection | 6 |
| recovery | 1 |
| delegation | 1 |
| coding | 0 |
| research | 0 |
| long_task | 0 |
| safety | 0 |
| context | 0 |

这不是"扩充了 eval 覆盖"，是把已经存在的偏科现状显式标出来——8 条里 6 条都在
测"该不该调工具"这一件事，`coding`（改代码后代码真的对不对）、`safety`（危险
操作前置确认）、`context`（长上下文/压缩后还记得住）、`long_task`（跨轮续做）、
`research`（信息检索综合）五个类目零覆盖。这是诚实的现状记录，不是这轮的
遗留 bug——写这五类需要真的懂每类场景该怎么设计断言（比如 `coding` 类要断言
"改完的代码真的能跑测试"而不是"调用了 hicode_run 这个工具"，断言口径完全不同，
不是加几个 `EvalCase` 就能糊弄过去的），需要单独一轮设计，不该在这次顺手编。

### 2.2 三个派生指标（从已采集数据算，不新增采集链路）

`server/agent_eval.py` 新增：

- `category_breakdown(run, cases)`：按 `meta["category"]` 分组均分。未跑到
  的用例（不在 `run.scores` 里）不计入，避免伪造缺失数据。
- `unnecessary_tool_rate(run, cases)`：只统计 `expected.max_tool_calls == 0`
  的用例里真实触发了 >0 个工具调用的比例——不是全量用例的笼统统计（那样会跟
  "该调工具但调多了"的场景混在一起，口径不干净）。没有任何该类用例时返回
  `None`，不伪造 `0.0`（那看起来像"全部通过"而不是"没数据"）。
- `cost_per_case(run)`：直接读 `chat_stream()` 结果里已经带的 `cost_usd`
  字段（`cli/headless.py::headless_run` 已经在用这个字段），不新增任何采集
  埋点。缺字段的用例不进结果表，不伪造 `0.0` 成本。

`eval_run_to_dict(run, cases=...)` 新增可选 `cases` 参数，传入时自动附带这三项；
`scripts/run_agent_eval.py` 已接上，跑一次会打印分类均分。

## 3. 明确不做

- 不新写 eval 用例凑数量——见 §1。
- 不做 LLM-judge 评分器——`_score_result` 的规则打分器本来就是可插拔设计
  （`Scorer = Callable[[EvalCase], float]`），换成 LLM-judge 是独立的一次改动，
  不该跟这次的分类/指标工作混在一起。
- 不碰 `oskill.eval_suite`（3O 子库单一来源，`EvalCase.meta` 字段已经支持
  这次要的东西，不需要改子库本体）。

## 4. 验证

`tests/test_agent_eval_suite.py`：新增 5 条直接单测（`category_breakdown`
分组正确性 + 未跑到用例不计入、`unnecessary_tool_rate` 只统计相关用例 + 无
相关用例返回 `None`、`cost_per_case` 跳过缺字段用例）。全量 10/10 通过，含
原有的真实 `chat_stream()` 接线测试（证明分类/指标改动没碰坏真实调用链路）。
