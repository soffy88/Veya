# Veya Personal Intelligence Audit

- Audit ID: `pia-personal-gold-12beca1fe0594232afc251749f27f102-0dc04ccdca64`
- Dataset: `personal-agent-gold-v1`
- Eval run: `personal-gold-12beca1fe0594232afc251749f27f102`
- Gold SHA: `8b07025289a933cac1674a898d8cc79a762b3d03`
- Approved scenarios: `170/170`
- Decision: **PASS**

This audit is based only on approved manually labelled deterministic replay evidence. It is not a production-conversation accuracy claim.

## Complete metric ledger

| Metric | Numerator / denominator | Rate | 95% CI | Target | Result |
|---|---:|---:|---|---:|:---:|
| `continuity_state_restore_accuracy` | 30/30 | 1.0000 | [0.8865, 1.0000] | >=0.98 | PASS |
| `continuity_task_recovery_accuracy` | 30/30 | 1.0000 | [0.8865, 1.0000] | >=0.98 | PASS |
| `learning_candidate_precision` | 30/30 | 1.0000 | [0.8865, 1.0000] | >=0.95 | PASS |
| `learning_regression_escape_rate` | 0/2 | 0.0000 | [0.0000, 0.6576] | <=0.00 | PASS |
| `memory_conflict_resolution_accuracy` | 24/24 | 1.0000 | [0.8620, 1.0000] | >=0.95 | PASS |
| `memory_correction_success_rate` | 8/8 | 1.0000 | [0.6756, 1.0000] | >=0.99 | PASS |
| `memory_precision` | 44/44 | 1.0000 | [0.9197, 1.0000] | >=0.95 | PASS |
| `memory_recall_when_needed` | 44/44 | 1.0000 | [0.9197, 1.0000] | >=0.90 | PASS |
| `retrieval_precision` | 44/44 | 1.0000 | [0.9197, 1.0000] | >=0.95 | PASS |
| `skill_activation_precision` | 29/29 | 1.0000 | [0.8830, 1.0000] | >=0.95 | PASS |
| `skill_regression_rate` | 0/8 | 0.0000 | [0.0000, 0.3244] | <=0.01 | PASS |
| `skill_reuse_success_rate` | 29/29 | 1.0000 | [0.8830, 1.0000] | >=0.90 | PASS |
| `skill_version_selection_accuracy` | 8/8 | 1.0000 | [0.6756, 1.0000] | >=0.95 | PASS |
| `stale_memory_use_rate` | 0/32 | 0.0000 | [0.0000, 0.1072] | <=0.01 | PASS |
| `unnecessary_memory_use_rate` | 0/18 | 0.0000 | [0.0000, 0.1759] | <=0.05 | PASS |
| `wrong_skill_activation_rate` | 0/50 | 0.0000 | [0.0000, 0.0713] | <=0.02 | PASS |

## Failure slices

### difficulty


### memory_case


### scope


### session_shape


### skill_case


## Failure summary

- Failing scenarios: `0`
- By domain: `{}`
- By category: `{}`
- By reason: `{}`
- Critical regression escapes: `0`

## Failed scenarios

| Scenario | Domain | Category | Reasons |
|---|---|---|---|

## Audit conclusion

The approved Gold contract meets every configured quality gate. The result is a deterministic replay audit, not a claim about unlabeled production conversations.
