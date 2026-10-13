# W12FIX Report — supervised progress never reached the user

Branch `fix/w12-live-progress` (worktree `veya-w12fix`), based on canonical
`main` `47ccc52b`. Fixes the live failure where a bot answered
「每 5 分钟告诉我进度：统计 README.md 有多少行」 and the user never received a
single progress or final report.

## Root cause

Three independent defects, all on the fire path
`routine_clock._default_handoff_consumer → _try_supervisor_handoff → supervisor_handoff`.

### 1. The supervised run was filed under the wrong bot (the primary bug)

`server/bot_team/turns.py::_supervise_from_phrase` runs **before**
`_run_product_task` binds the running-bot scope. `supervise_task` reads its owner
from `current_bot_id()`, which was therefore `None`, so every artifact — the
GoalRun, the routine, the binding, and the posted report — was stamped
`bot_id = "veya-default"` instead of the bot the user was talking to:

```
turn:    已启动监工任务（binding: sup_...）           # reply looks fine
binding: bot_id='veya-default' owner_id='veya-default'
routine: bot_id='veya-default'
report → conversation 'veya-default', not the user's bot
```

The user's bot conversation only ever held their own message; the report went to
a conversation nobody was reading.

### 2. `_try_supervisor_handoff` looked the binding up by the wrong field

`server/bot_team/routine_clock.py::_try_supervisor_handoff` did
`list_bindings(owner_id=bot_id)` — filtering on `owner_id`, the bot that *started*
the run, not the routine that fired. The authoritative link is `routine_id`
(what the clock fires on and what `supervise_task` stored). Whenever the two bots
differed the lookup returned nothing → `False` → the routine fell through to the
legacy path. This is what the live log showed as `_try_supervisor_handoff → False`.

### 3. The legacy fallback wrote the read-only process cwd

The legacy unscoped path built its request with `project_root="."`. In production
the cwd is `/app`, read-only, so the GoalRun write under `./.veya-project/goal-runs`
raised `FileNotFoundError` / `EACCES`. Even a correctly-routed fallback could
never deliver.

## Changes

- **`server/bot_team/turns.py`** — `_supervise_from_phrase` now binds the turn's
  bot (`bind_current_bot(bot.bot_id)` / `reset_current_bot`) around
  `supervise_task`, so the GoalRun, routine, binding and every report are owned by
  the bot that spoke.
- **`server/bot_team/routine_clock.py::_try_supervisor_handoff`** — match the
  binding on `routine_id` (open/paused), preferring the binding whose `bot_id` is
  the firing bot. No longer filters by `owner_id`.
- **`server/bot_team/routine_clock.py::_legacy_routine_project_root`** (new) — the
  legacy path's `project_root` is now `<VEYA_WRITE_ROOT>/routine/<bot_id>`
  (default `~/.veya/work`), never `"."`.

## Behaviour after the fix

phrase → `supervise_task` (owned by the real bot) → routine fires on schedule →
`_try_supervisor_handoff` finds the binding by routine id → progress report posted
into the **bot's own** conversation with `meta.source="supervisor"`; on stall a
HEARTBEAT nudge; when the verifier passes (all tasks completed) the final report
is posted, the binding is closed and the routine is disabled.

## Tests

New file `tests/test_w12_supervise_handoff_e2e.py` — 5 offline tests
(`tmp_path` + `monkeypatch`, no real model):

1. `test_phrase_binds_real_bot_and_fire_reaches_conversation` — end-to-end: the
   binding is owned by the real bot and the fire posts a progress report into
   that bot's conversation (and nothing into `veya-default`).
2. `test_handoff_matches_by_routine_not_owner` — a binding whose `owner_id`
   differs from the firing bot is still found by routine id.
3. `test_completed_goal_posts_final_report_and_closes` — completed goal posts the
   final report, closes the binding, disables the routine.
4. `test_legacy_routine_never_uses_cwd` — the legacy path's `project_root` is
   under `VEYA_WRITE_ROOT`, not `"."`.
5. `test_legacy_project_root_helper_is_under_write_root`.

All 5 fail on the pre-fix tree and pass after — verified by stashing the source
edits and re-running (5 failed / 5 passed).

### Required suite

```
.venv/bin/python -m pytest -q tests/test_w12_*.py tests/test_w1*.py \
  tests/test_nano_*.py tests/test_lorca_*.py tests/test_orca_*.py \
  tests/test_p3a_bot_isolation.py tests/test_product_shell.py
→ 923 passed
```

(918 pre-existing + 5 new; the 3 known environment-bound
`test_canonical_proxies` failures are outside this selection as agreed.)

## Note on the worktree

`platform/3O/{obase,oprim,oskill,omodul,oservi}` were empty (uninitialized
submodules) in this worktree, which blocked every import. They were symlinked to
the canonical populated copies (`/data/soffy/projects/veya/platform/3O/*`),
matching the pattern already used by the `veya-w13`/`veya-w14`/`veya-orca-w4`
worktrees. No 3O content was modified; the symlinks are submodule paths and do
not appear in `git status`.
