# NANO_W6_REPORT — Tiered approvals, @runner routing, approval relay, pairing, delivery states

Branch `feat/nano-approvals-devices` (base main `fcad8ad4`). Clean-room: no nanoMuse/Lorca code copied; built from the spec text only.
Implementation by OpenCode agents (nemotron / longcat); finished, debugged and verified by the supervisor (Hark) on 2026-10-11.

## What was built
1. **Approval gates** (`VEYA_APPROVAL_GATES`, default OFF): destructive/outbound/payment-like tool calls pause in `server/user_control._wait_approval` → durable approval request + push relay → resume on `once` / `session` / `always` / `deny`. "Always" grants persist per user+tool+scope (SQLite, `veya/obase/approvals.py`); list + revoke via REST. Human handoff (`request_handoff`) with Done/Cancel resume. Flag OFF = legacy session approval path unchanged.
2. **Approval relay**: pending approval fires a Web Push (P5) with deep link `/approvals/<id>`; answering resumes the run (DB-polled wait loop + in-process wake event, so a different process can answer).
3. **@runner routing**: `@<runner> ...` at the start of a bot message forwards the turn to that P6 runner; unknown name → visible error listing runners.
4. **UI**: `/approvals` inbox (pending approvals with the four answers + "always" grants with revoke), `/approvals/[id]` push landing page, inline chat approval card gains 本会话 / 总是 (tries the gated endpoint, falls back to legacy on 404).
5. **Addendum J — pairing codes** (`VEYA_DEVICE_PAIRING`, default OFF): device requests short code (TTL, single use, rate-limited); signed-in user confirms; credential issued + runner registered. Codes/credentials never logged.
6. **Addendum K — delivery states**: agent mailbox messages carry `delivered` / `started` / `unconfirmed`; derivation is a pure oskill; `list_for_recipient` attaches state. No new store.
7. **Addendum L — guard tests**: agent principals cannot resolve approvals unless the approval's policy allows a delegated agent; high-risk classes human-only; double resolve is a conflict.

## Supervisor fixes (on top of the agent's work)
- `oskill.request_permission_decision` ignored in-memory session grants (only durable grants could allow) → "allow for this session" never took effect and the run waited forever; fixed. The hanging test also picked the wrong row (`list_approvals` is newest-first incl. resolved) → now selects the pending one with a timeout.
- `AgentMessage.to_dict` crashed on plain-string delivery mode.
- Pairing confirm returned 422 instead of 404 while the flag is off (body validated before flag check).
- **Security**: `/approvals/{id}/resolve` and `/handoff/{id}/done|cancel` did not check ownership — any account could answer another account's approval. Now 404 unless the approval/handoff belongs to the caller's scope; test added.
- `/team/bot/[bot_id]` and `/team/group/[group_id]` had no `+page.ts`, so `data.botId`/`data.groupId` were undefined (pre-existing P5 bug, also on main) — added.
- Repo-level (committed on main, not this branch): `platform/3O/*` had been committed as symlinks (01e61218/fcad8ad4), leaving canonical `oskill`/`oservi` as self-referencing links and breaking 3O imports; gitlinks restored in `d4270ba4` and submodules re-checked-out.

## New model-callable tools
- `request_handoff(reason)` — registered only when `VEYA_APPROVAL_GATES` is on; side effect LOCAL_WRITE, capability `manual_only`.

## Tests
- `tests/test_nano_w6_approvals.py` 26, `tests/test_nano_w6_pairing.py`, `tests/test_nano_w6_delivery_states.py` → **43 passed**.
- Regression: lorca p1/p2, p3a isolation, product_shell, all Lorca P3–P6 + msgbot, all Orca W1–W5 → **162 passed** (after the 3O submodule repair; previously the 2 product_shell resume failures were from the broken 3O mount).
- Web: `pnpm run check` 0 errors (31 pre-existing warnings), `pnpm run build` OK.
- 3O gates: `check_3o_gates.py --strict`, `check_no_reverse_dep.py`, `check_no_direct_io.py`, `check_oskill_pure.py` — **zero new violations** vs main (W6 totals ≤ main: gates 53 vs 60 lines, direct-io 276 vs 282, reverse-dep 8 vs 8, oskill-pure 0). `tests/guardians/test_3o_migration_guards.py` 9 pass / 1 fail, same failure on main (`test_direct_io_checker_passes_with_baseline`, pre-existing).

## Element → layer → why
| Element | Layer | Why |
|---|---|---|
| `veya/oskill/pure/permission.py` `evaluate_capability_permission`, `scope_match` | oskill (pure) | tool+args+grants → allow/ask/deny; no IO |
| `veya/oskill/pure/mention.py` `parse_runner_mention` | oskill (pure) | text parsing only |
| `veya/oskill/pure/delivery_state.py` | oskill (pure) | state derived from message + events |
| `veya/oskill/approval.py` `request_permission_decision` | oskill | composes oprim grant atoms with pure evaluation; store injected |
| `veya/oprim/approval.py` approval/grant/handoff atoms | oprim | one atomic op each over an injected store, keyword-only |
| `veya/oprim/pairing.py` code create/consume | oprim | atomic ops |
| `veya/obase/approvals.py` `ApprovalStore` | obase | SQLite persistence adapter |
| `veya/obase/pairing.py` | obase | pairing persistence + credential hashing |
| `veya/omodul/approval_flow.py` | omodul | request/resolve/handoff flow, never raises, `decision_trail` pillar |
| `veya/omodul/pairing_flow.py` | omodul | request/confirm flow, `decision_trail` |
| `server/approvals.py`, `server/pairing.py`, `server/approval_tools.py` | service layer | thin REST/tool wiring, scope pseudonymized, ownership checks |
| `server/user_control.py` gated wait, `server/bot_team/turns.py` @runner | service layer | GoalRun pause/resume and turn routing glue |
| `apps/web` approvals pages, chat card | UI | presentation only |

## Known gaps / honest notes
- Approval requests and grants live in a W6 SQLite store (`ApprovalStore`) beside SideEffectLedger; the gated wait is not yet driven through `ActionGateway` end-to-end (the decision trail is in the omodul). Spec asked to persist through SideEffectLedger/ActionGateway — folding the request record into the ledger is the follow-up before turning the flag on in production.
- REST resolve trusts the caller as a human (signed-in user); agent principals are enforced in-process (guard L). Auth still falls back to anonymous repo-wide, so ownership = scope of that fallback when unauthenticated.
- Daily per-user routine wiring for pairing cleanup is not added (expired codes are rejected on read).
