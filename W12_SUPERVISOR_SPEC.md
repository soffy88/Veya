# W12 — Supervised long tasks (监工) + brain circuit breaker

Repo /data/soffy/projects/veya, branch feat/w12-supervisor (worktree veya-w12), base = main (d4ed7fab).
3O paradigm rules hold: NO second scheduler, event store, approval path, side-effect or capability registry.
Everything new behind flags, default OFF. Reply/test text in Chinese where user-facing.

## Goal
A bot can take one long task, keep working on it until it is verified done, and post progress
reports on a schedule — the way Hark does. Example: user tells their bot
"把 X 修好，每 30 分钟告诉我进度" → bot starts the task, posts a report every 30 min,
nudges itself when stuck, and posts a final report (with evidence) when the verifier passes.

## Part A — Supervisor (flag VEYA_SUPERVISOR=1)
A1. Supervised goal = an existing GoalRun + an existing routine bound to it. Reuse
    server/goal_run/routine_registry.py + routine_dispatch.py for the schedule and the GoalRun
    store for state. Add only a binding record (goal_run_id <-> routine_id, owner, bot, interval,
    stall_after, max_repairs) persisted where routines already persist.
A2. Start: model-callable tool `supervise_task(objective, report_every_minutes=30,
    stall_after_minutes=20, max_repairs=3)` registered with side_effect=LOCAL_WRITE (flag it in the
    report as a NEW model-callable tool). Also parse plain phrasing ("每N分钟告诉我进度/汇报") in the
    bot turn path into the same call. Owner-scoped: only the owning user's bot; ownership checked
    on every REST route.
A3. Each routine fire builds a progress report from GoalRun state only (status, todos done/total,
    last evidence, last activity time, blockers). It must not invent progress; if nothing changed
    since last report say so. Posted into the bot's own chat as a bot message with
    meta.source="supervisor", goal_run_id, report_seq.
A4. Stall: no GoalRun activity for stall_after → one continuation nudge via existing
    continuation.py / execution_recovery.py; count it. Exceeded max_repairs → report "卡住了" with
    the blocker and stop nudging (routine keeps reporting until user cancels).
A5. Done: only when the existing verifier (Verification OS generate→evaluate→repair) passes.
    Post final report with evidence summary, then delete the routine and mark binding closed.
    Failed verification → repair loop counts toward max_repairs.
A6. Controls: REST GET/POST /api/v1/bots/{bot_id}/supervised (list, cancel, pause, resume,
    change interval); owner-only (404 for others). Cancel = pause GoalRun + delete routine.
A7. Restart-safe: after backend restart, open bindings resume from persisted state; no duplicate
    reports for the same report_seq.

## Part B — Brain circuit breaker (flag VEYA_BRAIN_BREAKER=1)
Observed 2026-10-13: opencode-go returns 429 GoUsageLimitError limitName=monthly on every model;
OpenRouter free models all fail; the router still tries each tier on every turn and turns time out.
B1. In the existing router (veya/obase/llm.py, _llm_config.py — no new registry), when a provider
    answers 429/quota/limit errors, open a breaker for that provider: skip it for a cooldown
    (monthly/daily limit → until the next UTC day boundary re-probe; plain 429 → 10 min, doubling
    to 2 h). One cheap re-probe when cooldown ends.
B2. When every brain tier is open, fail fast (<3 s) with the existing friendly
    "模型服务暂时没有响应" reply instead of a 100 s timeout. Supervisor reports in that state say
    "模型不可用，任务暂停" and do not count as stalls.
B3. Expose breaker state read-only in GET /health/llm (or existing health route) — provider,
    state, reopen_at. No secrets.
B4. Fix while here: static _VEYA12_FREE_POOL seed must not shadow the model-state derived pool
    when model-state has entries.

## Tests (must add, all offline)
tests/test_w12_supervisor.py: start→reports on schedule (fake clock)→stall nudge→max_repairs
blocked→verifier pass closes routine; ownership 404; restart resume no duplicate seq; no invented
progress. tests/test_w12_brain_breaker.py: 429 monthly opens breaker, skip, re-probe, all-open
fails fast with friendly reply, health shows state, seed-vs-model-state.

## Done =
- New tests pass; regression: tests/test_lorca_*.py tests/test_orca_*.py tests/test_p3a_bot_isolation.py
  tests/test_product_shell.py tests/test_nano_*.py tests/test_w11_*.py tests/test_llm_router.py
  tests/remote/test_policy_resolver.py tests/remote/test_effect_registry_authority.py
  all pass except the 3 known env-bound tests/test_canonical_proxies.py failures.
- ~/lorca-jobs/gates.sh diff vs canonical = zero new.
- Commit on feat/w12-supervisor with clear messages. Do NOT push, do NOT touch platform/3O,
  .venv, node_modules, veya/remote/* uncommitted WIP.
- Write W12_REPORT.md in the worktree: what was built, files, new model-callable tools, test output.
