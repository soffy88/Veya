# LORCA_P3_REPORT — P3: readable, editable per-bot Markdown memory for Veya

Worktree `/data/soffy/projects/veya-lorca-p3`, branch `feat/lorca-memory`, built on
`main` (`f59644ea`). This report covers the FINISH-UP items only; the three prior
commits on this branch (store, REST router, first test suite) were made by the
previous agent and are kept unchanged in behaviour:

| commit | content |
|---|---|
| `dc852d2b` | `feat(p3-memory): add BotMemoryStore — per-bot Markdown memory store` |
| `c595e9ec` | `feat(p3-memory): wire bot_memory REST router into server/app.py` |
| `85bc186d` | `test(p3-memory): add tests/test_lorca_p3_memory.py — 49 tests` |

## 1. FINISH-UP item 1 — memory injected into the running bot's semantic context

**What was built.** `server/bot_team/memory.py` gained an additive helper,
`inject_memory_context(semantic_agent, session_id, bot_id)`:

- renders `BotMemoryStore(bot_id).render_for_prompt()` (default 4000 chars) and
  wraps it in a marked system block (`<!-- bot-memory-context bot_id=… -->`);
- injects it into the oservi MasterAgent session history at index 1, exactly the
  shape/hook the host already uses for graft context (`_inject_graft_context`):
  the previous block is removed first, so a re-injection **refreshes** the memory
  the model sees instead of accumulating blocks;
- gated on `VEYA_BOT_MEMORY_MD` (default `"0"` = off). With the flag off it is a
  strict no-op — the history is not even created;
- returns `False` (never raises into the run) when the store is empty, the flag
  is off, or the agent exposes no `_histories`.

**Where it is wired.** `server/goal_run/canonical_worker.py::execute_semantic_task`
calls it right before `MasterAgent.semantic_step(...)` for the running bot
(`state.bot_id`), i.e. on the single existing GoalRun→MasterAgent semantic path —
no second loop, no second context store. Failures are suppressed at the call
site, matching the host's "context assembly must never break the conversation"
convention.

**Files:** `server/bot_team/memory.py`, `server/goal_run/canonical_worker.py`.

## 2. FINISH-UP item 2 — `bot_memory_note(text)` tool

The registry made this straightforward, so the tool was added (not skipped).

- `bot_memory_note(text)` in `server/bot_team/memory.py` appends a timestamped
  note to the **running bot's** `MEMORY.md`, resolved through the new
  `runtime.bot_scope.current_bot()` binding and enforced by
  `require_same_bot(caller_bot_id, bot_id, ...)` — a bot can only write its own
  memory, and an unbound scope raises instead of defaulting to some bot.
- `register_tools(registry)` (idempotent, same contract as the coding/harness
  `register_tools`) registers it into the existing registry;
  `server/tool_registry.py` gains exactly one import and one call
  (`_register_bot_memory_tools(master_tools)`).
- The running-bot binding itself (`bind_current_bot` / `reset_current_bot` /
  `current_bot`) lives in `runtime/bot_scope.py`, the runtime-layer single source
  of truth for bot scope, and is bound/reset in
  `canonical_worker.execute_canonical_action` around the physical execution —
  the same place the cross-bot guard already runs. No scheduler, task store or
  executor was added.

**Files:** `runtime/bot_scope.py`, `server/bot_team/memory.py`,
`server/tool_registry.py`, `server/goal_run/canonical_worker.py`.

## 3. FINISH-UP item 3 — tests

`tests/test_lorca_p3_memory.py` grew from 49 to **63 tests** (14 new):

- `TestSemanticContextInjection` — flag off (explicit and unset/default) is a
  no-op; flag on injects after the system prompt; refresh does not accumulate;
  empty store injects nothing; only the running bot's content is injected;
  render truncation carries into the block.
- `TestBotMemoryNoteTool` — unbound scope raises; note lands in the running
  bot's store; two bots stay isolated; registration is idempotent.
- `TestCanonicalWorkerWiring` — through `execute_semantic_task`: memory injected
  with the flag on, absent with the flag off, and the physical action observes
  `current_bot() == state.bot_id` with the binding reset afterwards.

The flag-off assertions were mutation-checked: forcing
`bot_memory_enabled() → True` makes them fail, so they are load-bearing, not
vacuous.

### Exact test command and result

```
./.venv/bin/python -m pytest -p no:cacheprovider -q \
  tests/test_lorca_p3_memory.py tests/test_p3a_bot_isolation.py \
  tests/test_p3b_bot_delegation.py tests/test_p3c_multi_bot_coordination.py \
  tests/test_product_shell.py
```
→ **3 failed, 98 passed** — the 3 failures are the pre-existing
`test_product_shell.py` ones (`kwargs["semantic_llm_kwargs"]` KeyError: the
product route bakes semantic kwargs into the adapter while the test expects them
forwarded as `project_run_goal` kwargs). They fail identically at the branch base
and are unrelated to memory; no new failures were added.

Additional runs (all green or pre-existing-only):

| command | result |
|---|---|
| `pytest -q tests/goal_run/` | 165 passed, 1 skipped |
| `pytest -q tests/runtime/test_execution_runtime.py` | 13 passed |
| `python -m evals.personal_agent_gold.gate` | PASS |
| `pytest -q tests/server/` | 1 failed (pre-existing), 26 passed |
| `ruff check <touched files>` / `ruff format --check <touched files>` | clean |
| `mypy --config-file pyproject.toml --follow-imports=silent server/coordinator_master.py server/tool_registry.py` | Success |

Pre-existing failures seen while sweeping neighbours (all reproduced/attributed,
none caused by this work):

- `tests/server/test_coding_tools.py::test_coding_tools_register_additively_and_idempotently`
  — `16 == 13`: `runtime/coding/tools.py` registers 16 tools since main commits
  `af3e5f12`/`557ba93f`; the expectation was never updated. A fresh registry
  contains only `coding_*` tools and not `bot_memory_note`, so this change is
  not involved.
- `tests/test_master_tools.py::test_side_effect_pure_read_matches_parallel_safe_whitelist`
  — `coding_git_verify` (main commit `af3e5f12`) is annotated `PURE_READ` but is
  not in `_PARALLEL_SAFE_TOOLS`. `bot_memory_note` is `LOCAL_WRITE`, so it cannot
  enter the annotated set.
- `tests/test_master_tools.py::test_chat_stream_hitl_after_max_rounds` — the
  pinned oservi loop returns `no_progress_detected` where the test expects the
  `a90e91d` `success` summary. Reproduced with `bot_memory_note` unregistered,
  i.e. identical without this change.
- `tests/runtime/test_coding_sandbox_profiles.py::test_command_parser_rejects_shell_escape_and_runner_captures_redacted_artifact`
  — sandbox command policy accepts `echo ok && touch outside`; untouched module.
- `tests/guardians/test_3o_migration_guards.py::test_direct_io_checker_passes_with_baseline`
  — already listed in `tests/baseline_failures.json` as known baseline drift.

### Gates

- `ruff check` / `ruff format --check`: clean on every file this branch touches.
  While verifying, the router include added by `c595e9ec` was found unsorted
  (`ruff I001`); the import was moved to its alphabetical position — still the
  single router include line, no other `app.py` change.
- Direct-I/O gate (`scripts/check_direct_io.py` with baseline): this change adds
  **zero** new findings (before/after diff is line-number drift only, caused by
  the new imports/docstrings shifting the store's 4 pre-existing findings).
  The gate was already failing on `main` with 25 pre-existing findings and is
  recorded in `tests/baseline_failures.json`; the 4 findings from
  `server/bot_team/memory.py` (atomic write mechanics: `tempfile`, `os.fdopen`,
  `os.replace`, `os.unlink`) come from commit `dc852d2b`, not from this work.

## 4. Known gaps / notes

- The tool is registered unconditionally; the prompt-injection side is the part
  behind `VEYA_BOT_MEMORY_MD`. With the flag off a note still lands in the
  bot's readable/editable store (and REST), it just is not auto-injected into
  the model context. Deliberate: the store is the feature, the flag gates the
  prompt surface.
- `inject_memory_context` seeds the session history only when the flag is on. If
  the flag is turned off mid-session, a previously injected block stays in that
  live history until the session ends (it is never refreshed). Fresh sessions
  are unaffected.
- The 3 `test_product_shell.py` failures and the direct-IO baseline drift need a
  human decision (test-expectation update vs. behaviour change; baseline
  promotion) — both are pre-existing and out of scope for this finish-up.
- Nothing was pushed. No changes to `platform/3O`, `.venv`, systemd units, or
  `server/goal_run/models.py`, `server/routes/product.py`; `server/app.py` has
  only the one router include (plus its sorted position).

## 5. Commits added by this finish-up

1. `feat(p3-memory): bind the running bot for bot-scoped capabilities`
   (`runtime/bot_scope.py`)
2. `feat(p3-memory): inject per-bot Markdown memory into semantic context`
   (`server/bot_team/memory.py`, `server/goal_run/canonical_worker.py`)
3. `style(app): sort the bot_memory router import (ruff I001)` (`server/app.py`)
4. `feat(p3-memory): add bot_memory_note tool scoped to the running bot`
   (`server/bot_team/memory.py`, `server/tool_registry.py`)
5. `test(p3-memory): cover memory injection, tool scope, worker wiring`
   (`tests/test_lorca_p3_memory.py`)
6. `docs(p3-memory): add LORCA_P3_REPORT.md`
