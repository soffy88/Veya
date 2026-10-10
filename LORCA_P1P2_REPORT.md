# LORCA P1 + P2 report — bot team + group chats

Branch: `feat/lorca-team-groupchat` (base `main` f59644ea). Work only in this
worktree; nothing was read or imported from `/data/soffy/projects/veya`, and no
code was copied from `egoist/lorca`. Commits: `33ee09d4`, `56a69ff8`, `6bb752c1`,
`ffdb6cd9`.

## What was built

### P1 — multiple named bots + Chef

**1. BotSpec (backward compatible)** — `server/goal_run/bot_identity.py`
Optional fields `name`, `description` (the one job), `instructions`, `provider`,
`model`, `runner_id` (default `"local"`). `from_dict` reads every new key with a
default, so a pre-P1 record loads as the same bot; `to_dict` just carries the
extra keys. No migration, no second schema. `BotRegistry.unregister()` added
(needed by `DELETE /api/v1/bots/{bot_id}`).

**2. bot_id threaded end to end** — `server/routes/product.py` →
`server/coordinator_master.py` → `server/goal_run/runner.py` →
`server/goal_run/planner.py`
`_run_product_task(..., bot_id=None)` puts `bot_id` into the
`CanonicalExecutionRequest` metadata; `MasterCoordinator.execute_structured`
forwards it (and `semantic_llm_kwargs`, which this path was dropping) into
`project_run_goal`; `g1_plan` stamps it on the `GoalRunState` it creates.
`None`/empty keeps the historical `veya-default`, so every old caller is
unchanged. A bot's `provider`/`model` travel through the existing
`semantic_llm_kwargs`; its `instructions` + one-job description are prepended to
the turn objective (`server/bot_team/turns.py::build_objective`), because
`task.instruction` is what MasterAgent receives as the prompt for that GoalRun.
`_run_product_task` now also returns the canonical response (ignored by
fire-and-forget callers) so the bot-team surface can record what was answered.

**3. Per-bot conversation store** — `server/bot_team/conversations.py`
One JSON log per bot at `~/.veya/bots/<bot_id>/messages.json`
(`VEYA_BOTS_ROOT` overrides), atomic writes (tmp + fsync + `os.replace`),
messages `{id, role user|bot, bot_id, text, task_id, ts, hidden}`. A reply equal
to the PASS sentinel `[PASS]` is stored with `hidden=true` and is not returned by
the messages endpoint. Bot ids are restricted to a safe alphabet because they
become directory names.

**4. Bot API** — `server/routes/bots.py`, mounted in `server/app.py`
`/api/v1/bots`: `GET /` (list, seeds Chef), `POST /` (create + register),
`GET/PATCH/DELETE /{bot_id}`, `GET /{bot_id}/messages`,
`POST /{bot_id}/messages` (records the user message, runs a product task as that
bot, records the reply), `POST /{bot_id}/message_bot`. Auth is
`Depends(auth_mod.get_current_user)`, the same as `product.py`. The default bot
is synthesised in memory when it is not in the catalog (no record is invented),
it cannot be deleted, and `/api/v1/bot` keeps working exactly as before.
`veya/server/app.py` reuses the same `server.app` instance, so both entry
surfaces expose the new endpoints.

**5. Chef** — `server/bot_team/chef.py`
Seeded on the first `GET /api/v1/bots` if missing (`bot_id="chef"`, idempotent).
Its instructions tell it to ask what the user works on, propose a small team of
one-job bots, and never create anything itself. A proposal is one fenced block:

````
```veya-bots
{"bots": [{"bot_id": "research", "name": "Researcher", "description": "Finds sources", "instructions": "..."}]}
```
````

`extract_proposal` only honours that shape (a prose mention of a bot is not a
proposal; a proposed bot with no job is dropped). A proposal is stored when Chef
emits it; `POST /api/v1/bots/proposals/{id}/accept` is what creates the bots,
owned by the accepting user. A second accept is refused (409). Chef has no
privileges beyond this scripted behaviour.

### P2 — bots message each other + group chats

**1. message_bot** — `server/bot_team/messaging.py`
`BotMessenger.message_bot(source, target, text)` drives the existing
`DelegateRuntime.run_cross_bot` under a `SpawnGuard` with
`SpawnBudget(max_depth=3, max_tokens=300_000)` (fresh per call, so the token
budget is per turn). Refused, never repaired: self-target, unknown source or
target bot, and `depth >= MAX_HOP_DEPTH` (the guard enforces the same limit on
`request.depth`, so the boundary is exact). The target runs a normal turn
(its own product task), the result is returned to the caller and recorded in
both conversations. Exposed as a service + `POST /api/v1/bots/{bot_id}/message_bot`
— deliberately **not** as a model-visible tool, see "Known gaps".

**2. Group chats** — `server/bot_team/groups.py` + routes in `bots.py`
`POST /api/v1/bots/groups {name, bot_ids (1..6), rounds_per_message=1}`,
`GET /api/v1/bots/groups`, `GET /groups/{id}/messages`,
`POST /groups/{id}/messages`. On each user message every member is offered one
turn per round, in the order it was added, with the group transcript as context.
A `[PASS]` reply is stored hidden (in the group transcript and in the bot's own
conversation) and is not returned by the messages endpoint; the round stops early
once every member that is still in the catalog has passed. A member that was
deleted is reported as skipped instead of failing the round. 7 members → 422.

## Files

New: `server/bot_team/__init__.py`, `conversations.py`, `turns.py`, `chef.py`,
`messaging.py`, `groups.py`, `server/routes/bots.py`,
`tests/test_lorca_p1_bots.py`, `tests/test_lorca_p2_groups.py`.

Changed: `server/goal_run/bot_identity.py`, `server/goal_run/planner.py`,
`server/goal_run/runner.py`, `server/coordinator_master.py`,
`server/routes/product.py`, `server/app.py`.

No `server/bot_runtime/`, no `platform/3O` change, no `.venv` or
`LORCA_*_SPEC.md` in any commit. No second scheduler, task store or executor.

## Tests

Exact command (this is what was run, with the new files included):

```
./.venv/bin/python -m pytest tests/test_p3a_bot_isolation.py \
  tests/test_p3b_bot_delegation.py tests/test_p3c_multi_bot_coordination.py \
  tests/test_product_shell.py tests/test_lorca_p1_bots.py \
  tests/test_lorca_p2_groups.py -q -p no:cacheprovider
```

Result:

```
2 failed, 64 passed, 5 warnings in 26.80s
```

Baseline before this work was `3 failed, 35 passed`. The failures:

| test | status |
|---|---|
| `test_product_shell.py::test_product_task_runner_binds_existing_goalrun_semantic_loop` | **was failing, now passes** — `execute_structured` dropped `semantic_llm_kwargs` on the PRODUCT path; it is now forwarded into `project_run_goal`. |
| `test_product_shell.py::test_resume_keeps_the_original_product_task_projection` | pre-existing failure, untouched: `server/routes/tasks.py::resume_task` no longer forwards `max_rounds` to `chat_stream`. Unrelated to Lorca; not fixed (out of scope). |
| `test_product_shell.py::test_product_task_resume_reuses_persisted_provider_binding` | same root cause as above. |

New coverage (17 + 11): old BotSpec records; create/list/patch/delete; default
bot unchanged, not deletable, patchable; `bot_id` reaching `GoalRunState`
(planner → runner → canonical request) and a plain product task still sending no
`bot_id`; Chef seeded once; proposal accept creating bots and refusing a second
accept; `message_bot` self-target / unknown bot / hop limit (with the positive
case one hop below the limit) and both conversations being recorded; group turn
order with the transcript as context; PASS hidden + early stop; mixed pass/speak
rounds; max 6 members; rounds persistence; 404s. The LLM is faked at
`master_coordinator.execute_structured`; GoalRun, DelegateRuntime and SpawnGuard
are real.

Also run: `tests/server/`, `tests/goal_run/`, `tests/test_goal_run_boundary.py`,
`tests/test_a1_f_gates.py`, `tests/test_cindy_compat.py`,
`tests/server/test_server_boot_gate.py`. Only two pre-existing problems appear,
neither caused by this branch:

- `tests/server/test_coding_tools.py::test_coding_tools_register_additively_and_idempotently`
  expects 13 coding tools; `runtime/coding/tools.py` registers 16 (drift from
  commits `557ba93f` and `af3e5f12`).
- `tests/goal_run/test_daemon_durable_closure.py::test_daemon_process_kill_resume_preserves_goalrun_owner`
  is a subprocess-kill test that flakes when run with other files (passes alone).

Lint/format/typecheck:

```
./.venv/bin/python -m ruff check server/bot_team server/routes/bots.py \
  server/routes/product.py server/goal_run/bot_identity.py server/goal_run/planner.py \
  server/goal_run/runner.py server/coordinator_master.py server/app.py tests/test_lorca_p1_bots.py tests/test_lorca_p2_groups.py   # All checks passed
./.venv/bin/python -m ruff format --check <same paths>   # all formatted
./.venv/bin/python -m mypy --config-file pyproject.toml --follow-imports=silent server/coordinator_master.py server/tool_registry.py   # Success
```

`ruff check .` (whole repo) reports 16 errors in files this branch does not
touch (`runtime/execution/checkpoint.py`, `server/knowledge_binding.py`,
`server/skill_eligibility.py`, `scripts/gc_remote_worktrees.py`, `tests/remote/*`,
`veya/remote/*`) — pre-existing.

## Known gaps / decisions

1. **`message_bot` is not a model-visible tool.** The spec allows "service + REST
   POST" when the tool registry is not the straightforward route. Registering it
   in `MasterToolRegistry` would add it to the mainline tool surface, which
   `AGENTS.md` / `docs/ARCHITECTURE_STABLE.md` freeze (tool-surface changes need
   explicit user approval). Wiring it in is ~10 lines once approved.
2. **Bot turns are executed inline.** `POST /{bot_id}/messages` and
   `POST /groups/{id}/messages` return when the round finished, so a long turn
   holds the request open. The response carries `task_id` / `session_id` and a
   `workbench_url`, and the durable record is the existing product Task. A
   streaming/202-Accepted variant was not added.
3. **Delegate routing refs.** `DelegateRequest.parent_trace_id` and
   `target_goal_run_id` are refs derived from the delegate id, because the
   target's real GoalRun id only exists after the product path starts. The real
   task id is carried back in the delegate result (evidence ref `task:<id>`) and
   recorded in the conversations.
4. **Two pre-existing `test_product_shell.py` failures** (resume path drops
   `max_rounds`) are left alone; they are outside Lorca's scope and would need
   `server/routes/tasks.py` changes.
5. **Chef is a normal bot, not an agent runtime.** It only gets one scripted
   behaviour (propose a team in a fenced block) and it is the only bot whose
   replies are scanned for a proposal.
6. **No frontend.** `apps/web` is untouched; the endpoints are the contract.
7. **Not pushed** (no `git push`, per the spec).
