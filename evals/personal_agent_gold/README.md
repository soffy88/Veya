# Personal Agent Gold Benchmark

`personal-agent-gold-v1` is a versioned, manually labelled fixture corpus:

| Domain | Scenarios |
|---|---:|
| Memory | 60 |
| Skill | 50 |
| Continuity | 30 |
| Learning | 30 |
| **Total** | **170** |

Only scenarios with `review_status=approved`, `reviewer_type=human`, and the
matching `labels_version` are evaluated. The JSONL files are immutable through
their hashes in `manifest.json`; changing a label requires a new dataset
version and a new manifest hash.

The fixtures are hand-authored Gold labels and deterministic replay traces.
They are not LLM-generated labels, LLM judge output, or unlabeled production
telemetry. The replay evaluator uses no LLM. A result is therefore a measured
contract benchmark for the observable Personal Agent decisions, not a claim
that all future natural-language conversations have the same distribution.

Run from the repository root:

```bash
python -m evals.personal_agent_gold.benchmark
```

The runner writes:

- `results/<dataset>-<git-sha>.json` and `.md`;
- `results/latest.json` and `.md`;
- `failures/<eval-run-id>.jsonl`.

Generate the full audit from the latest approved report:

```bash
python -m evals.personal_agent_gold.audit
```

The audit preserves every slice and failure record and writes
`results/personal-intelligence-audit-latest.json` plus `.md`. Its status is
`BLOCKED_BY_GOLD_GATE` when the measured baseline misses a target; production
service health is reported separately.

`candidate_eval_cases.jsonl` is an optional privacy-safe shadow-candidate
sink. It is draft-only and is never loaded as Gold. The helper stores hashes,
not prompts, answers, or raw user content.

## Metric denominators

- `memory_precision`: correct memory IDs used / all memory IDs used.
- `memory_recall_when_needed`: required Gold memory IDs used / all required
  Gold memory IDs.
- `unnecessary_memory_use_rate`: scenarios with forbidden memory used / all
  scenarios where unrelated memory was an opportunity.
- `stale_memory_use_rate`: scenarios with superseded memory used / all
  scenarios containing stale/superseded memory.
- `wrong_skill_activation_rate`: scenarios with a wrong activation / all Skill
  activation opportunities, including no-activation cases.
- `skill_regression_rate`: observed regressions / version-regression
  opportunities.
- `learning_regression_escape_rate`: critical regressions applied or escaped /
  all critical-regression opportunities.

Every metric includes numerator, denominator, Wilson 95% confidence interval,
target, and pass/fail. Slice reports cover difficulty, scope, session shape,
memory correction/conflict/stale, and Skill exact/ambiguous cases.
