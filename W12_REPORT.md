# W12 Report — Supervised long tasks (监工) + brain circuit breaker

Branch: `feat/w12-supervisor` (worktree `veya-w12`), base `main` = `d4ed7fab`.
Both features ship **default OFF** behind env flags:
- `VEYA_SUPERVISOR=1` — Part A supervisor
- `VEYA_BRAIN_BREAKER=1` — Part B brain circuit breaker

3O paradigm held throughout: no second scheduler, no event store, no new
approval path, no side-effect or capability registry. The GoalRun store
stays the execution authority; the RoutineRegistry stays the schedule
authority.

## Part A — Supervisor (flag `VEYA_SUPERVISOR=1`)

### Files added / modified
- `server/supervisor/__init__.py` — `enabled()` flag check.
- `server/supervisor/binding.py` — `SupervisorBinding` dataclass +
  JSON store (`save_binding` / `get_binding` / `list_bindings` /
  `delete_binding` / `bindings_for_goal`). `report_seq` is monotonic
  per routine so restarts never replay the same report.
- `server/supervisor/reporting.py` — `build_progress_report()` derives
  status / todos-done / last-evidence / stall from `GoalRunState`
  **only**. It never invents progress; a vanished GoalRun returns
  `"找不到该任务的执行状态。"`.
- `server/supervisor/handoff.py` — `supervisor_handoff()` posts the
  report into the bot's own conversation with
  `meta.source="supervisor"`, emits a HEARTBEAT nudge on stall
  (counted via `binding.nudges_sent`), and on `is_done` closes the
  binding and disables the routine (A5).
- `server/supervision_tools.py` — new model-callable tool
  `supervise_task(project_root, objective, report_every_minutes=30,
  stall_after_minutes=20, max_repairs=3)` with side effect
  `local_write`. Raises `RuntimeError("supervisor is disabled; set
  VEYA_SUPERVISOR=1 to enable")` when the flag is off.
- `server/bot_team/turns.py` — `parse_supervisor_phrase()` pure parser
  for `"每N分钟告诉我进度/汇报/上报/报告/给我进度/进展/情况/状态"`.
- `server/bot_team/routine_clock.py` — `_try_supervisor_handoff()`
  intercepts supervisor-bound routines before the normal bot-turn path.
- `server/routes/supervised.py` — REST controller:
  `GET/POST /api/v1/bots/{bot_id}/supervised` and per-binding
  `cancel / pause / resume / interval / stall / max-repairs`. Foreign
  bindings return **404** (not 403) so they are invisible.
- `server/app.py` — router registration.
- `server/feature_flags.py` — both flags registered.

### New model-callable tool
`supervise_task` (registered in `server/tool_registry.py` via
`register_tools()`), side effect `LOCAL_WRITE`, capability
`manual_only`. It is the **only** new tool in this PR.

### Plain-language trigger
`parse_supervisor_phrase("把 X 修好，每 30 分钟告诉我进度")` →
`{"report_every_minutes": 30, "stall_after_minutes": 15}` (stall
defaults to half the report interval).

## Part B — Brain circuit breaker (flag `VEYA_BRAIN_BREAKER=1`)

### Files added / modified
- `veya/obase/brain_breaker.py` — per-provider breaker:
  `classify_quota_error()` (monthly/daily/plain), `record_failure()`,
  `is_open()`, `all_tiers_open()`, `snapshot()`, `enabled()`.
  - Monthly/daily limit → skip until next UTC day boundary.
  - Plain 429 → 10 min cooldown, doubling on repeat, capped at 2 h.
  - One cheap re-probe when the cooldown ends.
- `veya/obase/llm.py` — `_veya12_rr_call` skips open breakers and
  re-probes on cooldown expiry; `llm_call`'s `HTTPStatusError`
  handler calls `record_provider_failure()`; when every tier is open,
  the router returns the existing `_MODEL_UNAVAILABLE_REPLY`
  (`"模型服务暂时没有响应…"`) in <3 s instead of a 100 s timeout (B2).
- B4 fix: `_VEYA12_FREE_POOL` static seed no longer shadows the
  model-state-derived pool when `model-state.json` has eligible
  entries.
- `/health` exposes `llm_breakers` (provider, state, reopen_at) when
  the flag is on. No secrets.

## Tests

All offline, all Chinese user-facing text.

```
$ .venv/bin/python -m pytest tests/test_w12_supervisor.py tests/test_w12_brain_breaker.py -v
============================= 26 passed in 1.13s ==============================
```

`tests/test_w12_supervisor.py` (16 tests):
- flag off → `supervise_task` raises
- flag on → GoalRun + RoutineSpec + SupervisorBinding created together
- phrase parsing (basic, short forms, no-cadence, zero-interval)
- progress report (1/3 done, no-invented-progress, all-completed)
- stall detection (stalled when idle, not stalled when recent)
- missing GoalRun → friendly message, no crash
- ownership: foreign bot → 404; owner bot → allowed
- restart-resume: `bindings_for_goal` excludes closed, no dup seq
- binding CRUD roundtrip

`tests/test_w12_brain_breaker.py` (10 tests):
- `classify_quota_error` (monthly/daily/plain/non-quota)
- monthly 429 opens breaker; state carries no secrets
- same-kind repeat doubles cooldown; capped at 2 h
- breaker respects cooldown expiry; success closes breaker
- `all_tiers_open` detection; flag off is inert; non-quota errors do
  not open a breaker

## Regression

```
$ .venv/bin/python -m pytest tests/test_lorca_*.py tests/test_orca_*.py \
    tests/test_p3a_bot_isolation.py tests/test_product_shell.py \
    tests/test_nano_*.py tests/test_w11_*.py tests/test_llm_router.py \
    tests/test_w12_*.py tests/remote/test_policy_resolver.py \
    tests/remote/test_effect_registry_authority.py -q
================= 915 passed, 27 warnings in 160.35s =================
```

(Excludes the 3 known env-bound `tests/test_canonical_proxies.py`
failures, unchanged from base.)

## Commits (on `feat/w12-supervisor`)

```
37807e04 w12: lint fixes (import sorting, unused imports, parenthesized and-chain)
92b90c16 w12(supervisor): wire A3/A4/A5 handoff into routine_clock, add completion/close logic
6893891a w12(supervisor): add binding store, progress reporter, REST routes, supervise_task tool, phrase parser
7ef4bb08 w12(breaker): add B2 fast-fail + enabled() helper + tests
6678aaf1 w12: register VEYA_BRAIN_BREAKER and VEYA_SUPERVISOR flags
afb8aa79 w12(breaker): deduplicate re-probe loop in _veya12_rr_call
bcad1792 w12(breaker): wire breaker into llm.py and /health
d6fe83b5 w12(breaker): add brain circuit breaker module (VEYA_BRAIN_BREAKER=1)
01012c51 w12: add supervisor + brain breaker spec
```

Not pushed. `platform/3O`, `.venv`, `node_modules`, and
`veya/remote/*` WIP untouched.
