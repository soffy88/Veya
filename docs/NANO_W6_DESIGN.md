# NANO_W6_DESIGN — Tiered approvals + cross-device @runner routing with approval relay

Spec: `NANO_W6_SPEC.md`. Branch `feat/nano-approvals-devices`.
Test files: `tests/test_nano_w6_approvals.py`, `tests/test_nano_w6_pairing.py`,
`tests/test_nano_w6_delivery_states.py`.

## Goals

1. **Approval gates** — before destructive/outbound/payment-like tool calls, GoalRun
   pauses with an approval request. Answers: allow once / allow for this session /
   always allow (per user+tool+scope pattern) / deny. "Always" grants persist per
   user in SQLite (existing store conventions); list + revoke via REST. Human
   handoff: a tool may request "user must do this step" (login/captcha/code) →
   paused state with Done/Cancel resume.
2. **Approval relay** — pending approvals fire a Web Push (P5) to the user's
   subscribed devices; the push opens `/approvals/<id>` in the PWA; answering
   resumes the run on whichever runner holds it (durable record + DB-polling wait
   loop = cross-process resume, no new store).
3. **@runner routing** — a chat message starting with `@<runner-name> ...`
   (case-insensitive, matches P6 runner registry names/aliases) forwards the turn
   to that runner; unknown name → helpful error listing runners. Approvals from
   that remote run come back to the originating user's devices.
4. **UI** — approvals inbox page + inline approval card in chat; settings page
   listing "always" grants with revoke.
5. Flag `VEYA_APPROVAL_GATES` (default off; when off, behaviour unchanged).

Addenda:
- **J. Pairing codes** — flag `VEYA_DEVICE_PAIRING` (default OFF). New
  device/runner requests pairing → short code (8 chars, unambiguous alphabet,
  10-minute TTL, single use, rate-limited); a signed-in user confirms the code
  in Veya (UI + API) → device credential issued + registered in the existing
  runner/device registry. Code generation/validation = obase (crypto,
  constant-time compare); confirmation flow = omodul with decision_trail;
  never log codes or credentials.
- **K. Dispatch delivery states** — `delivered` / `started` / `unconfirmed`
  recorded when a coordinator dispatches work or a mid-run steer to another
  agent. Extend existing agent_mailbox / work_attempts records (no new store);
  state derivation = pure oskill over events; surface in UI/report.
- **L. Guard test only** — approvals/gates resolvable only by a human principal
  unless the approval's policy explicitly allows a delegated agent; an
  agent/coordinator principal attempting to approve its own or a sibling's
  pending approval is rejected and recorded in the decision trail. High-risk
  classes (DB migrations, payment/secret access, production) are human-only
  regardless.

## Architecture (3O PARADIGM)

New reusable logic goes into the in-repo 3O layers; server/ stays a thin
service layer. Element → layer → why (full table also in NANO_W6_REPORT.md):

| Element | Layer | Why |
|---|---|---|
| `ApprovalStore` (approvals + always-grants + handoffs, SQLite) | `veya/obase/approvals.py` | obase = external adapters + cross-cutting persistence; follows `SqliteKvStore` conventions (WAL-style lock, JSON values, atomic writes); never imports 3O layers |
| `PairingCodeStore` (generate/consume, constant-time, TTL, rate limit) | `veya/obase/pairing.py` | obase = crypto/secrets boundary (same as `veya/remote/auth.py` SHA-256 + `hmac.compare_digest`) |
| `approval_create` / `approval_resolve` / `grant_lookup` / `grant_put` / `grant_revoke` / `grant_list` / `handoff_create` / `handoff_resolve` / `pairing_code_create` / `pairing_code_consume` | `veya/oprim/approval.py`, `veya/oprim/pairing.py` | one atomic op each, ≤1 positional arg rest keyword-only, no oprim→oprim calls, no decision_trail; all IO via injected obase handle |
| `evaluate_capability_permission` (tool+args+grants → allow/ask/deny) | `veya/oskill/pure/permission.py` | 3O-PURE: stateless decision logic, no IO, no user_id (pseudonymized scope passed in by the service layer) |
| `parse_runner_mention` | `veya/oskill/pure/mention.py` | 3O-PURE: pure text parsing |
| `derive_delivery_state` (message+events → delivered/started/unconfirmed) | `veya/oskill/pure/delivery_state.py` | 3O-PURE: pure derivation over inputs |
| `request_permission_decision` (composes grant_lookup + evaluate_capability_permission + scope_match) | `veya/oskill/approval.py` | oskill composite: ≥2 oprims, stateless |
| `run_approval_flow` (request/resolve/handoff ops) | `veya/omodul/approval_flow.py` | omodul contract (config, input_data, output_dir) → dict, never raises; `_enabled_pillars = {"decision_trail"}` (security/approval related) |
| `run_pairing_flow` (request/confirm ops) | `veya/omodul/pairing_flow.py` | confirmation flow with decision_trail |
| Approval gate wiring, REST, push relay, @runner routing, guard L | `server/` (thin) | routes/auth/SSE/UI = project service layer; pseudonymize user ids before calling omodul |

Frozen decisions respected (3O_RECONCILIATION OBSOLETE): NO second approval
system, scheduler, event store, side-effect system, capability registry, ACP
authority, or MasterAgent. Approvals persist through the existing durable
store (`runtime/execution/durable.py` SQLite conventions) + GoalRun pause
state; pushes reuse P5 `PushNotifier`; routing reuses the P6 runner registry +
`RunnerClient`; events reuse `server/events.py`.

## Behaviour

### Gate flow (VEYA_APPROVAL_GATES=1)

1. Tool call classified (existing `_effect_for` + `classify_action_effect`).
2. When off: exactly today's behaviour.
3. When on, for gated classes (destructive / network / payment-like /
   human-only high-risk): the oskill composite
   `request_permission_decision` evaluates tool+args+grants →
   - `allow` (matching "always" grant) → proceed;
   - `ask` → pause: durable approval record created (status `pending`),
     Web Push fired to the user's subscribed devices with deep link
     `/approvals/<id>`, run waits (DB-polling loop, existing
     `_wait_approval` pattern — works across processes/runners);
   - `deny` (policy refusal) → refuse with reason.
4. Resolution options: `once` (default), `session` (in-memory session
   grant), `always` (persisted per-user+tool+scope grant), `deny`.
5. Human handoff: `request_handoff(reason)` → durable handoff record +
   paused state; resume via Done (`handoff_resolve(status="done")`) or Cancel
   (`status="cancelled"`).

### Guard L

`POST /api/v1/approvals/{id}/resolve` checks the principal: a human user
(Bearer user token) may resolve; an agent/coordinator principal (bot/agent
identity, or an explicit `principal: agent:<id>` marker) may resolve only if
the approval record's `delegate_policy` explicitly allows that agent; otherwise
403 + decision-trail entry. High-risk classes (db_migration, payment,
secret_access, production) are human-only regardless of delegation.

### @runner routing

`parse_runner_mention` (pure) extracts a leading `@name` (case-insensitive).
`run_bot_turn` consults the P6 registry (names + aliases); match → forward the
turn to that runner via the existing `RunnerClient.forward_turn` path (same
shape as bot.runner_id routing); no match → visible error turn listing
registered runners. Approval requests created during a remote run carry the
originating user's id, so the relay push reaches the user's devices; the
DB-polling wait loop resumes the run on the holding runner.

### Delivery states (K)

`AgentMessage` gains `delivery_state` (`unconfirmed` default). `poll()` →
`delivered`; `derive_delivery_state` (pure oskill) upgrades to `started` when
the target run has positive start evidence (first token/tool call/event).
Surfaced via the mailbox REST list + report; never assumed — no start signal
stays `unconfirmed`.

### Pairing (J, VEYA_DEVICE_PAIRING=1)

- `POST /api/v1/pairing/request` (device, rate-limited) → 8-char code from an
  unambiguous alphabet, 10-min TTL, single use; code hashed (SHA-256) at rest,
  constant-time compare on consume; never logged.
- `POST /api/v1/pairing/confirm` (signed-in user + code, omodul
  `pairing_flow` with decision_trail) → device credential issued (existing
  credential conventions; token_ref never the secret) + runner/device
  registered in the existing registry.

## Files

- `veya/obase/approvals.py`, `veya/obase/pairing.py` (new)
- `veya/oprim/approval.py`, `veya/oprim/pairing.py` (new)
- `veya/oskill/approval.py`, `veya/oskill/pure/permission.py`,
  `veya/oskill/pure/mention.py`, `veya/oskill/pure/delivery_state.py` (new)
- `veya/omodul/approval_flow.py`, `veya/omodul/pairing_flow.py` (new)
- `veya/oprim/__init__.py`, `veya/oskill/__init__.py`, `veya/omodul/__init__.py`
  (re-export)
- `server/approvals.py` (new: service + REST router), `server/pairing.py` (new),
  `server/bot_team/mention.py` (new), `server/user_control.py` (extend),
  `server/goal_run/agent_mailbox.py` (extend), `server/feature_flags.py`
  (2 flags), `server/app.py` (router mount)
- `apps/web/src/routes/approvals/+page.svelte` (new inbox),
  `apps/web/src/lib/components/ApprovalCard.svelte` (new inline card),
  settings + pairing UI (extend SettingsPanel)
- Tests: `tests/test_nano_w6_approvals.py`, `tests/test_nano_w6_pairing.py`,
  `tests/test_nano_w6_delivery_states.py`

## Verification

- `venv/bin/python -m pytest -q tests/test_nano_w6_approvals.py` (then
  pairing, then delivery_states — one or two files per call)
- Regression: `tests/test_lorca_p1_bots.py tests/test_lorca_p2_groups.py
  tests/test_p3a_bot_isolation.py tests/test_product_shell.py` (2 resume
  failures pre-existing)
- `python scripts/check_3o_gates.py --strict`,
  `scripts/check_no_reverse_dep.py`, `scripts/check_oskill_pure.py`,
  `scripts/check_no_direct_io.py` (baseline mode),
  `tests/guardians/test_3o_migration_guards.py`
- `cd apps/web && NODE_OPTIONS=--max-old-space-size=2048 pnpm run check &&
  pnpm run build`
- `ruff check .`

## Risks / gaps

- Cross-runner resume relies on the DB-polling wait (existing pattern); a
  runner holding a run must be able to read the same durable store (true for
  the hub topology; a fully remote runner with a private store is out of
  scope — the approval record is created before dispatch in that case).
- Session grants are in-memory (lost on restart) — documented, matches
  "allow for this session" semantics.
