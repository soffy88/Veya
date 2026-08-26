# Personal Agent Gold Benchmark — personal-agent-gold-v1

- Eval run: `personal-gold-7cce444ff93c4812a0cbd462031c0a8f`
- Git SHA: `2a4d9e3ca241021df2f524d1dd8bd57008d31f60`
- Scenarios: `170/170` approved
- Labels: `human-gold-v1` (human-reviewed fixtures only)
- Runtime schema: `3`
- Status: **PASS**

Metrics use explicit numerator/denominator definitions and Wilson 95% confidence intervals. The replay judge is deterministic; no LLM judge is used.

| Metric | Current | Target | Pass | N | 95% CI |
|---|---:|---:|:---:|---:|---|
| `retrieval_precision` | 1.0000 | >=0.95 | PASS | 44/44 | [0.9197, 1.0000] |
| `memory_precision` | 1.0000 | >=0.95 | PASS | 44/44 | [0.9197, 1.0000] |
| `memory_recall_when_needed` | 1.0000 | >=0.90 | PASS | 44/44 | [0.9197, 1.0000] |
| `unnecessary_memory_use_rate` | 0.0000 | <=0.05 | PASS | 0/18 | [0.0000, 0.1759] |
| `stale_memory_use_rate` | 0.0000 | <=0.01 | PASS | 0/32 | [0.0000, 0.1072] |
| `memory_conflict_resolution_accuracy` | 1.0000 | >=0.95 | PASS | 24/24 | [0.8620, 1.0000] |
| `memory_correction_success_rate` | 1.0000 | >=0.99 | PASS | 8/8 | [0.6756, 1.0000] |
| `skill_activation_precision` | 1.0000 | >=0.95 | PASS | 29/29 | [0.8830, 1.0000] |
| `wrong_skill_activation_rate` | 0.0000 | <=0.02 | PASS | 0/50 | [0.0000, 0.0713] |
| `skill_reuse_success_rate` | 1.0000 | >=0.90 | PASS | 29/29 | [0.8830, 1.0000] |
| `skill_regression_rate` | 0.0000 | <=0.01 | PASS | 0/8 | [0.0000, 0.3244] |
| `skill_version_selection_accuracy` | 1.0000 | >=0.95 | PASS | 8/8 | [0.6756, 1.0000] |
| `continuity_task_recovery_accuracy` | 1.0000 | >=0.98 | PASS | 30/30 | [0.8865, 1.0000] |
| `continuity_state_restore_accuracy` | 1.0000 | >=0.98 | PASS | 30/30 | [0.8865, 1.0000] |
| `learning_candidate_precision` | 1.0000 | >=0.95 | PASS | 30/30 | [0.8865, 1.0000] |
| `learning_regression_escape_rate` | 0.0000 | <=0.00 | PASS | 0/2 | [0.0000, 0.6576] |

## Dataset

Category distribution: `{"backend_crash": 5, "below_threshold": 7, "cli_web": 5, "contradiction": 8, "correction": 8, "critical_regression": 2, "deprecated_blocked": 5, "exact_activation": 8, "improvement_validated": 2, "interrupted": 5, "irrelevant_memory": 8, "multiple_candidates": 5, "multiple_tasks": 5, "replay_rejected": 4, "same_workspace_old_tasks": 5, "should_not_activate": 8, "similar_wording": 8, "single_failure": 8, "stable_preference": 10, "stale": 8, "threshold_reached": 7, "user_workspace_precedence": 8, "version_selection": 8, "web_cli": 5, "workspace_isolation": 10, "wrong_workspace": 8}`

Difficulty distribution: `{"easy": 67, "hard": 41, "medium": 62}`

Failure scenarios: `0`

Quality policy: `personal-runtime-quality-v1`

## Release interpretation

These numbers measure the approved deterministic Gold replay contract at the recorded commit. They are not a claim about unlabeled production conversations. New production shadow candidates remain outside the benchmark until human review and a dataset version bump.

## Comparison with previous failed run

Baseline eval: `personal-gold-a2547d54677c460f9ed76f5f59fa2a1d` (`7d623fae3bdb75f4d19fdf163b87e501b0fc489a`)

| Metric | Previous | Current | Delta |
|---|---:|---:|---:|
| `retrieval_precision` | 0.8913043478260869 | 1.0 | 0.10869565217391308 |
| `memory_precision` | 0.8913043478260869 | 1.0 | 0.10869565217391308 |
| `memory_recall_when_needed` | 0.9318181818181818 | 1.0 | 0.06818181818181823 |
| `unnecessary_memory_use_rate` | 0.1111111111111111 | 0.0 | -0.1111111111111111 |
| `stale_memory_use_rate` | 0.09375 | 0.0 | -0.09375 |
| `memory_conflict_resolution_accuracy` | 0.9166666666666666 | 1.0 | 0.08333333333333337 |
| `memory_correction_success_rate` | 1.0 | 1.0 | 0.0 |
| `skill_activation_precision` | 0.9032258064516129 | 1.0 | 0.09677419354838712 |
| `wrong_skill_activation_rate` | 0.06 | 0.0 | -0.06 |
| `skill_reuse_success_rate` | 0.9310344827586207 | 1.0 | 0.06896551724137934 |
| `skill_regression_rate` | 0.125 | 0.0 | -0.125 |
| `skill_version_selection_accuracy` | 0.875 | 1.0 | 0.125 |
| `continuity_task_recovery_accuracy` | 0.9666666666666667 | 1.0 | 0.033333333333333326 |
| `continuity_state_restore_accuracy` | 0.9666666666666667 | 1.0 | 0.033333333333333326 |
| `learning_candidate_precision` | 0.9666666666666667 | 1.0 | 0.033333333333333326 |
| `learning_regression_escape_rate` | 0.5 | 0.0 | -0.5 |