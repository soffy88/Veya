# Veya Personal Intelligence Audit

- Audit ID: `pia-personal-gold-a2547d54677c460f9ed76f5f59fa2a1d-c664324d52e2`
- Dataset: `personal-agent-gold-v1`
- Eval run: `personal-gold-a2547d54677c460f9ed76f5f59fa2a1d`
- Gold SHA: `7d623fae3bdb75f4d19fdf163b87e501b0fc489a`
- Approved scenarios: `170/170`
- Decision: **BLOCKED_BY_GOLD_GATE**

This audit is based only on approved manually labelled deterministic replay evidence. It is not a production-conversation accuracy claim.

## Complete metric ledger

| Metric | Numerator / denominator | Rate | 95% CI | Target | Result |
|---|---:|---:|---|---:|:---:|
| `continuity_state_restore_accuracy` | 29/30 | 0.9667 | [0.8333, 0.9941] | >=0.98 | FAIL |
| `continuity_task_recovery_accuracy` | 29/30 | 0.9667 | [0.8333, 0.9941] | >=0.98 | FAIL |
| `learning_candidate_precision` | 29/30 | 0.9667 | [0.8333, 0.9941] | >=0.95 | PASS |
| `learning_regression_escape_rate` | 1/2 | 0.5000 | [0.0945, 0.9055] | <=0.00 | FAIL |
| `memory_conflict_resolution_accuracy` | 22/24 | 0.9167 | [0.7415, 0.9768] | >=0.95 | FAIL |
| `memory_correction_success_rate` | 8/8 | 1.0000 | [0.6756, 1.0000] | >=0.99 | PASS |
| `memory_precision` | 41/46 | 0.8913 | [0.7696, 0.9527] | >=0.95 | FAIL |
| `memory_recall_when_needed` | 41/44 | 0.9318 | [0.8177, 0.9765] | >=0.90 | PASS |
| `retrieval_precision` | 41/46 | 0.8913 | [0.7696, 0.9527] | >=0.95 | FAIL |
| `skill_activation_precision` | 28/31 | 0.9032 | [0.7510, 0.9665] | >=0.95 | FAIL |
| `skill_regression_rate` | 1/8 | 0.1250 | [0.0224, 0.4709] | <=0.01 | FAIL |
| `skill_reuse_success_rate` | 27/29 | 0.9310 | [0.7804, 0.9809] | >=0.90 | PASS |
| `skill_version_selection_accuracy` | 7/8 | 0.8750 | [0.5291, 0.9776] | >=0.95 | FAIL |
| `stale_memory_use_rate` | 3/32 | 0.0938 | [0.0324, 0.2422] | <=0.01 | FAIL |
| `unnecessary_memory_use_rate` | 2/18 | 0.1111 | [0.0310, 0.3280] | <=0.05 | FAIL |
| `wrong_skill_activation_rate` | 3/50 | 0.0600 | [0.0206, 0.1622] | <=0.02 | FAIL |

## Failure slices

### difficulty

- `easy` (67 scenarios):
  - `continuity_state_restore_accuracy` 11/12 = 0.9167; CI [0.6461, 0.9851]
  - `continuity_task_recovery_accuracy` 11/12 = 0.9167; CI [0.6461, 0.9851]
  - `learning_candidate_precision` 12/13 = 0.9231; CI [0.6669, 0.9863]
  - `learning_regression_escape_rate` 1/1 = 1.0000; CI [0.2065, 1.0000]
  - `memory_conflict_resolution_accuracy` 7/9 = 0.7778; CI [0.4526, 0.9368]
  - `memory_precision` 14/19 = 0.7368; CI [0.5121, 0.8819]
  - `memory_recall_when_needed` 14/17 = 0.8235; CI [0.5897, 0.9381]
  - `retrieval_precision` 14/19 = 0.7368; CI [0.5121, 0.8819]
  - `skill_activation_precision` 10/13 = 0.7692; CI [0.4974, 0.9182]
  - `skill_regression_rate` 1/3 = 0.3333; CI [0.0615, 0.7923]
  - `skill_version_selection_accuracy` 2/3 = 0.6667; CI [0.2077, 0.9385]
  - `stale_memory_use_rate` 3/12 = 0.2500; CI [0.0889, 0.5323]
  - `unnecessary_memory_use_rate` 2/7 = 0.2857; CI [0.0822, 0.6411]
  - `wrong_skill_activation_rate` 3/19 = 0.1579; CI [0.0552, 0.3757]

### memory_case

- `conflict` (16 scenarios):
  - `memory_conflict_resolution_accuracy` 14/16 = 0.8750; CI [0.6398, 0.9650]
  - `memory_precision` 14/16 = 0.8750; CI [0.6398, 0.9650]
  - `memory_recall_when_needed` 14/16 = 0.8750; CI [0.6398, 0.9650]
  - `retrieval_precision` 14/16 = 0.8750; CI [0.6398, 0.9650]
  - `stale_memory_use_rate` 2/16 = 0.1250; CI [0.0350, 0.3602]
- `stale` (8 scenarios):
  - `memory_precision` 0/1 = 0.0000; CI [0.0000, 0.7935]
  - `retrieval_precision` 0/1 = 0.0000; CI [0.0000, 0.7935]
  - `stale_memory_use_rate` 1/8 = 0.1250; CI [0.0224, 0.4709]

### scope

- `user` (8 scenarios):
  - `memory_conflict_resolution_accuracy` 7/8 = 0.8750; CI [0.5291, 0.9776]
  - `memory_precision` 7/8 = 0.8750; CI [0.5291, 0.9776]
  - `memory_recall_when_needed` 7/8 = 0.8750; CI [0.5291, 0.9776]
  - `retrieval_precision` 7/8 = 0.8750; CI [0.5291, 0.9776]
  - `stale_memory_use_rate` 1/8 = 0.1250; CI [0.0224, 0.4709]
- `workspace` (170 scenarios):
  - `continuity_state_restore_accuracy` 29/30 = 0.9667; CI [0.8333, 0.9941]
  - `continuity_task_recovery_accuracy` 29/30 = 0.9667; CI [0.8333, 0.9941]
  - `learning_regression_escape_rate` 1/2 = 0.5000; CI [0.0945, 0.9055]
  - `memory_conflict_resolution_accuracy` 22/24 = 0.9167; CI [0.7415, 0.9768]
  - `memory_precision` 41/46 = 0.8913; CI [0.7696, 0.9527]
  - `retrieval_precision` 41/46 = 0.8913; CI [0.7696, 0.9527]
  - `skill_activation_precision` 28/31 = 0.9032; CI [0.7510, 0.9665]
  - `skill_regression_rate` 1/8 = 0.1250; CI [0.0224, 0.4709]
  - `skill_version_selection_accuracy` 7/8 = 0.8750; CI [0.5291, 0.9776]
  - `stale_memory_use_rate` 3/32 = 0.0938; CI [0.0324, 0.2422]
  - `unnecessary_memory_use_rate` 2/18 = 0.1111; CI [0.0310, 0.3280]
  - `wrong_skill_activation_rate` 3/50 = 0.0600; CI [0.0206, 0.1622]

### session_shape

- `multi-session` (162 scenarios):
  - `continuity_state_restore_accuracy` 29/30 = 0.9667; CI [0.8333, 0.9941]
  - `continuity_task_recovery_accuracy` 29/30 = 0.9667; CI [0.8333, 0.9941]
  - `learning_regression_escape_rate` 1/2 = 0.5000; CI [0.0945, 0.9055]
  - `memory_conflict_resolution_accuracy` 22/24 = 0.9167; CI [0.7415, 0.9768]
  - `memory_precision` 41/46 = 0.8913; CI [0.7696, 0.9527]
  - `retrieval_precision` 41/46 = 0.8913; CI [0.7696, 0.9527]
  - `skill_activation_precision` 28/31 = 0.9032; CI [0.7510, 0.9665]
  - `skill_regression_rate` 1/8 = 0.1250; CI [0.0224, 0.4709]
  - `skill_version_selection_accuracy` 7/8 = 0.8750; CI [0.5291, 0.9776]
  - `stale_memory_use_rate` 3/32 = 0.0938; CI [0.0324, 0.2422]
  - `unnecessary_memory_use_rate` 2/18 = 0.1111; CI [0.0310, 0.3280]
  - `wrong_skill_activation_rate` 3/50 = 0.0600; CI [0.0206, 0.1622]

### skill_case

- `ambiguous` (42 scenarios):
  - `skill_activation_precision` 20/23 = 0.8696; CI [0.6787, 0.9546]
  - `skill_regression_rate` 1/8 = 0.1250; CI [0.0224, 0.4709]
  - `skill_version_selection_accuracy` 7/8 = 0.8750; CI [0.5291, 0.9776]
  - `wrong_skill_activation_rate` 3/42 = 0.0714; CI [0.0246, 0.1901]

## Failure summary

- Failing scenarios: `13`
- By domain: `{"continuity": 2, "learning": 1, "memory": 5, "skill": 5}`
- By category: `{"backend_crash": 1, "contradiction": 1, "critical_regression": 1, "irrelevant_memory": 1, "multiple_candidates": 1, "multiple_tasks": 1, "should_not_activate": 1, "stale": 1, "user_workspace_precedence": 1, "version_selection": 2, "workspace_isolation": 1, "wrong_workspace": 1}`
- By reason: `{"conflict_resolution_failed": 2, "continuity_state_not_restored": 1, "critical_regression_escaped": 1, "forbidden_or_unexpected_retrieval": 5, "forbidden_or_unexpected_usage": 5, "learning_decision_mismatch": 1, "skill_regression_occurred": 1, "skill_reuse_failed": 2, "stale_memory_used": 3, "wrong_skill_activation": 3, "wrong_skill_version": 1, "wrong_task_recovery": 1}`
- Critical regression escapes: `1`

## Failed scenarios

| Scenario | Domain | Category | Reasons |
|---|---|---|---|
| `mem-irrelevant_memory-001` | memory | irrelevant_memory | forbidden_or_unexpected_retrieval, forbidden_or_unexpected_usage |
| `mem-contradiction-001` | memory | contradiction | forbidden_or_unexpected_retrieval, forbidden_or_unexpected_usage, stale_memory_used, conflict_resolution_failed |
| `mem-stale-001` | memory | stale | forbidden_or_unexpected_retrieval, forbidden_or_unexpected_usage, stale_memory_used |
| `mem-workspace_isolation-001` | memory | workspace_isolation | forbidden_or_unexpected_retrieval, forbidden_or_unexpected_usage |
| `mem-user_workspace_precedence-001` | memory | user_workspace_precedence | forbidden_or_unexpected_retrieval, forbidden_or_unexpected_usage, stale_memory_used, conflict_resolution_failed |
| `skill-wrong_workspace-001` | skill | wrong_workspace | wrong_skill_activation |
| `skill-should_not_activate-001` | skill | should_not_activate | wrong_skill_activation |
| `skill-version_selection-001` | skill | version_selection | skill_regression_occurred, wrong_skill_version |
| `skill-version_selection-002` | skill | version_selection | skill_reuse_failed |
| `skill-multiple_candidates-001` | skill | multiple_candidates | wrong_skill_activation, skill_reuse_failed |
| `cont-backend_crash-001` | continuity | backend_crash | continuity_state_not_restored |
| `cont-multiple_tasks-001` | continuity | multiple_tasks | wrong_task_recovery |
| `learning-critical_regression-001` | learning | critical_regression | learning_decision_mismatch, critical_regression_escaped |

## Audit conclusion

The runtime is production-healthy, but this baseline is not intelligence-quality-gate healthy. Memory stale-use/conflict, wrong Skill activation/version choice, continuity misses, and a critical learning regression escape remain release blockers. The failure corpus contains expected/actual/replay evidence for each failure.
