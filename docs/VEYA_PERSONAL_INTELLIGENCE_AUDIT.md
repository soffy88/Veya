# Veya Personal Intelligence Audit

运行：

```bash
python -m evals.personal_agent_gold.audit
```

审计读取 `personal-agent-gold-v1` 的 approved Gold report 和对应 failure
corpus，生成：

- `evals/personal_agent_gold/results/personal-intelligence-audit-latest.json`
- `evals/personal_agent_gold/results/personal-intelligence-audit-latest.md`

审计保留完整 16 项指标、每个分子/分母与 Wilson 95% CI，以及
difficulty、scope、session shape、memory case 和 skill case 的全部 failure
slices。它还按 domain、category、reason 汇总失败场景。

审计结论有意分开两个维度：

1. Production health：Backend、PostgreSQL authority、queue、lease、outbox 和
   reconciler 是否健康。
2. Intelligence quality gate：Gold benchmark 是否达到目标。

服务健康不代表长期智能质量 gate 通过。Gold 失败会返回
`BLOCKED_BY_GOLD_GATE`，不会被改写成成功。Production shadow candidate 不是
Gold；只有人工审核并 bump dataset version 后才能进入 benchmark。
