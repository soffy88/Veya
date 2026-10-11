# NANO-W9 Implementation Report

> Spec: `NANO_W9_SPEC.md` (branch `feat/nano-buzz-ideas`).
> Design (written first): `docs/NANO_W9_DESIGN.md`.
> Base commit: `468f4c1d` · Head: `209e5529` + this report.
> Nothing was pushed; no service was restarted.

---

## 1. Clean-room statement

`NANO_W9_SPEC.md` line 1 names three upstreams.

| Upstream | Licence | What was actually done |
|---|---|---|
| `nanoMuse` | **GPL-3.0** | **Not consulted, not read, not copied.** Every design decision comes from the Veya codebase plus the feature prose in the spec. Nothing GPL-derived entered this repo, so the worktree stays GPL-clean. |
| `HKUDS/CLI-Anything` | Apache-2.0 | Referenced only at the level of *harness conventions* — a `--json` flag with a stable envelope, REPL on bare invocation, self-describing `--help`, a generated `SKILL.md`. **No CLI-Anything source was read or copied**; the conventions are reimplemented from the description. |
| `block/buzz` family | Apache-2.0 | Informed the feature description only. No code was copied. |

No third-party source tree was vendored, so there is nothing to attribute in a
`THIRD_PARTY_NOTICES` file — the repo has none today and W9 did not create one.
Had any code been borrowed, the spec's attribution requirement would have forced
that file to be added.

---

## 2. element → layer → reason (MANDATORY table)

| # | Element | Module | Layer | Why this layer (not another) |
|---|---------|--------|-------|------------------------------|
| 1 | `compute_event_hash` | `veya/oprim/audit_chain.py` | **oprim** | One atomic digest over one event. No peer calls, no trail, no orchestration. |
| 1 | `chain_head` | `veya/oprim/audit_chain.py` | **oprim** | One atomic fold over an ordered record list. |
| 1 | `verify_chain_links` | `veya/oprim/audit_chain.py` | **oprim** | One atomic re-derivation that reports the first break. |
| 1 | `normalize_receipt` / `normalize_query` / `receipt_uri` | `veya/oprim/receipt_query.py` | **oprim** | One source per call; owns the receipt *shape*, not any store's schema. |
| 2 | `signal_matches_triggers` + 3 matchers | `veya/oskill/pure/trigger_match.py` | **oskill (pure)** | ≥2 oprim-shaped transforms composed into predicates; stateless, no IO, deterministic. |
| 3 | `rank_receipts` / `merge_receipts` / `score_receipt` / `build_citation` / `build_answer` | `veya/oskill/pure/receipt_rank_merge.py` | **oskill (pure)** | Ranking + de-dup + citation rendering is a deterministic algorithm over receipt dicts. No IO, no `user_id`. |
| E | `assemble_trajectory` / `replay_states` / `build_preview_bundle` | `veya/oskill/pure/trajectory_assemble.py` | **oskill (pure)** | Trajectory assembly and the replay walk are pure transforms; persistence stays on the event log. |
| 3 | `StdioJsonRpcTransport` | `veya/obase/acp_stdio.py` | **obase** | External-world adapter: spawning a child process and speaking NDJSON JSON-RPC over its pipes is precisely obase's charter (§3/§7.2). |
| 1 | `branch_room_open`, `branch_room_publish` | `veya/omodul/branch_room_sync.py` | **omodul** | Business transactions with clear completed/failed semantics and the standard `(config, input_data, output_dir) → dict` contract. |
| F | `cli_gen_{analyze,design,implement,test_plan,tests,docs,package}` | `veya/omodul/agent_native_scaffold.py` | **omodul (×7)** | Each phase is a business transaction with the standard contract; they live in one module precisely so none imports a sibling omodul. |
| 1 | `BranchRooms`, `branch_room_name`, `list_branch_rooms` | `server/nano_w9/branch_rooms.py` | **layer 4** | Binds the P2 `GroupStore`; group storage is project-owned, so the 3O element receives it by injection. |
| 2 | `RoutineSignalRouter`, `ApprovalBridge`, `verify_webhook_signature`, `build_triage_manifest` | `server/nano_w9/routine_triggers.py` | **layer 4** | Assembles the existing `RoutineRegistry` + `dispatch_trigger` and the existing action gateway. Routing is a business decision. |
| 3 | `AcpMembers`, `detect_acp_candidates`, `acp_runner_id` | `server/nano_w9/acp_members.py` | **layer 4** | Member records live in the existing P1 `BotRegistry`; turn forwarding is the existing `project_run_goal`. |
| 4 | `stamp_event`, `verify_audit_chain`, `audit_records`, `audit_envelopes` | `server/nano_w9/audit_chain.py` | **layer 4** | Owns *which* topics are audited and how chain columns ride the existing `server/events.py` record. The digest itself is an oprim. |
| 5 | `ReceiptSearch`, `default_sources` | `server/nano_w9/search_receipts.py` | **layer 4** | Binds W2 FTS / group store / goal events to per-source callables; the merge itself is pure. |
| E | `Trajectory` | `server/nano_w9/trajectory.py` | **layer 4** | Persistence onto the existing `trajectory.recorded` topic + the read model the UI/CLI render. |
| F | `run_cli_gen`, `build_cli_gen_manifest`, `PHASE_FUNCTIONS` | `server/nano_w9/cli_gen.py` | **layer 4** | Assembles the 7 omodul phases through `SequentialComposerEngine` (§1.2.1: omodul composition goes through the orchestration layer). |
| all | feature flags | `server/nano_w9/flags.py` | **layer 4** | Deployment configuration is a project concern. |
| all | HTTP routes | `server/routes/nano_w9.py` | **layer 4** | Routes/auth/SSE are the project service layer (§8); the handler pseudonymizes the user before any 3O call. |
| 6 | `scripts/veya_cli.py`, `scripts/gen_veya_cli_skill.py`, `skills/veya-cli/SKILL.md` | — | **layer 4 (CLI)** | CLI is explicitly the project service layer. |
| E | `TrajectoryReplay.svelte` | `apps/web/src/lib/components/` | **layer 4 (UI)** | UI is the project service layer. |

**Single-source reuse actually performed (SPEC §1.4) — no second implementations:**

| Needed | Reused instead of re-implementing |
|---|---|
| canonical JSON + SHA-256 | `obase.canonical_json` / `obase.sha256_hash` (verified byte-identical to `obase` in a test) |
| webhook HMAC | `obase.webhook.sign_payload` (verified against `obase` in a test) |
| PII redaction | `obase.redact_value` after identity-key stripping (obase masks values, not key names, so it alone cannot satisfy §5.5.1) |
| receipt deep links | `veya.oprim.receipt_query.receipt_uri`, imported by the oskill — not re-derived |
| CI check polling | the **existing** W3 `server/routes/review.py::_fetch_failing_checks`, injected — `gh` is never invoked twice |
| ACP stdio JSON-RPC | **promoted** `server/acp_client.py`'s plumbing to `veya/obase/acp_stdio.py` (canonical) and rewrote `ACPBackend` to delegate; guard test `TestAcpStdioSingleSource` |
| ACP agent detection | the **existing** W1 `runtime/coding/cli_agents.CLIAgentRegistry` + `server/backends.py` |
| routine dispatch | the **existing** `server/goal_run/routine_dispatch.dispatch_trigger` + `RoutineRegistry` |
| approval | the **existing** `ActionGatewayAdapter` / `SideEffectLedger` (via `ApprovalBridge`) |
| group storage | the **existing** P2 `GroupStore` / `BotRegistry` |
| event persistence | the **existing** `server/events.py` (`EventEnvelope` + JSONL) |

---

## 3. What was built

### Element 1 — branch as room
`POST /api/v1/nano-w9/branch-rooms/{open,publish}`, `GET /branch-rooms`, `veya room {open,publish,list}`.
A branch room is an ordinary P2 `GroupChat` named `branch:<name>` — `GroupChat` has no
`kind` column and adding one would be a schema change to frozen P2, so the prefix is the
convention and `list_branch_rooms()` recognises it. Publishing posts commits, CI results
(W3 `gh` polling, injected), line annotations (W3 store, injected), the optional AI
first-pass review, and the merge decision. **Every posted section yields a citation**, and a
source that is *not* injected is recorded `skipped` — in particular an absent CI checker can
never post "all checks green" (that was a real defect caught by the tests and fixed).
Phases run through `SequentialComposerEngine`.

### Element 2 — event-triggered routines
`POST /api/v1/nano-w9/signals`, `veya routine signal {webhook,message,reaction}`.
Webhook (HMAC, per-routine secret), message (regex/mention) and reaction (e.g. 👍) triggers.
Matching is pure; the mechanism is the **existing** `oservi.TriageEngine` (`on_signal`)
assembled via `ServiceManifest`; dispatch goes to the **existing** routine registry/dispatch.
**No new scheduler and no new task store.**
Reaction-as-approval does **not** approve anything: it raises a request through the existing
action-gateway bridge, so W9 holds no approval state.

### Element 3 — ACP members
`GET/POST /api/v1/nano-w9/acp/members`, `POST /acp/turn`, `veya acp {members,add,turn}`.
An ACP agent becomes a member by being registered as a `BotSpec` with `runner_id="acp:<id>"` —
**no new member store**. Detection reuses W1's CLI registry. Turns are forwarded through the
existing `project_run_goal`; **ACP is never an authority** (it cannot schedule, approve, or
start anything — `test_turn_is_forwarded_through_goal_run` asserts it cannot even dispatch).

### Element 4 — hash-chained audit
`GET /api/v1/nano-w9/audit/verify`, `veya audit {verify,records}`.
`hash_i = hex(sha256(canonical_json({**event_i, "prev_hash": hash_{i-1}})))`, chained over group
messages, tool side-effects, approvals and git events. The three chain columns ride on the
**existing** `server/events.py` record (inside `payload.audit`) — no new store, no migration.
Verification re-derives every link from the raw records and reports the first broken index; a
test tampers with a stored payload and asserts the break is found at the right index.

### Element 5 — search with receipts
`POST /api/v1/nano-w9/search`, `veya search`.
Unified over sessions (W2 FTS), GoalRun results, group messages, approvals and annotations.
Every answer line carries `[source] ref — title <uri>`. When nothing matches the answer says so
explicitly (`cited: false`) rather than asserting something unsourced. A receipt that shares **no
term** with the query is filtered out — without that rule a high-prior source (an approval) would
answer *any* question on source weight alone (defect found and fixed by the tests).

### Element 6 — `scripts/veya_cli.py`
19 commands across 10 groups (flags, chat, room, goal, routine, search, audit, trajectory, acp,
cli-gen). In-process by default (no server needed) or HTTP via `--base-url` with
`Authorization: Bearer $VEYA_TOKEN` — the existing token mechanism.

### Addendum D — CLI-Anything conventions + generated `SKILL.md`
`--json` on every command with one stable envelope on stdout; errors are
`{"status":"error","error":{"code",...}}` with a non-zero exit; bare invocation opens a REPL
(including `help <group>`); `--help` is self-describing.
`skills/veya-cli/SKILL.md` is **generated** from the live argparse tree by
`scripts/gen_veya_cli_skill.py`, and `test_skill_md_is_in_sync_with_the_command_tree` +
`test_generator_check_mode_passes` fail on drift (and `test_generator_detects_drift` proves the
detector works). Uses stdlib `argparse` — `click` is not a declared runtime dependency, so it
was not added.

### Addendum E — preview + trajectory + replay
`POST/GET /api/v1/nano-w9/trajectory`, `veya trajectory {record,replay}`, and
`apps/web/src/lib/components/TrajectoryReplay.svelte`. Trajectory entries
`{step, command/tool, artifact_ref, preview_ref, ts}` land on the **existing**
`server/events.py` topic `trajectory.recorded`. Assembly/replay are pure; persistence is
layer 4. The Svelte component is standalone (not yet mounted in a route) so the frontend
surface is additive and reversible.

### Addendum F — agent-native CLI generation
`POST /api/v1/nano-w9/cli-gen`, `veya cli-gen`. Seven omodul phases composed by the **existing**
`SequentialComposerEngine` through a `ServiceManifest`, with a layer-4 router that carries each
phase's artifacts forward (this is why no phase imports a sibling omodul). Default model is the
frozen strongest proxy `veya1.2`. Marked `experimental` in every response; `VEYA_CLI_GEN` default OFF.

---

## 4. New model-callable tools

**None.** No entry was added to `server/tool_registry.py`. This is asserted by
`TestThreeOLayerContract::test_no_new_model_callable_tool_was_registered`, which scans the
registry source for any W9 symbol. Everything W9 exposes is an HTTP endpoint for the existing
shell/UI or a CLI command.

---

## 5. Feature flags (all default OFF)

| Flag | Default | Guards |
|---|---|---|
| `VEYA_BRANCH_ROOMS` | off | element 1 (enforced in the layer-4 facade *and* the route, so CLI and HTTP cannot disagree) |
| `VEYA_ROUTINE_TRIGGERS` | off | element 2 |
| `VEYA_ACP_MEMBERS` | off | element 3 |
| `VEYA_AUDIT_CHAIN` | off | element 4 |
| `VEYA_CLI_GEN` | off | addendum F |
| — | on | element 5 (search) and element 6 (CLI): read-only surfaces, which the spec permits |

An off flag produces `{"status":"error","error":{"code":"FEATURE_DISABLED","flag":"..."}}` and a
non-zero exit (CLI) / HTTP 404 with the same body (route) — never a crash, never a silent no-op.
`test_gated_routes_404_when_the_flag_is_off` parameterises **all nine** gated routes.

---

## 6. Files

**New — 3O layers**
- `veya/oprim/audit_chain.py`, `veya/oprim/receipt_query.py` (+ `veya/oprim/__init__.py` re-export)
- `veya/oskill/pure/trigger_match.py`, `receipt_rank_merge.py`, `trajectory_assemble.py`
  (+ `veya/oskill/pure/__init__.py` manifest, version `0.2.0 → 0.3.0`)
- `veya/obase/acp_stdio.py` (+ `veya/obase/__init__.py` manifest entry)
- `veya/omodul/branch_room_sync.py`, `veya/omodul/agent_native_scaffold.py`

**New — layer 4**
- `server/nano_w9/{__init__,flags,branch_rooms,routine_triggers,acp_members,audit_chain,search_receipts,trajectory,cli_gen}.py`
- `server/routes/nano_w9.py` (+ 2 import/include lines in `server/app.py`)
- `scripts/veya_cli.py`, `scripts/gen_veya_cli_skill.py`, `skills/veya-cli/SKILL.md`
- `apps/web/src/lib/components/TrajectoryReplay.svelte`

**Modified**
- `server/acp_client.py` — now delegates its stdio transport to `veya/obase/acp_stdio.py`
  (single-source convergence; −85 net lines, no behaviour change, covered by a fake-agent subprocess test)
- `veya/oprim/__init__.py`, `veya/obase/__init__.py`, `veya/oskill/pure/__init__.py` — exports/manifests only

**Tests**
- `tests/test_nano_w9_buzz.py` (77 tests)
- `tests/test_nano_w9_cli_harness.py` (36 tests)

---

## 7. Tests run + results

Only the spec-named files were run (one or two per pytest call). The full `tests/` tree was
**never** run (OOM, per HARD RULES).

### W9 suites

| Command | Result |
|---|---|
| `pytest -q tests/test_nano_w9_buzz.py` | **77 passed** |
| `pytest -q tests/test_nano_w9_cli_harness.py` | **36 passed** |
| `pytest -q tests/test_nano_w9_buzz.py tests/test_nano_w9_cli_harness.py` | **113 passed** (cross-file run, no ordering coupling) |

### Regression set (spec-named) — compared against the base commit

Baseline measured by checking out `468f4c1d` into a scratch worktree and running the same
commands, because several of these failures pre-date W9.

| Test file | Base `468f4c1d` | HEAD (with W9) | Delta |
|---|---|---|---|
| `tests/test_lorca_p1_bots.py` + `tests/test_lorca_p2_groups.py` | 3 failed, 15 passed, 10 errors | **3 failed, 15 passed, 10 errors** | none |
| `tests/test_p3a_bot_isolation.py` | 1 error | **1 error** | none |
| `tests/test_product_shell.py` | 4 failed, 9 passed | **4 failed, 9 passed** | none |

**Zero regressions introduced.** The pre-existing failures are environmental, not behavioural:
`platform/3O/oskill` and `platform/3O/oservi` are **symlink loops** in this checkout
(`platform/3O/oskill -> /data/soffy/projects/veya/platform/3O/oskill -> …`), so
`veya.platform.load("oskill")` raises `RuntimeError: 3O main library 'oskill' is not mounted`.
`obase`, `oprim` and `omodul` resolve normally. W9 code degrades gracefully in this situation
(every `veya.platform.load` is wrapped), which is why the W9 suites run green.

### Frontend (spec: exactly one check + one build)

| Command | Result |
|---|---|
| `cd apps/web && NODE_OPTIONS=--max-old-space-size=2048 pnpm run check` | **0 errors**, 31 warnings in 2 files — all pre-existing; `TrajectoryReplay.svelte` contributes **0** |
| `cd apps/web && NODE_OPTIONS=--max-old-space-size=2048 pnpm run build` | **succeeded** |

(`pnpm install --frozen-lockfile` was required first: `node_modules` was absent in the worktree.)

### Lint / types

| Command | Base | HEAD | Delta |
|---|---|---|---|
| `ruff check .` | 83 errors | 83 errors | **0 new** |
| `ruff check <all W9 files>` | — | **All checks passed** | — |
| `ruff format --check <all W9 trees>` | — | **90 files already formatted** | — |
| `mypy --config-file pyproject.toml --follow-imports=silent <W9 modules>` | — | **Success: no issues found in 15 source files** | — |

---

## 8. 3O gates

```
check_no_reverse_dep.py . --baseline …   exit 0   PASS
check_oskill_pure.py    . --baseline …   exit 0   PASS
check_no_direct_io.py   . --baseline …   exit 1   FAIL — 60 pre-existing, 0 from W9
check_3o_gates.py --strict               exit 1   FAIL — same two pre-existing buckets
pytest -q tests/guardians/test_3o_migration_guards.py   9 passed, 1 failed (pre-existing)
```

**Zero NEW violations — proven, not asserted.** The direct-IO finding list captured before any
W9 change and after is byte-identical:

```
$ diff <(grep '\[FAIL\]' /tmp/gate_base.txt | sort) <(grep '\[FAIL\]' /tmp/gate_w9b.txt | sort)
IDENTICAL
$ grep -E 'nano_w9|audit_chain|acp_stdio|branch_room_sync|agent_native_scaffold|veya_cli' /tmp/gate_w9b.txt
NONE
```

Bucket sizes, base vs HEAD:

| Bucket | Base `468f4c1d` | HEAD | Δ |
|---|---|---|---|
| `OPRIM_PEER_CALLS_VIOLATIONS` | 0 | 0 | 0 |
| `OPRIM_PEER_CALLS_INFO` | 10 | 12 | +2 (package re-exports of the two new oprims — INFO only, not violations) |
| `OMODUL_DIRECT_CALLS_VIOLATIONS` | 0 | 0 | 0 |
| `DIRECT_IO_NEW_VIOLATIONS` | 60 | 60 | **0** |
| `DIRECT_MEMORY_WRITE_VIOLATIONS` | 0 | 0 | 0 |
| `ACTION_GATEWAY_BYPASS_VIOLATIONS` | 4 | 4 | **0** |

`tests/guardians/test_3o_migration_guards.py` fails only
`test_direct_io_checker_passes_with_baseline`, which asserts the direct-IO gate is green — i.e.
it is a *symptom* of the same pre-existing drift. It fails identically on `468f4c1d`
(verified in a scratch worktree: `1 failed, 9 passed` on both).

Additional 3O contract tests added in `TestThreeOLayerContract`:
pure-module purity (`3O-PURE` marker, no IO/non-deterministic imports, `__all__` present);
the two-phase omodul signature is exactly `(config, input_data, output_dir)`;
`_enabled_pillars` is declared at class level and non-empty;
`branch_room_sync` imports **no** sibling omodul;
no 3O layer imports a business root (`server`/`agents`/`cli`/`tools`);
no W9 symbol in `server/tool_registry.py`.

### Why `--strict` is still red — and why it was not made green by fiat

The 60 direct-IO findings and the 4 action-gateway bypasses are **pre-existing drift**:
`scripts/baseline_direct_io.txt` was last regenerated in `bc7b52c7`, and everything since then
has accumulated findings. They are unrelated to W9 and sit in frozen paths
(`server/routes/review.py`, `server/sse.py`, `server/dsh_runtime.py`, `server/goal_run/routine_dispatch.py`, …).

Making `--strict` green would require baselining 60 findings written by code nobody on this
branch reviewed. That is the "do not silently reset a baseline" anti-pattern, and it would let
genuine future regressions hide in a 366-entry allowlist. Per *Working Principles* ("No
drive-by changes — mention them, don't fix them") the baseline was **left untouched** and the
pre-existing debt is reported here instead.

**Remediation (owner decision, not taken here):** either (a) regenerate
`scripts/baseline_direct_io.txt` in a dedicated commit that lists each of the 60 additions with
an owner and a remediation note, or (b) route the `server/goal_run/*` and `server/routes/*`
findings through `veya/oprim` + `# 3O-IO-ALLOW` markers as already done for
`tools/harness/control_harness.py`. Until then `check_3o_gates.py --strict` exits 1 both before
and after W9 — **the delta attributable to W9 is exactly zero.**

---

## 9. Gaps and honest limits

1. **`check_3o_gates.py --strict` exits 1** (pre-existing, §8 above). Not caused by W9; not
   silenced by W9 either.
2. **Three of the 3O submodules are unusable in this checkout** (`oprim`, `oskill`, `oservi`
   are symlink loops). Every W9 code path degrades gracefully and is tested in that state, but
   `SequentialComposerEngine` / `TriageEngine` / `ResearcherEngine` assembly could only be
   verified by manifest construction, not by executing a real engine. The fallback path is
   exercised and asserted (`engine: "sequential_fallback"` in the test output).
3. **`TrajectoryReplay.svelte` is not mounted in a route.** It type-checks and builds, and the
   data endpoint is live, but no page renders it yet — mounting it means picking a goal-detail
   surface, which is a product decision outside this scope.
4. **AI first-pass review is an injected seam, not a wired model call.** The omodul accepts an
   `llm_caller` and accounts its `cost_usd`; layer 4 does not inject one by default, so the step
   is recorded `skipped` until a caller is supplied. Wiring it to `veya1.2` would be a routing
   decision the frozen architecture reserves for `MasterAgent`.
5. **`fetch_commits` uses `veya.oprim.git.git_log`** and returns an empty list on any error;
   no commit-sync daemon exists. Branch rooms show commits only when the caller passes a
   `repo_path` and git is available.
6. **Reaction-as-approval builds an approval *request***; the decision still has to be resolved
   by the existing user-approval flow. That is deliberate — W9 must not become a second
   approval system — but it means a 👍 alone never completes an action.
7. **No push, no service restart, no dev server** was performed, per HARD RULES.