# Veya Required Release Candidate

## Verified baseline

- Verified HEAD: `1a7470031bce75643d4175dfd4bbc7515d7e73cc`
- `origin/main` matched the verified HEAD.
- CI: `33047844480` — **success**
- Required pytest: **1184 passed, 10 skipped**
- New regressions: **0**
- Required release blockers: **0**

## Health

| Check | Result | Evidence |
|---|---:|---|
| Public `/health` | PASS | HTTP 200, aggregate status `ok` |
| Backend `/health` | PASS | HTTP 200, version `0.5.1` |
| MCP health | PASS | HTTP 200, `tools_count=170` |
| Web | PASS | HTTP 200 |
| Durable authority | PASS | PostgreSQL, enabled, healthy, schema `3` |
| Durable idle state | PASS | queue `0`, active leases `0`, outbox `0`, quarantine `0` |
| Reconciler | PASS | enabled and healthy |
| Personal Runtime | PASS | PostgreSQL, enabled, healthy, schema `3` |
| Gold benchmark | PASS | `personal-agent-gold-v1`, approved `170/170` |
| Latest Gold eval | PASS | `personal-gold-12beca1fe0594232afc251749f27f102` |

The public aggregate endpoint performs real backend, durable-runtime, and
Personal Runtime probes. A healthy aggregate returns HTTP 200; dependency
failure returns `503` with `status=degraded`.

## Required quality gates

- Ruff format: **PASS** in CI.
- Ruff lint: **PASS**.
- Frontend `svelte-check`: **PASS**, 0 errors and 0 warnings.
- Frontend build: **PASS**.
- Personal Gold gate: **PASS**.
- Execution Runtime ABI: unchanged.
- Gold dataset: unchanged.

## Optional/full legacy status

The optional/full legacy suite retains **18 historical failures**. They are
outside required CI and are not new Personal Runtime or Execution Runtime
regressions. They remain explicitly tracked in
`docs/release-health/optional-suite-baseline-latest.md` and do not block this
required release candidate.

## Protected worktree files

These pre-existing user changes remain modified, uncommitted, and excluded:

- `tests/test_inferera_free_pool.py`
- `veya/obase/_llm_config.py`
- `veya/obase/llm.py`

No secrets, production credentials, Gold labels, or temporary test artifacts
are included in this snapshot.
