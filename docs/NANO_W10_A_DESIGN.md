# NANO-W10 Design — Always-on agent ergonomics (group A: turn runtime core)

> Written first, before implementation, per `NANO_W10_SPEC.md` HARD RULES
> ("Finish: write docs/NANO_Wn_DESIGN.md first, then code+tests, then commit
> NANO_Wn_REPORT.md").
>
> **This document is scoped to `W10_GROUP_A.md`**: spec item 1 (turn queue),
> item 2 (wake router), and addendum W3 (Goal vs Loop), W4 (live stream + plan
> list + correction box), W5 (finish checkpoint card). Other workers own the
> remaining items in parallel; their sections below are the agreed contract,
> not this group's work.
>
> **Clean-room statement.** `NANO_W10_SPEC.md` line 1 names one upstream: a
> public CS146S lecture (Lee Robinson, Oct 2026, "Grok Bot"). It is a *talk*, not a
> code base: **no third-party source was read or copied.** Every design decision
> below is derived from the Veya codebase plus the feature prose in the spec.
>
> The three ADDENDUMs name three further upstreams. All are handled at the
> **design-description level only**, and all are attributed in
> `THIRD_PARTY_NOTICES.md`:
>
> | Upstream | Licence | What was done |
> |---|---|---|
> | `FeiZhuLulu/DeepSeek-Bot` | Apache-2.0 | Its `dsh` TypeScript plugin was **not vendored and not read as source**. The feature prose in the addendum informed *what to build*. |
> | `Panniantong/Agent-Reach` | MIT | Design borrowed (ordered per-channel backend ladders). **No upstream tool was vendored**; the ladders are Veya's own table format inside the existing CLI ladder. |
> | `totec448-spec/chat-on-steroids` | MIT | Design borrowed (persistent workers, goal/loop). Its ChatGPT web-UI automation/recording is explicitly **out of scope** (provider-terms risk). |
>
> GPL source is never consulted. Apache/MIT borrowing is design-level and is
> recorded in `THIRD_PARTY_NOTICES.md` (append-only).

---

## 1. Frozen decisions this work must not violate

From `3O_RECONCILIATION.md` "OBSOLETE" and `AGENTS.md` "🧊 冻结架构":

| Forbidden | What W10 does instead |
|---|---|
| second scheduler | The turn queue is a **queue in front of the existing turn path** (`server/bot_team/turns.py` / `async_turns.py`), not a clock. Wake sources keep using the existing `RoutineClock`/`RoutineRegistry`. |
| second task store | Turn state is written onto the **existing** product `Task` projection (via `begin_bot_turn`) and the existing `ConversationStore`. The queue keeps only *envelope* bookkeeping in this process — no new durable store. |
| second approval system | Item 3 "ask human" routes into the existing `SideEffectLedger` + `ActionGatewayAdapter` policy hook. Never a new approval table. |
| second event store | Decision trails go through `server/events.py` `append_canonical_event`. |
| second side-effect system | `runtime/execution/side_effects.py` only. |
| second secret vault | Addendum Q stores values in the **existing** secret/authorization store, injected env-only, masked through the existing masking path. |
| second permission system | Addendum P enforces through the existing `server/authorization` + ActionGateway. |
| second capability registry | Addendum V registers channels in the **existing** `server/tool_registry` / W8 CLI ladder — not a new registry. |
| second MasterAgent / change to it | none — `MasterAgent` untouched |
| ModelRouter task-semantic selection | untouched (availability / failover / usage only). Item 3/4/M call it only along the availability path. |
| Domain allow/deny policy engine | Item 3 emits *pre-existing-policy* verdicts only. It never overrides an existing DENY (see §3.3). |

---

## 2. Element → layer → reason (group A)

| # | Element | Layer | One-line reason |
|---|---------|-------|-----------------|
| 1 | `turn_envelope`: `idem_key`, `envelope_fingerprint` | **oprim** | One atomic digest each. They call `obase.canonical_json`/`sha256_hash` directly — importing another oprim is a gate violation. |
| 1 | `turn_priority`: `classify_inbound`, `envelope_priority`, `order_queue`, `steer_decision`, `coalesce_window` | **oskill (pure)** | Ordering/coalescing/steer rules over envelopes — deterministic, no IO, no `user_id`. |
| 1 | `turn_intake` | **omodul** | A business transaction (envelope → action) with the standard `(config, input_data, output_dir) → dict` contract. |
| 1 | `TurnQueue`, `TurnQueue.steer` | **layer4 (`server/nano_w10`)** | In-process envelope bookkeeping in front of the existing turn path, exactly like `async_turns._pending`. Not a store. |
| 2 | `worth_bugging_user`, `classify_wake`, `is_quiet_hours` | **oskill (pure)** | The "reply now / digest / silent / emoji-ack" policy is a deterministic predicate over signals + settings. |
| 2 | `wake_router` | **omodul** | Dedupe + classify + dispatch is a transaction; the engine assembly stays layer4. |
| 2 | `WakeRouter`, `build_triage_engine` | **layer4** | Assembles the **existing** `oservi.triage(on_signal)` skeleton via `ServiceManifest`; no new engine. |
| W3 | `loop_policy`: `parse_loop_brief`, `loop_decision`, `checkpoint_card` | **oskill (pure)** | Budget/stop predicates and card rendering are pure. Reuses the caller's existing no-progress verdict rather than re-implementing it. |
| W3 | `loop_run` | **omodul** | GoalRun *policy* (not a scheduler) is a business transaction. |
| W3/W5 | `LoopRunPolicy`, `finish_card` | **layer4** | Reads the existing GoalRun projection; posts through the existing conversation sink. |
| W4 | `live_trace`: `step_from_tool_event`, `plan_rows`, `correction_action` | **oskill (pure)** | Projections over already-emitted stream events; `correction_action` reuses item 1's steer vocabulary. |
| W4 | `live_trace` (omodul) | **omodul** | Projects the existing `fire_step` stream and lands a correction; no second stream or event store. |
| W4 | `LiveStream`, `wire_to_turn_queue` | **layer4** | Binds the omodul to the existing `on_step` callback and the item-1 queue. |
| W4 | `POST /api/v1/bots/{bot_id}/corrections` | **layer4 (route)** | Thin; `require_user` + `_require_bot(owner_id=...)`; delegates to `TurnQueue.steer`. |
| — | other spec items (3–7, M–V) | other groups | Not this group's scope. Contract only. |

---

## 3. Architecture

```
┌─ layer4 (project service layer) ────────────────────────────────────────────┐
│ server/nano_w10/{flags,turn_queue,wake_router,auto_review,prompt_cache,     │
│                  memory_layers,a11y,tool_errors,eval_flywheel,search,       │
│                  channels,secrets,group_roles,usage,loop,workers}.py        │
│ server/routes/nano_w10.py  ·  scripts/llm_tap.py  ·  scripts/steward_eval.py│
│ server/bot_team/memory.py (extended, not replaced)                         │
└───────────────┬────────────────────────────────────────────────────────────┘
                │ injects stores/callables downward (never imports business stores downward)
┌───────────────▼─────────── 3O ─────────────────────────────────────────────┐
│ oprim   turn_envelope  ·  idem_key  ·  review_outcome  (atomic digests)     │
│ oskill  pure/{turn_priority,wake_policy,command_review,prompt_sections,    │
│               steward,memory_layers,observation_ladder,tool_error,         │
│               eval_case,search_order,channel_ladder,loop_policy}.py        │
│ omodul  turn_intake · wake_router · auto_review · prompt_cache ·           │
│         context_steward · memory_review · loop_run                         │
│ obase   a11y_snapshot · dom_reader · channel_probe · secret_env            │
│ oservi  ASSEMBLED EXISTING engines only: triage(on_signal) · alerter ·     │
│         sequential_composer · feed_tracker  (no new engine)                │
└────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Item 1 — turn queue (priority + coalescing)

* Priority: `direct user message` > `group/@mention` > `channel inbound` > `routine/background`.
* A direct user message arriving **while a turn is pending** becomes a **steer**
  (W6 K receipts: `delivered` / `started` / `unconfirmed`) instead of a second
  queued turn. `steer_decision` returns the receipt state so the caller can
  record what actually happened; delivery is never assumed.
* Messages arriving within `coalesce_window_s` (default 2.0, configurable) while
  a turn is pending merge into one turn.
* `idem_key` is a per-inbound-message idempotency key; the queue is a pure
  function of envelopes + now, and a repeated key is dropped by the **existing**
  conversation store check, not a new ledger.
* The queue itself lives in layer4 (`server/nano_w10/turn_queue.py`) as an
  in-process envelope list — exactly like `async_turns._pending`, which the
  module docstring already declares is not a store.

### 3.2 Item 2 — wake router + "worth bugging the user?"

All wake sources (user message, routine clock, W9 event trigger, W7 channel,
runner result, sub-agent completion) call one intake. Intake dedupes via
`compute_fingerprint_for` and classifies the outcome as
`reply_now | digest | silent | emoji_ack`.

Policy inputs: user-directed vs background, novelty, urgency, the user's quiet
hours setting, and the per-bot notification level. Push (P5) and channels (W7)
fire **only** for `reply_now`; `digest` entries reuse the W7 morning feed.
Implementation: the **existing** `triage(on_signal)` and `alerter` engines are
assembled through `ServiceManifest` in layer4; this work adds no engine.

### 3.3 Item 3 — auto-review of commands

Hooked at the existing `ActionGatewayAdapter._evaluate_policy` **before** the
existing policy runs. A separate reviewer model call (injected `LLMCaller`,
resolved over the existing ModelRouter availability path) returns
`allow | ask human | block` with a reason, given the command, the originating
instruction, and whether untrusted input influenced it (provenance marked by
`firewall.sanitize`).

**Precedence (this is the frozen part):** the review may only *escalate*.
An existing `DENY` stays `DENY`; a review `allow` never downgrades an existing
`APPROVAL_REQUIRED`. Human-only categories (W6 L: DB migrations,
payments/secrets, production) always go to the human. "Ask human" re-enters the
existing SideEffectLedger approval path — it does not create one.

### 3.4 Item 4 + M — prompt-cache discipline and the context steward

* Sections are assembled in a **fixed order**, each hashed:
  `system → tool_defs → core_memory → …then conversation`. The tool set is
  **frozen for the duration of a turn**: `tool_search` results are appended as
  messages, never by mutating the tool list.
* Cached vs uncached input tokens are recorded into the existing usage events
  and surfaced on the performance route as a cache-hit rate.
* Prewarm on a debounced typing signal; proactive compaction while idle and the
  provider cache window is still warm (default 5 min) reuses the existing
  `compact_session`.
* **M**: at turn end, one cheap model call decides `CHECKPOINT` or `HANDOFF`
  when tokens pass the check line (0.8) / hard line (0.95). The memory/system
  section is frozen per part so the provider cache holds.

### 3.5 Item 5 + O — memory layers, review, team memory

```
<bots_root>/<bot_id>/memory/
    MEMORY.md              always injected, capped, gardening proposes trims
    <topic>.md             topic files, capped
    daily/YYYY-MM-DD.md    dated log, searchable, never injected
    scratch.md             TTL entries, injected while live, GC'd
    journal.jsonl          every change with before/after
<bots_root>/team/memory/   TEAM scope (read by all, written by Chef/admins)
```

Memory review (an LLM pass) may only keep/update/remove **its own** entries:
one line, ≤300 chars, in the user's language, ending
`[source: <anchor>; added: YYYY-MM-DD; by: <bot>]`. It refuses
keys/passwords/ID numbers and anything that arrived from web/file/tool results.
Gardening **never** silently deletes a user-written line.

### 3.6 Item 6 + R — observation ladder and DOM reader

Order: **a11y tree → trimmed HTML → screenshot/pixel**. The ladder decision is
pure; the a11y snapshot (Playwright `accessibility.snapshot`, AT-SPI fallback) is
an obase adapter. The DOM reader converts the visible DOM to Markdown, marks
controls as `[#n]`, opens shadow DOM and same-origin iframes to depth 3, skips
hidden nodes, **never** reads password / `autocomplete=cc-*` / OTP field values,
and enforces the published limits. Fetched page content is **data**, never
instructions (firewall provenance).

### 3.7 Item 7 — small items

* **7a** (default **ON**): every tool error is normalised to
  `{error, what_failed, next_step}`. A guard test walks the live registry.
* **7b**: a failed or user-corrected GoalRun exports as an `EvalCase` through one
  CLI (`veya eval export-failure`) and one REST route.
* **7c**: the W8 ladder doc extends to the full 6 steps:
  `context → connector/MCP/API → web search → browser → desktop → ask human`.

### 3.8 Addendum Q/P/V — secrets, roles, channels

* **Q**: `request_secret` puts a secret card in the bot's **own** chat (never in
  a group). The value lands in the existing secret/authorization store, is
  injected env-only as `VEYA_SECRET_<NAME>`, and every exact copy is masked to
  `[secret:<NAME>]` before tool results are saved and before any model request.
  `list_secrets` returns names only. Settings warns when the page is not HTTPS.
* **P**: group admin bot + reply mode, enforced through the existing
  authorization/ActionGateway. `[PASS]` silence is kept.
* **V**: per-channel ordered backend ladders (a pure table); obase *really*
  probes each backend (small known-good call, 5 s timeout); `veya doctor channels`
  + `GET /api/v1/channels/status` + a settings section. Probes run on demand and
  from the existing routine clock. Login-based channels stay OFF until enabled,
  and enabling one goes through the existing approval flow.

### 3.9 Addendum W1-W5 — persistent workers, loop, live stream

* **W1**: a delegated worker keeps its context inside one GoalRun/session; the
  next task to the same worker resumes it. Worker list on the existing
  session/team UI; reset/ retire supported.
* **W2**: compact/handoff writes ONE resume brief carrying the session **and**
  each worker's summary + open items (reuses the W2 fork/handoff brief).
* **W3**: `Loop` is a GoalRun **policy** — budget of turns/time, a checkpoint per
  round, stop on budget / user stop / no progress for 2 rounds. Not a scheduler.
  `Goal` mode is a no-op path, so turning the flag on cannot change a Goal run.
* **W4**: live tool-result stream from the existing `server/events.py` stream +
  a per-task plan list + a correction box that lands via the item-1 steer. The
  tool **result** is a first-class step (not reconstructed by the client), the
  plan is derived from the existing `plan_update` todos rather than stored, and
  the correction box is literally item 1's `TurnQueue.steer` — there is no
  second control channel. New route `POST /api/v1/bots/{bot_id}/corrections`
  requires auth and bot ownership.
* **W5**: a "what changed / tests run / open risks" finish card before done,
  rendered by the same `checkpoint_card` element the loop checkpoints use.

---

## 4. Flags (HARD RULES: default OFF, except 7a)

| Flag | Default | Guards |
|---|---|---|
| `VEYA_TURN_QUEUE` | off | item 1 turn queue |
| `VEYA_WAKE_ROUTER` | off | item 2 wake routing |
| `VEYA_AUTO_REVIEW` | off | item 3 command auto-review |
| `VEYA_PROMPT_CACHE` | off | item 4 prompt-cache discipline |
| `VEYA_BOT_MEMORY_MD` | off | item 5 memory layers (flag stays, per spec) |
| `VEYA_MEMORY_REVIEW` | off | addendum O memory review |
| `VEYA_A11Y_FIRST` | off | item 6 a11y-first observation |
| `VEYA_DOM_READER` | off | addendum R DOM reader |
| `VEYA_CONTEXT_STEWARD` | off | addendum M context steward |
| `VEYA_GROUP_ROLES` | off | addendum P group roles |
| `VEYA_BOT_SECRETS` | off | addendum Q per-bot secrets |
| `VEYA_CHANNEL_LADDER` | off | addendum V channel ladder |
| `VEYA_PERSISTENT_WORKERS` | off | addendum W1 persistent workers |
| `VEYA_LOOP_MODE` | off | addendum W3 loop mode |
| `VEYA_LIVE_STREAM` | **on** | addendum W4 live stream (spec: "live stream UI may be on") |
| item 7a tool-error shape | **on** | spec: "Flags default OFF except 7a" |
| usage page / search fallback / `llm_tap` / channel status+doctor | **on** | read-only surfaces the addendum allows |

An **off** flag never silently no-ops: it returns
`{"code": "FEATURE_DISABLED", "flag": ...}` so an operator can tell "disabled"
from "broken".

---

## 5. Gates

`python scripts/check_3o_gates.py --strict` plus `check_no_reverse_dep.py`,
`check_oskill_pure.py`, `check_no_direct_io.py` (baseline mode) and
`tests/guardians/test_3o_migration_guards.py` must show **zero NEW** violations
against the pre-existing baseline (recorded in the report).

Test file: `tests/test_nano_w10_a.py`.
Regression set: `tests/test_lorca_p1_bots.py`, `tests/test_lorca_p2_groups.py`,
`tests/test_p3a_bot_isolation.py`, `tests/test_product_shell.py`
(2 resume failures are pre-existing).