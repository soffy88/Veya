# NANO-W9 Design — Branch Rooms, Routine Triggers, ACP Members, Audit Chain, Receipt Search, Agent CLI

> Written first, before implementation, per `NANO_W9_SPEC.md` HARD RULES
> ("Finish: write docs/NANO_Wn_DESIGN.md first, then code+tests").
>
> **Clean-room statement.** The feature list in `NANO_W9_SPEC.md` line 1 names two
> upstream projects. `nanoMuse` is **GPL-3.0** and therefore **NOT consulted, read, or
> copied** — every design decision below is derived from the Veya codebase and from the
> feature prose in the spec. `HKUDS/CLI-Anything` is Apache-2.0 and is referenced only at
> the level of *harness conventions* (a `--json` flag with a stable envelope, a REPL on
> bare invocation, self-describing `--help`, a generated `SKILL.md`); no CLI-Anything code
> was read or copied. The `block/buzz` family is Apache-2.0 and likewise informs only the
> *feature description*. No third-party source tree was added to this repository; see
> "Attribution" in `NANO_W9_REPORT.md`.

---

## 1. Frozen decisions this work must not violate

Taken verbatim from `3O_RECONCILIATION.md` §"OBSOLETE" and `AGENTS.md` "🧊 冻结架构":

| Forbidden | What W9 does instead |
|---|---|
| second approval system | reaction-as-approval → existing `SideEffectLedger` + `ActionGatewayAdapter` (`server/action_gateway_adapter.py`) |
| second scheduler | triggers feed the existing `RoutineRegistry` (`server/goal_run/routine_registry.py`) and `RoutineClock`; no new clock |
| second event store | hash chain columns ride on the **existing** `server/events.py` `EventEnvelope` / JSONL |
| second side-effect system | `runtime/execution/side_effects.py` only |
| ACP authority | ACP member is an **executor/harness provider**; turns are forwarded through the existing `project_run_goal` |
| second MasterAgent / MasterAgent change | none — `MasterAgent` untouched |
| ModelRouter task-semantic selection | untouched (availability / failover / usage only) |
| second task store | `.veya-project/goal-runs/` unchanged |

---

## 2. Element → layer → reason (summary; full table in the report)

| # | Element | Layer | One-line reason |
|---|---------|-------|-----------------|
| 1 | `branch_room_sync` | omodul | a business transaction (git/CI/review facts → group room transcript) with clear done/failed semantics |
| 1 | `branch_room_name` / `summarize_review_findings` | oskill (pure) | ≥2 oprim-free pure transforms (naming + digest) composed; stateless, no IO |
| 2 | `trigger_match` | oskill (pure) | regex / mention / reaction matching is a pure predicate over a signal |
| 3 | `StdioJsonRpcTransport` | obase | external-world adapter (child process + NDJSON over pipes) = obase's exact job |
| 4 | `compute_event_hash` / `chain_head` / `verify_chain_links` | oprim | single atomic digests; no peer calls |
| 5 | `rank_receipts` / `merge_receipts` / `build_citation` | oskill (pure) | ranking + merge + citation is a deterministic algorithm over receipt dicts |
| E | `assemble_trajectory` / `replay_states` | oskill (pure) | trajectory assembly is pure; persistence stays on `server/events.py` |
| F | 7 × `cli_gen_*` phase callables | omodul | each phase is a business transaction with the standard `(config, input_data, output_dir)` contract |
| 1–F | groups / routines / GoalRun / event store / ActionGateway | layer4 (`server/`) | project-owned stores and routes; layer4 supplies them by injection |

---

## 3. Architecture

```
┌─ layer4 (project service layer) ─────────────────────────────────────────┐
│ server/nano_w9/{flags,branch_rooms,routine_triggers,acp_members,          │
│                 audit_chain,search_receipts,trajectory,cli_gen}.py       │
│ server/routes/nano_w9*.py  ·  scripts/veya_cli.py  ·  skills/veya-cli/   │
└───────────────┬──────────────────────────────────────────────────────────┘
                │ injects (never imports business stores downward)
┌───────────────▼─────────── 3O ──────────────────────────────────────────┐
│ omodul : branch_room_sync · cli_gen_{analyze,design,implement,           │
│                                 test_plan,tests,docs,package}            │
│ oskill : pure/{trigger_match, receipt_rank_merge, trajectory_assemble}   │
│ oprim  : audit_chain                                                  │
│ obase  : acp_stdio (single canonical stdio JSON-RPC transport)          │
│ oservi : assembled EXISTING engines — triage (on_signal), researcher,   │
│          sequential_composer — via ServiceManifest, no new engine       │
└────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Element 1 — branch as room

* **Trigger surfaces** (layer4):
  * `POST /api/v1/nano-w9/branch-rooms/open` — "open room on a branch" (explicit user action).
  * `on_branch_created` hook callable invoked from the workspace/branch-creation path
    (injected; the branch feature calls it, W9 never forks that path).
* **Naming** is pure (`oskill/pure/branch_room_name`): `branch:<name>` — a *group name
  prefix*, not a new group `kind` column. `GroupChat` has no `kind` field and adding one
  would be a frozen-adjacent schema change to P2; the prefix is the documented convention
  and `list_branch_rooms()` recognises it by prefix.
* **Members** reuse the P1 `BotRegistry`; the room is created with
  `GroupStore.create(name=..., bot_ids=[...])` and every fact is posted with
  `GroupStore.append_message(group_id, role="bot", bot_id=..., text=...)`.
  **Membership is injected** (`groups` layer4 injection point) so the omodul never
  imports `server/bot_team` — that would be a reverse dependency.
* **CI check results** reuse the W3 `gh` polling path by *injecting* a
  `fetch_checks(branch, repo_path) -> list[str]` callable (production wires
  `server.routes.review._fetch_failing_checks`; tests inject a stub). W9 does not
  re-implement `gh` invocation.
* **AI first-pass review** is optional: an injected `llm_caller`. When present the
  omodul's `cost` pillar accumulates `result["cost_usd"]`; when absent the step is
  recorded as `skipped` in the decision trail.
* **Line annotations** reuse `server/goal_run/annotations.py` via injection.
* **Merge decision** is posted as a `role="bot"` message and recorded in the
  decision trail; the decision itself is *made by the GoalRun*, W9 only reports it.
* **Assembly**: `sequential_composer` over the two omoduls
  (`branch_room_open`, `branch_room_publish`) in `server/nano_w9/branch_rooms.py`.

### 3.2 Element 2 — event-triggered routines

* **Triggers** (declared on the existing `RoutineSpec.probe`-adjacent field; W9 adds
  `RoutineSpec.triggers: list[dict]` — *additive*, backward compatible through
  `from_dict` defaults, and **not** a new scheduler).
  Trigger kinds: `webhook` (HMAC-signed per-routine secret), `message` (regex/mention),
  `reaction` (e.g. 👍).
* **Matching** is pure: `oskill/pure/trigger_match.py`
  (`signal_matches_triggers`, `match_message_trigger`, `match_reaction_trigger`).
* **Webhook verification** reuses `obase.webhook.sign_payload` (HMAC-SHA256) — no second
  crypto path. The per-routine secret is read from the routine's own trigger entry; it is
  never persisted in plaintext outside the routine spec and never logged.
* **on_signal path**: `TriageEngine` (existing oservi skeleton, `trigger_mode="on_signal"`)
  assembled with `filters=[...]` (layer4, project-specific predicates) and
  `llm_caller=[noop_scorer]` — matching is deterministic, so no LLM is required to score.
  On a match, the engine result is fed to the **existing**
  `server/goal_run/routine_dispatch.dispatch_trigger(...)` (GoalRun-bound, idempotent) —
  or, when the routine is clock-registered, `RoutineRegistry.lookup(topic)`.
* **Reaction-as-approval**: a `reaction` trigger with `action="approve"` does **not**
  approve by itself. It emits an approval *request* through
  `ActionGatewayAdapter` / `SideEffectLedger`; the ledger records
  `state="declared"` and the actual decision is resolved by the existing user-approval
  flow (`server/user_control.resolve_approval`). W9 holds no approval state.

### 3.3 Element 3 — ACP members

* **Transport convergence (§1.4)**: the stdio JSON-RPC plumbing already lives in
  `server/acp_client.py`. W9 promotes it to the **canonical** implementation in
  `veya/obase/acp_stdio.py::StdioJsonRpcTransport` (external adapter = obase's job) and
  rewrites `ACPBackend` to **delegate** to it. No copy-paste; a guard test asserts the
  delegation.
* **ACP protocol semantics** are *not* re-implemented: layer4 composes
  `StdioJsonRpcTransport` with the external `obase.acp_adapter.ACPAgentAdapter`
  (via `veya.platform.load("obase")`) when the main library is mounted.
* **Detection** reuses the W1 `runtime/coding/cli_agents.CLIAgentRegistry` plus the
  `kind="acp"` entries already in `server/backends.py`.
* **Member record** = an existing `BotSpec` with `runner_id="acp:<backend_id>"` and
  `knowledge_namespace="acp"`. **No new member store.**
* **Turn forwarding** = `project_run_goal(..., tasks=[...])` with the task assigned to the
  ACP member. ACP is never an authority: it cannot start, approve, or schedule anything;
  it can only execute a task inside an existing GoalRun.

### 3.4 Element 4 — hash-chained audit

* **Chain rule** (`oprim/audit_chain.py`):
  `hash_i = hex(sha256(obase.canonical_json({**event_i, "prev_hash": hash_{i-1}})))`
  with `hash_0.prev_hash = GENESIS_HASH = "0"*64`.
  `obase.canonical_json` + `obase.sha256_hash` are reused — no second JSON/hash path.
* **Storage**: the chain columns (`prev_hash`, `hash`, `chain_index`) live on the
  **existing** `EventEnvelope` record in `server/events.py`. No new store, no migration.
* **Coverage**: messages (`server/bot_team/groups`), tool side-effects
  (`action_gateway.audit` / `side_effect.*`), approvals (`tool.approval_*`), git events
  (`goal_run.*`).
* **Verify** = `GET /api/v1/nano-w9/audit/verify` and `veya-cli audit verify`.
  Verification re-derives every hash from the raw records and reports the first
  broken link index — it never trusts a stored `hash`.

### 3.5 Element 5 — search with receipts

* **Ranking/merge is pure** (`oskill/pure/receipt_rank_merge.py`): deterministic
  `score = w_text*text_overlap + w_recency*recency_decay + w_source*source_prior`,
  tie-break by `(score, receipt_id)` for total ordering. It is deliberately **not** BM25:
  it scores a small heterogeneous receipt set with a recency decay and a source prior,
  which is a different function than `oprim.bm25_search`'s corpus retrieval — no
  duplicate implementation.
* **Per-source queries are oprim-shaped** (`oprim/receipt_query.py`): each source
  (sessions FTS, GoalRun results, group messages, approvals, annotations) gets ONE atomic
  `query_receipts(source, ...)` call; the omodul does not know any store's schema.
* **Citations**: every hit carries `citation = {source, ref, title, uri}`; the answer
  text is assembled by `build_citation` so the answer can never cite nothing.
* **Assembly**: the existing `researcher` engine (`on_demand`) with
  `search = query_receipts`, `fetch = load_receipt`, `llm = optional summarizer`.

### 3.6 Element 6 — `scripts/veya_cli.py`

`argparse` (stdlib; **no new dependency** — `click` is not a declared runtime dep).
* every command: `--json` → stable envelope `{"status": "ok"|"error", ...}` on stdout,
  errors as `{"status":"error","error":{...}}` with **non-zero exit**;
* bare invocation → interactive REPL;
* `--help` self-describing;
* auth: `VEYA_TOKEN` env var (or `--token`), sent as `Authorization: Bearer …` to the
  existing `server.auth.resolve_token` store — the same token the REST API uses.

### Addendum D — CLI-Anything conventions + generated SKILL.md

* `scripts/gen_veya_cli_skill.py` walks the live `argparse` tree of `scripts/veya_cli.py`
  and emits `skills/veya-cli/SKILL.md` (YAML frontmatter `name`/`description`, command
  groups, examples, agent guidance).
* `tests/test_nano_w9_cli_harness.py::test_skill_md_in_sync_with_command_tree` re-generates
  into a tmp dir and asserts byte-equality with the checked-in file. Drift fails CI.

### Addendum E — preview + trajectory

* **Pure** (`oskill/pure/trajectory_assemble.py`): `assemble_trajectory(entries)`,
  `replay_states(entries)` (walk `command → artifact` states), `build_preview_bundle(...)`.
* **Persistence**: trajectory entries `{step, command/tool, artifact_ref, preview_ref, ts}`
  are appended to the **existing** `server/events.py` topic `trajectory.recorded`
  (already in `_MINIMAL_EVENTS`). No new store.
* **Replay view**: `veya-cli trajectory replay --goal <id>` (CLI) + a
  `GET /api/v1/nano-w9/trajectory/{goal_id}` endpoint. Frontend component
  `apps/web/src/lib/components/TrajectoryReplay.svelte`, rendered only when the route
  already renders goal detail — no new route, no new nav entry.

### Addendum F — "Make it agent-native" GoalRun template

7 phases, each an omodul with the standard signature, composed by the **existing**
`SequentialComposerEngine` via a `ServiceManifest`:

| Phase | omodul | Output |
|---|---|---|
| 1 | `cli_gen_analyze` | repo/URL analysis → target description |
| 2 | `cli_gen_design` | command group design |
| 3 | `cli_gen_implement` | `--json` CLI implementation |
| 4 | `cli_gen_test_plan` | test plan |
| 5 | `cli_gen_tests` | tests |
| 6 | `cli_gen_docs` | `SKILL.md` docs |
| 7 | `cli_gen_package` | packaging |

* Registered as a **routine/GoalRun template** on the existing machinery
  (`RoutineSpec.goal_template` + `project_run_goal`), never a second runner.
* Default model = strongest available (`veya1.2` proxy via `veya/obase/canonical_proxies`).
* **experimental** in every response; flag `VEYA_CLI_GEN` **default OFF**.

---

## 4. Feature flags

| Flag | Default | Gates |
|---|---|---|
| `VEYA_BRANCH_ROOMS` | off | element 1 endpoints + composer |
| `VEYA_ROUTINE_TRIGGERS` | off | element 2 signal router + trigger dispatch |
| `VEYA_ACP_MEMBERS` | off | element 3 member registration + forwarding |
| `VEYA_AUDIT_CHAIN` | off | element 4 chain stamping + verify endpoint |
| `VEYA_CLI_GEN` | off | addendum F phases |
| search + CLI | on | elements 5/6 are read-only surfaces |

Off by default ⇒ an off flag yields `{"status":"error","error":{"code":"FEATURE_DISABLED"}}`
(HTTP 404 for routes), never a crash and never a silent no-op.

---

## 5. New model-callable tools

**None.** No `server/tool_registry.py` entry is added. Everything W9 exposes is either an
HTTP endpoint for the existing shell/UI or a CLI command. This is stated explicitly because
HARD RULES require any new model-callable tool to be listed.

---

## 6. Test plan (only these files are run)

| File | Covers |
|---|---|
| `tests/test_nano_w9_buzz.py` | elements 1–5 + flags |
| `tests/test_nano_w9_cli_harness.py` | element 6 + addendum D (REPL, `--json`, SKILL.md sync) + addendum E replay |
| regression | `tests/test_lorca_p1_bots.py`, `tests/test_lorca_p2_groups.py`, `tests/test_p3a_bot_isolation.py`, `tests/test_product_shell.py` |

The full `tests/` directory is never run (OOM, per HARD RULES).

---

## 7. Gate plan

```
.venv/bin/python scripts/check_3o_gates.py --strict
.venv/bin/python scripts/check_no_reverse_dep.py . --baseline scripts/baseline_reverse_dep.txt --quiet
.venv/bin/python scripts/check_oskill_pure.py . --baseline scripts/baseline_oskill.txt --quiet
.venv/bin/python scripts/check_no_direct_io.py . --baseline scripts/baseline_direct_io.txt --quiet
.venv/bin/python -m pytest -q tests/guardians/test_3o_migration_guards.py
```

**Pre-existing drift, recorded at HEAD before any W9 change** (so "zero NEW violations"
is measurable):

```
OPRIM_PEER_CALLS_INFO=10 TYPES=2 VIOLATIONS=0
OMODUL_DIRECT_CALLS_VIOLATIONS=0 EXCEPTIONS=4
DIRECT_IO_NEW_VIOLATIONS=60          <-- pre-existing, not W9
DIRECT_MEMORY_WRITE_VIOLATIONS=0
ACTION_GATEWAY_READONLY_INFO=4 BYPASS_VIOLATIONS=4   <-- pre-existing, not W9
check_3o_gates.py --strict           <-- exit 1 at HEAD (the two buckets above)
```

`scripts/baseline_direct_io.txt` was last regenerated in `bc7b52c7`; the 60 drift items
come from later merged branches. W9 does **not** touch that baseline (a silent baseline
reset is an explicit anti-pattern) — the requirement is *zero new*, verified by comparing
the bucket sizes and by asserting no `[FAIL]` line mentions a W9 file.