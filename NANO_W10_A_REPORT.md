# NANO-W10 group A — report

Scope (`W10_GROUP_A.md`): spec **item 1** (turn queue: priority + coalescing +
steer + idempotency), **item 2** (wake router + "worth bugging the user?"
policy), addendum **W3** (Goal vs Loop as a GoalRun policy), **W4** (live
tool-result stream + plan list + in-run correction box), **W5** (finish
checkpoint card). Branch `feat/nano-always-on`. Design first:
`docs/NANO_W10_A_DESIGN.md`. Clean-room: see `THIRD_PARTY_NOTICES.md`.

---

## 1. Item status

| # | Item | State | What exists |
|---|------|-------|-------------|
| 1 | Turn queue (priority / coalescing / steer / idempotency) | **done** | `veya/oprim/turn_envelope.py`, `veya/oskill/pure/turn_priority.py`, `veya/omodul/turn_intake.py`, `server/nano_w10/turn_queue.py`, intake wired into `POST /api/v1/bots/{bot_id}/messages` |
| 2 | Wake router + worth-bugging policy | **done** | `veya/oskill/pure/wake_policy.py`, `veya/omodul/wake_router.py`, `server/nano_w10/wake_router.py`; delivery bound to the existing P5 push / W7 channels; the routine clock routes its result through the intake |
| W3 | Goal vs Loop as a GoalRun policy | **done** | `veya/oskill/pure/loop_policy.py`, `veya/omodul/loop_run.py`, `server/nano_w10/loop_mode.py`; consulted at the runner's existing `reason="task_round"` seam; a stop verdict starts the run's **own** finalization |
| W4 | Live tool-result stream + plan list + correction box | **done** | `veya/oskill/pure/live_trace.py`, `veya/omodul/live_trace.py`, `server/nano_w10/live_stream.py`; `POST /api/v1/bots/{bot_id}/corrections` (auth + ownership) landing through item 1's steer; `WorkProcess.svelte` renders `tool_result`; the plan list is the existing `PlanBoard`/`PlanCard` fed by `plan_update`, with the pure `plan_rows` projection folding statuses into done/doing/todo |
| W5 | Finish checkpoint card (post + accept / send back) | **done** | `FinishCheckpointGate` in `server/nano_w10/loop_mode.py`; card posted from `server/goal_run/runner.py` before the run marks itself done; `POST /api/v1/bots/{bot_id}/checkpoints/{card_id}`; card rendered with 接受 / 退回 buttons in the bot chat |
| — | Everything else in `NANO_W10_SPEC.md` (items 3–7, addenda M–V, W1/W2) | **not this group** | Other workers, in parallel. No group-A code touches their files. |

### Flags (all default OFF except the two the spec allows on)

| Flag | Default | Guards |
|------|---------|--------|
| `VEYA_TURN_QUEUE` | off | item 1 queue + the steer the correction box uses |
| `VEYA_WAKE_ROUTER` | off | item 2 intake and the routine-clock routing |
| `VEYA_LOOP_MODE` | off | W3 loop policy + its per-round checkpoint card |
| `VEYA_FINISH_CHECKPOINT` | off | W5 finish card + its verdict route |
| `VEYA_LIVE_STREAM` | **on** | W4 live stream (spec: "live stream UI may be on"); with it off every event projects to no step and a correction returns `FEATURE_DISABLED` |

(`VEYA_TOOL_ERROR_FRIENDLY`, `VEYA_LLM_TAP`, `VEYA_SEARCH_FALLBACK`,
`VEYA_CHANNEL_STATUS` are declared in the shared flag table for the other
groups' read-only surfaces; group A implements none of them.)

---

## 2. Tests actually run (this session, verbatim results)

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q tests/test_nano_w10_a.py` | **68 passed**, 6 warnings (1m35s) |
| `.venv/bin/python -m pytest -q tests/test_lorca_p1_bots.py tests/test_lorca_p2_groups.py` | **28 passed** |
| `.venv/bin/python -m pytest -q tests/test_p3a_bot_isolation.py` | **19 passed** |
| `.venv/bin/python -m pytest -q tests/test_product_shell.py` | **13 passed** |
| `.venv/bin/python -m pytest -q tests/goal_run` | **165 passed, 1 skipped** (the runner is wired by W3/W5) |
| `.venv/bin/python -m pytest -q tests/guardians/test_3o_migration_guards.py` | **9 passed, 1 failed** — see §4 |
| `cd apps/web && NODE_OPTIONS=--max-old-space-size=2048 pnpm run check` | **0 errors**, 32 pre-existing warnings in 3 files (none in the files this group touched) |
| `cd apps/web && NODE_OPTIONS=--max-old-space-size=2048 pnpm run build` | **built**, exit 0 |
| `.venv/bin/ruff check` on every file this group wrote | **All checks passed** |
| `.venv/bin/ruff format --check` on every file this group wrote | clean (except `server/bot_team/routine_clock.py`, which is unformatted repo-wide — see §5) |

One flake worth naming: `tests/goal_run/test_daemon_durable_closure.py::
test_daemon_process_kill_resume_preserves_goalrun_owner` failed **once** when
the whole `tests/goal_run` directory ran before it (15 s subprocess timeout,
the test normally takes 14.7–15.5 s), then passed on its own and on a full
re-run of the directory. It is timing, not this work.

### The one failing guardian test is pre-existing

`test_direct_io_checker_passes_with_baseline` fails because
`DIRECT_IO_NEW_VIOLATIONS=60`. Measured against the pre-session tree
(`89432a47`, this group's checkpoint commit) by checking the pre-session
copies of every file this group touched back out and running the same checks:

| Check | at `89432a47` (before this session) | now | delta |
|---|---|---|---|
| `check_no_direct_io.py --baseline …` | 共 60 处新增直接 I/O（存量 288 条在基线内） | **共 60 处新增直接 I/O（存量 288 条在基线内）** | **0** |
| `check_no_reverse_dep.py --baseline …` | `[OK] 149 个 3O 层文件全部满足单向依赖` | `[OK] 149 …` | **0** |
| `check_oskill_pure.py` | rc 0 | rc 0 | 0 |
| `check_3o_gates.py --strict` | oprim peer calls 0, omodul direct calls 0, direct-memory writes 0 | same | 0 |

Not one of the 60 points at a line this group wrote: the only hit in a touched
file is `server/bot_team/conversations.py:55/59` (`open` / `os.replace` inside
the pre-existing `atomic_write_json`, untouched — my change is below it).

---

## 3. New model-callable tools

**None.** Group A adds no tool to the registry and no tool the model can call.
Item 1, W3, W4 and W5 all reuse surfaces that already existed (messages,
steer, the SSE step stream). `POST /api/v1/bots/{bot_id}/corrections` and
`POST /api/v1/bots/{bot_id}/checkpoints/{card_id}` are **user** endpoints, not
model tools. The spec's tool-additions (memory_search, read_own_chat,
remember/forget/recall, request_secret, list_secrets, read_browser) all belong
to the other groups.

---

## 4. Element → 3O layer → why

| Element | Layer | Why this layer |
|---|---|---|
| `idem_key`, `envelope_fingerprint` | **oprim** | One atomic digest each; they call `obase.canonical_json`/`sha256_hash` directly — an oprim→oprim call is a gate violation. |
| `turn_priority`: `classify_inbound`, `envelope_priority`, `order_queue`, `steer_decision`, `coalesce_window` | **oskill (pure)** | Ordering / coalescing / steer rules over envelopes. Deterministic, no IO, no clock, no `user_id`. |
| `wake_policy`: `worth_bugging_user`, `classify_wake`, `is_quiet_hours` | **oskill (pure)** | The reply-now / digest / silent / emoji-ack decision is a predicate over signal + settings; the caller supplies "now". |
| `loop_policy`: `parse_loop_brief`, `loop_decision`, `checkpoint_card` | **oskill (pure)** | Budget/stop arithmetic and card rendering. Reuses the caller's existing no-progress verdict instead of re-implementing `NoProgressGuard`/`ProgressTracker`. |
| `live_trace`: `step_from_tool_event`, `plan_rows`, `correction_action` | **oskill (pure)** | Projections over already-emitted stream events; `correction_action` deliberately reuses item 1's steer vocabulary. |
| `turn_intake` | **omodul** | One business transaction (envelope → action) behind the `(config, input_data, output_dir) → {status,error}` contract. Pillar: `decision_trail`. |
| `wake_router` | **omodul** | Dedupe + classify + deliver-instruction is a transaction. Pillars: `fingerprint`, `decision_trail`. |
| `loop_run` | **omodul** | GoalRun *policy* transaction; never raises. Pillar: `decision_trail`. |
| `live_trace` (omodul) | **omodul** | Projects the existing `fire_step`/`on_step` events and lands a correction. No second stream, no second event store. |
| `TurnQueue`, `TurnQueue.steer` | **layer4** `server/nano_w10/turn_queue.py` | In-process envelope bookkeeping in front of the existing turn path — the same trade-off `async_turns._pending` already documents. Not a store. |
| `LoopRunPolicy`, `FinishCheckpointGate`, `finish_card`, `card_key` | **layer4** `server/nano_w10/loop_mode.py` | They read the existing GoalRun projection and post through the existing conversation / event / turn-queue paths. All collaborators injected. |
| `WakeRouter`, `default_wake_router`, `build_triage_engine` | **layer4** `server/nano_w10/wake_router.py` | Assembles the **existing** `oservi.triage(on_signal)` skeleton through a `ServiceManifest`; delivery is the existing P5/W7 functions. No new engine. |
| `LiveStream`, `wire_to_turn_queue` | **layer4** `server/nano_w10/live_stream.py` | Binds the omodul to the existing `on_step` callback and to item 1's queue. Owns no connection. |
| `_loop_policy_for`, `_post_finish_checkpoint` | **layer4** hooks in `server/goal_run/runner.py` | Layer-4 assembly into the existing runner at its existing seams. Both are no-ops unless their flag is on, so a Goal run is unchanged. |
| `POST /{bot_id}/corrections`, `POST /{bot_id}/checkpoints/{card_id}`, turn-queue intake in `POST /{bot_id}/messages` | **layer4 (route)** | Thin. `require_user` (401 anonymous) + `_require_bot(..., owner_id=user_id)` (404 someone else's bot); the checkpoint route additionally requires the card to be in **that** bot's conversation, so a guessed id is a 404. |

**Frozen decisions kept** (3O_RECONCILIATION OBSOLETE list + 🧊 冻结架构):
no second scheduler (the queue is in front of the existing turn path; schedules
stay on the routine clock), no second task store (state goes on the existing
`GoalRunState`/`budget` projection and the existing conversation log), no second
approval system (a verdict is an event; a send-back is a steer), no second event
store (`server/events.py`), no second side-effect system, no second capability
registry, no change to `MasterAgent`, no task-semantic `ModelRouter` selection.

---

## 5. Gaps, deviations, and things I deliberately did not do

1. **W5 is gated behind its own flag.** The spec's addendum names flags for W1
   and W3 and says the live stream UI may be on; it names none for W5. HARD
   RULES say new features default OFF, so the finish card and its verdict route
   are behind `VEYA_FINISH_CHECKPOINT` (off). Consequence: with the flag off,
   the card is not posted at all — the feature is off, not half-on.
2. **Digest collection into the rendered morning brief is a seam, not a
   build.** A `digest` wake is written to the existing canonical event store
   (`wake.digest`); turning that into a section of the W7 morning brief means
   editing `server/bot_team/morning_feed.py`, which belongs to the channels
   group. Per `W10_GROUP_A.md` that is left as a clearly named interface rather
   than edited from here.
3. **Per-owner notification settings do not exist yet.** `default_wake_router`
   passes an empty settings mapping, which is the fail-quiet case the policy is
   built for (background stays silent, a waiting user is never silenced). When
   W1/other groups add real settings, only `_owner_settings()` changes.
4. **The bot chat is poll-based, not streamed.** `ChatTab` polls
   `GET /messages`; the live step stream (`WorkProcess`) lives in the main
   workbench chat. So the finish card reaches the bot chat as a conversation
   message with `meta.kind = "finish_checkpoint"`, and the card's buttons render
   there; the tool-result stream and plan list render in the workbench. Adding
   an SSE channel to the bot chat would be a transport change this group was not
   asked for.
5. **`GoalRunResponse` and the runner's return shape are unchanged.** A stopped
   loop reports through the existing `summary` + a `goal_run.loop_round` runtime
   event + `state.last_stop_reason = "loop:<reason_code>"`; no new field was
   added to the durable projection.
6. **Loop "progress" is derived from the run's own state**: consecutive rounds
   in which `len(state.completed_ids)` did not grow. This reuses the existing
   progress signals rather than adding a second no-progress detector.
7. **In-process dedupe for cards** (one post per run/round per process), the
   same trade-off `TurnQueue` and `WakeRouter` already document: a restart
   forgets it. The durable record is the existing conversation message + event.
8. **`server/bot_team/routine_clock.py` is unformatted repo-wide** (ruff would
   rewrite the whole file, including `if expr == '*': return True`). I left its
   existing style alone and only added a block in that style. It has 3
   pre-existing ruff errors (import order, unused `RoutineSpec` import, E701) —
   identical before and after this work, confirmed by checking the pre-session
   copy of the file out and running ruff on it.
9. **Mid-session incident, recorded for honesty:** I used `git stash` once to
   compare lint baselines. In this worktree that emptied
   `platform/3O/{oprim,oskill,omodul,obase,oservi}`. I restored them with
   `git submodule update --init`, which checked each one out at its **pinned**
   commit (`b7d5593d`, `0f431108`, `dd132e2`, `b7f19d9`, `f05d673`) —
   `git diff HEAD -- platform/3O` is empty. A second `git checkout` during the
   baseline comparison reverted two of my uncommitted files; they were
   re-applied and re-tested (68 passed). No user work was lost, and
   `platform/3O` was not modified.

---

## 6. Commits (this group, on `feat/nano-always-on`)

| Commit | What |
|---|---|
| `b4b6d192` | design doc + item 1 turn queue (3O elements + layer-4 queue) |
| `6737d5d9` | item 2 wake router + worth-bugging policy |
| `d8a0b97d` | W4 live tool-result stream, plan rows, correction box (3O + layer4 + WorkProcess) |
| `a9e5593d` | W4 correction endpoint (auth + ownership) landing via the item-1 steer |
| `8cca6011` | W3 Goal vs Loop policy + W5 finish checkpoint card (elements) |
| `047eb9f6` | W4 correction box in the bot chat |
| `89432a47` | checkpoint after the host reboot (previous round) |
| `f1ec6032` | W5 gate + verdict route + W3 wired into the GoalRun rounds |
| `c3cc877f` | item-2 delivery binding + the routine clock on the one wake intake |
| `ce2efda1` | design doc matches what shipped |
| `898b514d` | loop round card no longer collides with the run's finish card |

Every commit used `git add -A -- . ':!.venv' ':!platform/3O' ':!apps/web/node_modules'`.
No push, no service restart, no version bump.