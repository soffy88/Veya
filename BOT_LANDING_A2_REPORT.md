# W11 Group A2 Delivery Report: Backend Always-On Behaviours

**Branch**: `feat/bot-landing-always-on`  
**Base**: `canonical main 693ed9f5`  
**Scope**: Scenarios S5 (Memory), S6 (Routine Always-On), S7 (Loop Goal & Checkpoint Cards), S8 (Group Roles & @mention), S9 (Secrets Never Leak), D3 (Flag Unification & Environment).  
**Verification Date**: 2026-10-11  

---

## 1. Executive Summary

Group A2 delivers the backend always-on capabilities required for Veya Bot Landing under the full production always-on flag set (`deploy/bot_landing_flags.env`). All five assigned scenarios (S5–S9) have been implemented, resolved, and verified using both automated test suites (`tests/test_bot_landing_e2e_more.py`) and live executable acceptance harnesses (`scripts/bot_landing_live_more.py`).

All regressions (421+ tests across W10, Lorca, Orca, Isolation, Product Shell, and Origin suites) pass cleanly. In addition, `gates.sh` checks confirm **zero new 3O layering violations** compared to canonical `main`.

---

## 2. Production Flag Set & Environment (Spec D3)

1. **Flag Unification (`server/feature_flags.py`)**:
   - Registered all always-on flags into the shared table `server.feature_flags.py` to establish a single source of truth across the repo.
   - Updated `server/nano_w10/flags.py` to serve as a thin reader over `server.feature_flags.enabled()`, eliminating redundant flag definitions.
2. **Production Flag File (`deploy/bot_landing_flags.env`)**:
   - Generated and committed `deploy/bot_landing_flags.env` containing the full set of production flags (`VEYA_TURN_QUEUE=1`, `VEYA_WAKE_ROUTER=1`, `VEYA_LOOP_MODE=1`, `VEYA_FINISH_CHECKPOINT=1`, `VEYA_LIVE_STREAM=1`, `VEYA_PROMPT_CACHE=1`, `VEYA_CONTEXT_STEWARD=1`, `VEYA_MEMORY_REVIEW=1`, `VEYA_READ_OWN_CHAT=1`, `VEYA_BOT_MEMORY_MD=1`, `VEYA_PERSISTENT_WORKERS=1`, `VEYA_AUTO_REVIEW=1`, `VEYA_GROUP_ROLES=1`, `VEYA_BOT_SECRETS=1`, `VEYA_A11Y_FIRST=1`, `VEYA_DOM_READER=1`, `VEYA_CHANNEL_LADDER=1`, `VEYA_TOOL_ERROR_FRIENDLY=1`, `VEYA_SEARCH_FALLBACK=1`, `VEYA_CHANNEL_STATUS=1`, `VEYA_APPROVAL_GATES=1`).

---

## 3. Scenarios Implemented & Verified

### S5: Memory
- **Behavior**: Remembers user preferences (`LayeredMemoryStore`) via memory review tools. In subsequent conversations, queries match semantic memories. Supports memory review, explicit forgetting, and memory MD storage.
- **Verification**: `test_s5_memory` in `tests/test_bot_landing_e2e_more.py` and `s5(h)` in `scripts/bot_landing_live_more.py`.

### S6: Routine / Always-On
- **Behavior**: Autonomous routine execution via `RoutineClock.tick()`. When triggered, the routine executes via `master_coordinator.execute_structured` and autonomously posts the resulting message to the bot's conversation store (`meta={"source": "routine", "routine_id": ...}`) without requiring a user turn. Supports pausing, deleting, and cache-reloaded scheduling.
- **Verification**: `test_s6_routine_always_on` and `s6(h)`.

### S7: Loop Goal & Checkpoint Cards
- **Behavior**: Multi-round tasks governed by `LoopRunPolicy` emit per-round checkpoint cards (`finish_checkpoint_gate`), pause for user verification, and resolve via the verdict route (`POST /api/v1/nano-w10-b/checkpoints/{id}/verdict`).
- **Verification**: `test_s7_loop_goal_and_checkpoint_cards` and `s7(h)`.

### S8: Group Roles & @Mention Routing
- **Behavior**: Multi-bot group conversations enforce role permissions (Admin vs Member; e.g. notice updates and deletion require Admin). `@mention` syntax routes messages exclusively to the mentioned bot, while `everyone` reply mode broadcasts appropriately.
- **Verification**: `test_s8_group_roles_and_mention` and `s8(h)`.

### S9: Secrets Never Leak
- **Behavior**: Secrets requested via `BotSecretStore.request_secret` emit structured `secret_request_card` in direct chats and are strictly rejected in group chats (`SECRETS_NOT_IN_GROUP`). Secret values are masked across `ConversationStore.append()`, `GroupStore.append_message()`, and `finish_bot_turn` replies.
- **Verification**: `test_s9_secrets_never_leak` and `s9(h)`.

---

## 4. Backend Code Changes

| File | Change Details |
|---|---|
| `server/feature_flags.py` | Registered always-on flags in shared definition table. |
| `server/nano_w10/flags.py` | Refactored `flag_enabled` to delegate directly to `server.feature_flags.enabled`. |
| `server/bot_team/routine_clock.py` | In `_default_handoff_consumer`, automatically append the routine execution result to `conversation_store().append(...)` with metadata. |
| `server/goal_run/routine_registry.py` | Added disk reload in `get()` and `list()` so changes via API / ticks sync across memory caches. |
| `server/bot_team/conversations.py` | Mask secrets in `ConversationStore.append` when `VEYA_BOT_SECRETS` is enabled. |
| `server/bot_team/groups.py` | Mask secrets in `GroupStore.append_message` when `VEYA_BOT_SECRETS` is enabled. |
| `server/bot_team/turns.py` | Mask secrets in `finish_bot_turn` reply before writing to history and returning to client. |
| `server/approval_tools.py` | Made `register_tools` check for `.has()` on target registry before falling back to `master_tools`, preventing crash when `VEYA_APPROVAL_GATES=1`. |
| `server/goal_run/canonical_worker.py` | Regenerate `VerificationSpec` when disk spec `goal_run_id != state.goal_id` rather than aborting with collision error. |

---

## 5. New Model-Callable Tools

- **None added**: Group A2 did not introduce any new model-callable tool.
- **Existing tool fixed**: `request_handoff` (from W6 approvals) registration was made defensive to avoid `AttributeError` when `VEYA_APPROVAL_GATES=1`.

---

## 6. Test Output & Verification Evidence (Real Runs Only)

### 6.1 Group A2 Automated E2E Suite
Command: `.venv/bin/python -m pytest -v tests/test_bot_landing_e2e_more.py`
```
tests/test_bot_landing_e2e_more.py::test_s5_memory PASSED                [ 20%]
tests/test_bot_landing_e2e_more.py::test_s6_routine_always_on PASSED     [ 40%]
tests/test_bot_landing_e2e_more.py::test_s7_loop_goal_and_checkpoint_cards PASSED [ 60%]
tests/test_bot_landing_e2e_more.py::test_s8_group_roles_and_mention PASSED [ 80%]
tests/test_bot_landing_e2e_more.py::test_s9_secrets_never_leak PASSED    [100%]
======================== 5 passed, 6 warnings in 41.24s ========================
```

### 6.2 Group A2 Live Acceptance Script
Command: `.venv/bin/python scripts/bot_landing_live_more.py`
```
============================================================
Starting Bot Landing Live Verification (Group A2: S5..S9)
Target: http://127.0.0.1:8767 | User prefix: e2e_tester_a2
============================================================
Logged in successfully (embedded TestClient).
  -> Testing S5: Memory...
  [PASS] S5 Memory verified.
  -> Testing S6: Routine always-on...
  [PASS] S6 Routine verified.
  -> Testing S7: Loop goal & Checkpoint cards...
  [PASS] S7 Loop goal & Checkpoint cards verified.
  -> Testing S8: Group roles & @mention...
  [PASS] S8 Group roles & @mention verified.
  -> Testing S9: Secrets never leak...
  [PASS] S9 Secrets never leak verified.
============================================================
ALL A2 SCENARIOS (S5..S9) PASSED!
```

### 6.3 Regression Suites
- **W10 Nano Suites** (`tests/test_nano_w10_a.py` through `d.py`):
  `233 passed in 125.94s`
- **Lorca, Orca, Isolation & Product Shell** (`test_lorca_p3_memory.py`, `test_lorca_p4_routines.py`, `test_orca_w4_routines_artifacts.py`, `test_p3a_bot_isolation.py`, `test_product_shell.py`):
  `133 passed in 49.63s`
- **Origin Tests** (`test_sse_envelope.py`, `test_stop_reentry_race.py`, `test_sf002_sandbox_boundary.py`):
  `55 passed in 6.42s`

### 6.4 3O Layering & Gates Verification
Checked via `~/lorca-jobs/gates.sh` compared against canonical `main` (`/data/soffy/projects/veya`):
- `check_3o_gates.txt`: 53 violations (vs 60 on canonical, -7 violations, **0 new**)
- `check_no_direct_io.txt`: 267 violations (vs 273 on canonical, -6 violations, **0 new**)
- `check_no_reverse_dep.txt`: 4 violations (vs 4 on canonical, **0 new**)
- `check_oskill_pure.txt`: 0 violations (vs 0 on canonical, **0 new**)
- **Total**: 324 violations vs 337 on canonical `main` (**zero new violations**).

---

## 7. Known Gaps & Boundaries

- **D1 (Plain chat verification)**: As specified in `W11_GROUP_A2.md`, D1 is handled by Group A1. We did not modify verification engine rules or add bypass flags; goal collision handling was made resilient in `canonical_worker.py`.
- **S1–S4, S10–S12, D2, and Web**: Out of scope for Group A2, owned by Group A1 / other tracks.
