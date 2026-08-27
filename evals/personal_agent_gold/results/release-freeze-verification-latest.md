# Personal Runtime Gold Gate — Release Freeze Verification

- Verification: `release-freeze-2026-08-27`
- HEAD/tag target: `818b017d7ade557eb1023bb6cfe4a5328d00576e`
- Dataset: `personal-agent-gold-v1`
- Approved: `170/170`
- Eval run: `personal-gold-988b3aa645ff40cb8aa3940fca21b1b0`
- Gold gate: **PASS**

## Gold metrics

| Metric | N/D | Rate | 95% CI |
|---|---:|---:|---|
| memory_precision | 44/44 | 1.0000 | [0.9197, 1.0000] |
| memory_recall_when_needed | 44/44 | 1.0000 | [0.9197, 1.0000] |
| unnecessary_memory_use_rate | 0/18 | 0.0000 | [0.0000, 0.1759] |
| stale_memory_use_rate | 0/32 | 0.0000 | [0.0000, 0.1072] |
| memory_conflict_resolution_accuracy | 24/24 | 1.0000 | [0.8620, 1.0000] |
| memory_correction_success_rate | 8/8 | 1.0000 | [0.6756, 1.0000] |
| skill_activation_precision | 29/29 | 1.0000 | [0.8830, 1.0000] |
| wrong_skill_activation_rate | 0/50 | 0.0000 | [0.0000, 0.0713] |
| skill_reuse_success_rate | 29/29 | 1.0000 | [0.8830, 1.0000] |
| skill_regression_rate | 0/8 | 0.0000 | [0.0000, 0.3244] |
| skill_version_selection_accuracy | 8/8 | 1.0000 | [0.6756, 1.0000] |
| continuity_task_recovery_accuracy | 30/30 | 1.0000 | [0.8865, 1.0000] |
| continuity_state_restore_accuracy | 30/30 | 1.0000 | [0.8865, 1.0000] |
| learning_candidate_precision | 30/30 | 1.0000 | [0.8865, 1.0000] |
| learning_regression_escape_rate | 0/2 | 0.0000 | [0.0000, 0.6576] |

## Verification

- Targeted runtime/GoalRun/Eval and MasterAgent/AgentLoop/Hicode/SSE/events/memory/skill/learning tests: `331 passed, 5 skipped`; `2` failures are known baseline `veya_loop._assembly` environment failures.
- PostgreSQL durable and kill-9 tests: `5 passed`.
- Ruff: `PASS`.
- Frontend: `svelte-check 0 errors, 0 warnings`; production build `PASS`.
- Baseline parser: `60 known, 0 new regressions`.
- Existing full-suite baseline: `27 failed, 33 errors`; no new regression was observed.

## Production health

- Backend/MCP/Web: `200/200/200`.
- Durable authority: `enabled=true`, `backend=postgres`, `authority=postgresql`, `schema_version=3`.
- Queue/leases/outbox/quarantine: `0/0/0/0`.
- Reconciler: `enabled/ok`; durable and Personal Runtime: `healthy=true`.
- Container: `healthy`.

The two component tags point to the verified HEAD: `execution-runtime-v1.0.0` and `personal-runtime-gold-v1.0.0`. Gold labels and secrets were not added or changed.
