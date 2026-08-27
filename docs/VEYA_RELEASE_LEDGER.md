# Veya Release Ledger

This ledger records component-level release baselines. It does not replace
`docs/ARCHITECTURE_STABLE.md` or the Execution Runtime ABI.

## Personal Runtime Gold v1.0.0

- Tag: `personal-runtime-gold-v1.0.0`
- Execution tag: `execution-runtime-v1.0.0`
- Frozen Gold commit: `818b017d7ade557eb1023bb6cfe4a5328d00576e`
- Main after remote integration: `ef26221903d73c9d5e220d929dd234a9d0bac55d`
- Dataset: `personal-agent-gold-v1`
- Approved scenarios: `170/170`
- Eval run: `personal-gold-988b3aa645ff40cb8aa3940fca21b1b0`
- Runtime schema: `3`
- Gold gate: **PASS**

### Gate metrics

| Metric | N/D | Rate | 95% CI | Gate |
|---|---:|---:|---:|:---:|
| memory_precision | 44/44 | 1.0000 | [0.9197, 1.0000] | PASS |
| memory_recall_when_needed | 44/44 | 1.0000 | [0.9197, 1.0000] | PASS |
| unnecessary_memory_use_rate | 0/18 | 0.0000 | [0.0000, 0.1759] | PASS |
| stale_memory_use_rate | 0/32 | 0.0000 | [0.0000, 0.1072] | PASS |
| memory_conflict_resolution_accuracy | 24/24 | 1.0000 | [0.8620, 1.0000] | PASS |
| memory_correction_success_rate | 8/8 | 1.0000 | [0.6756, 1.0000] | PASS |
| skill_activation_precision | 29/29 | 1.0000 | [0.8830, 1.0000] | PASS |
| wrong_skill_activation_rate | 0/50 | 0.0000 | [0.0000, 0.0713] | PASS |
| skill_reuse_success_rate | 29/29 | 1.0000 | [0.8830, 1.0000] | PASS |
| skill_regression_rate | 0/8 | 0.0000 | [0.0000, 0.3244] | PASS |
| skill_version_selection_accuracy | 8/8 | 1.0000 | [0.6756, 1.0000] | PASS |
| continuity_task_recovery_accuracy | 30/30 | 1.0000 | [0.8865, 1.0000] | PASS |
| continuity_state_restore_accuracy | 30/30 | 1.0000 | [0.8865, 1.0000] | PASS |
| learning_candidate_precision | 30/30 | 1.0000 | [0.8865, 1.0000] | PASS |
| learning_regression_escape_rate | 0/2 | 0.0000 | [0.0000, 0.6576] | PASS |

### Production verification

- Backend/MCP/Web: HTTP `200/200/200`.
- Durable authority: `enabled=true`, `backend=postgres`, `authority=postgresql`.
- Schema version: `3`; durable and Personal Runtime `healthy=true`.
- Queue/active leases/pending outbox/quarantine: `0/0/0/0`.
- Reconciler: `enabled/ok`; container: `healthy`.
- PostgreSQL durable and kill-9 suite: `5 passed`.
- Targeted runtime/GoalRun/Eval and integration suite: `331 passed, 5 skipped`; `2` known baseline failures.
- Ruff: `PASS`.
- Frontend: `svelte-check 0 errors, 0 warnings`; build `PASS`.

### Regression and CI status

- Full-suite known baseline: `27 failed, 33 errors`.
- Baseline comparison: `60 known, 0 new regressions`.
- Post-push CI run: `33026580466`, failed for unrelated existing environment/dependency conditions:
  - checkout could not fetch the existing `platform/3O/obase` commit `f808d316…`;
  - mypy reported 20 existing errors in `veya/obase/sandbox.py`.
- No Personal Runtime source or ABI regression was found in the local release verification.

### Invariants

- Single MasterAgent user path is preserved.
- GoalRun remains durable execution authority; it is not semantic authority.
- Execution Runtime 1.0 queue, lease, heartbeat, fencing, SideEffectLedger,
  outbox, Fan-In, and finalization contracts are unchanged.
- `docs/ARCHITECTURE_STABLE.md` remains authoritative.
- Remote workers remain outside the 1.0 release gate.

### Explicit exclusions

The following pre-existing user worktree changes were not staged, committed,
formatted, overwritten, or pushed by this release freeze:

- `tests/test_inferera_free_pool.py`
- `veya/obase/_llm_config.py`
- `veya/obase/llm.py`

## Main CI Green Baseline

- Date: `2026-08-27`
- HEAD: `bdeb1a825167c70eac65284cc10b6934842b61f1`
- Required CI run: `33043176096` — **success**
- Required pytest: `1184 passed, 10 skipped, 0 new regressions`
- Ruff format: **PASS**
- Ruff lint: **PASS**
- mypy: **PASS**
- 3O reverse dependency: **PASS**
- async contract: **PASS**
- Frontend: `svelte-check` **PASS**; build **PASS**
- Personal Gold gate: **PASS**, dataset `personal-agent-gold-v1`, approved `170/170`
- Durable health: **PASS**, `enabled=true`, PostgreSQL authority, `healthy=true`,
  schema `3`, queue `0`, active leases `0`, pending outbox `0`, quarantine `0`,
  reconciler `ok`
- Personal Runtime health: **PASS**, PostgreSQL authority, `healthy=true`,
  Gold `170`, gate `PASS`, eval
  `personal-gold-12beca1fe0594232afc251749f27f102`
- Optional/full legacy status: **18 historical failures**, outside required CI;
  retained and explicitly documented in
  `docs/release-health/optional-suite-baseline-latest.md`
- Protected user files excluded from this release documentation commit:
  `tests/test_inferera_free_pool.py`, `veya/obase/_llm_config.py`,
  `veya/obase/llm.py`

## Public Health Route Fix

- Date: `2026-08-27`
- Fix commit: `4bcf412f3b03e79b55cc92b4e8f6dfbb84139ce7`
- Route: `apps/web/src/routes/health/+server.ts`
- Root cause: Caddy routed non-API traffic to SvelteKit, where `/health` had no
  route and SSR returned HTTP 500.
- Public `/health`: HTTP `500 → 200`
- Behavior: sanitized, real probes of backend, durable runtime, and Personal
  Runtime; returns `503 degraded` when required probes fail.
- Verified fields: `web=ok`, `backend=ok`, `durable=ok`,
  `personal_runtime=ok`, `schema_version=3`, `gold_gate=PASS`,
  `gold_approved=170`
- Durable/Personal semantics, Gold dataset, Execution Runtime ABI, and release
  tags were not changed.
