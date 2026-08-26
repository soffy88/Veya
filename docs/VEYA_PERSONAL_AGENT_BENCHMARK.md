# Veya Personal Agent Gold Benchmark

Personal Agent 的长期质量指标来自
`evals/personal_agent_gold/`，不是来自运行次数、LLM 自评分或未标注的生产
会话。当前数据集版本是 `personal-agent-gold-v1`，包含 170 个场景：Memory 60、
Skill 50、Continuity 30、Learning 30。

## Gold 与重现

场景是手工编写的 Gold label 和确定性 replay trace。每条记录必须有
`review_status=approved`、`reviewer_type=human`、`reviewed_by` 和
`labels_version`；runner 只计算 approved 场景。JSONL 文件的 SHA-256 固定在
`manifest.json`，修改历史标签必须创建新的 dataset version。

运行：

```bash
python -m evals.personal_agent_gold.benchmark
```

每次运行保存 `eval_run_id`、dataset version、label version、git SHA、schema
version、feature flags、时间戳、分子/分母、Wilson 95% CI、切片和失败场景。
失败会进入 `evals/personal_agent_gold/failures/`；JSON/Markdown 报告进入
`evals/personal_agent_gold/results/`。

当前 replay judge 是 deterministic contract，未使用 LLM judge。它验证可明确
判断的 memory ID、scope、status、skill/version、task/artifact/checkpoint 和
learning gate 行为。未来自然语言 judge 只能作为 advisory evidence，不能替代
这些 release gate。

## 指标口径

- `memory_precision` = 正确使用的 memory ID / 实际使用的全部 memory ID。
- `memory_recall_when_needed` = 正确使用的必需 Gold memory ID / 全部必需 Gold
  memory ID。
- `unnecessary_memory_use_rate` = 使用无关 memory 的场景 / 存在无关 memory
  opportunity 的场景。
- `stale_memory_use_rate` = 使用 superseded/stale memory 的场景 / 存在 stale
  memory 的场景。
- `wrong_skill_activation_rate` = 错误激活场景 / 全部 Skill activation
  opportunities（包括正确不激活的场景）。
- `skill_regression_rate` = 实际 regression / version-regression opportunities。
- `learning_regression_escape_rate` = critical regression 仍被应用或逃逸的场景 /
  critical-regression opportunities。

所有指标都输出 numerator、denominator、rate、95% CI、目标和 PASS/FAIL；零分母
输出 `null`，不会用缺失样本伪造通过。

## 生产投影与 shadow

`personal_metrics()` 只读加载 approved 报告并放入 `gold_benchmark`，同时暴露同
名 rate 字段；它不会把 Gold 记录写入 Personal Runtime，也不会改变
`memory_search`、`skill_search` 或 MasterAgent 主链。生产 Compose 默认从只读的
`/repo/evals/personal_agent_gold/results/latest.json` 加载该报告。

`candidate_eval_cases.jsonl` 只接收 hash 后的事件/任务/结果引用，默认是 draft，
不参与 benchmark；必须人工 review 并 bump dataset version 后才能进入 Gold。

Remote worker 不属于本 benchmark 或 Execution Runtime 1.0 release gate。
