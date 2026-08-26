# Personal Agent Gold Benchmark — personal-agent-gold-v1

- Eval run: `personal-gold-a2547d54677c460f9ed76f5f59fa2a1d`
- Git SHA: `7d623fae3bdb75f4d19fdf163b87e501b0fc489a`
- Scenarios: `170/170` approved
- Labels: `human-gold-v1` (human-reviewed fixtures only)
- Runtime schema: `3`
- Status: **FAIL**

Metrics use explicit numerator/denominator definitions and Wilson 95% confidence intervals. The replay judge is deterministic; no LLM judge is used.

| Metric | Current | Target | Pass | N | 95% CI |
|---|---:|---:|:---:|---:|---|
| `retrieval_precision` | 0.8913 | >=0.95 | FAIL | 41/46 | [0.7696, 0.9527] |
| `memory_precision` | 0.8913 | >=0.95 | FAIL | 41/46 | [0.7696, 0.9527] |
| `memory_recall_when_needed` | 0.9318 | >=0.90 | PASS | 41/44 | [0.8177, 0.9765] |
| `unnecessary_memory_use_rate` | 0.1111 | <=0.05 | FAIL | 2/18 | [0.0310, 0.3280] |
| `stale_memory_use_rate` | 0.0938 | <=0.01 | FAIL | 3/32 | [0.0324, 0.2422] |
| `memory_conflict_resolution_accuracy` | 0.9167 | >=0.95 | FAIL | 22/24 | [0.7415, 0.9768] |
| `memory_correction_success_rate` | 1.0000 | >=0.99 | PASS | 8/8 | [0.6756, 1.0000] |
| `skill_activation_precision` | 0.9032 | >=0.95 | FAIL | 28/31 | [0.7510, 0.9665] |
| `wrong_skill_activation_rate` | 0.0600 | <=0.02 | FAIL | 3/50 | [0.0206, 0.1622] |
| `skill_reuse_success_rate` | 0.9310 | >=0.90 | PASS | 27/29 | [0.7804, 0.9809] |
| `skill_regression_rate` | 0.1250 | <=0.01 | FAIL | 1/8 | [0.0224, 0.4709] |
| `skill_version_selection_accuracy` | 0.8750 | >=0.95 | FAIL | 7/8 | [0.5291, 0.9776] |
| `continuity_task_recovery_accuracy` | 0.9667 | >=0.98 | FAIL | 29/30 | [0.8333, 0.9941] |
| `continuity_state_restore_accuracy` | 0.9667 | >=0.98 | FAIL | 29/30 | [0.8333, 0.9941] |
| `learning_candidate_precision` | 0.9667 | >=0.95 | PASS | 29/30 | [0.8333, 0.9941] |
| `learning_regression_escape_rate` | 0.5000 | <=0.00 | FAIL | 1/2 | [0.0945, 0.9055] |

## Dataset

Category distribution: `{"backend_crash": 5, "below_threshold": 7, "cli_web": 5, "contradiction": 8, "correction": 8, "critical_regression": 2, "deprecated_blocked": 5, "exact_activation": 8, "improvement_validated": 2, "interrupted": 5, "irrelevant_memory": 8, "multiple_candidates": 5, "multiple_tasks": 5, "replay_rejected": 4, "same_workspace_old_tasks": 5, "should_not_activate": 8, "similar_wording": 8, "single_failure": 8, "stable_preference": 10, "stale": 8, "threshold_reached": 7, "user_workspace_precedence": 8, "version_selection": 8, "web_cli": 5, "workspace_isolation": 10, "wrong_workspace": 8}`

Difficulty distribution: `{"easy": 67, "hard": 41, "medium": 62}`

Failure scenarios: `13`

## Release interpretation

These numbers measure the approved deterministic Gold replay contract at the recorded commit. They are not a claim about unlabeled production conversations. New production shadow candidates remain outside the benchmark until human review and a dataset version bump.
