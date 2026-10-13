# W13 — Cross-device agent control — report

Branch: `feat/w13-remote` · Spec: `W13_REMOTE_CONTROL_SPEC.md` · Brief: `W13_BRIEF.md`
Status: **Phase A (backend) complete; Phase B (web) complete.**
Both phases are behind `VEYA_REMOTE_CONTROL`, **default OFF**.

---

## 1. What shipped

Drive agents on `ubuntu26` and other tailnet hosts from a phone or browser:
start a task (text + voice), watch sessions, cancel, hand a task to another
agent with context, get notified on done / needs-approval, and exchange files
behind expiring recipient-bound links. W13 adds **no** authority of its own — it
is a projection over the flag registry, `atomic_write_json`, `ApprovalService`,
`RoutineRegistry`/`RoutineClock`, the goal_run verifier and the notification
centre.

### 1.1 Flag and environment surface

| Name | Default | Meaning |
|---|---|---|
| `VEYA_REMOTE_CONTROL` | `False` | The one feature flag (registered in `server/feature_flags.py`, appended after W12). Unregistered → treated as off. |
| `VEYA_REMOTE_CONTROL_HOSTS_PATH` | `~/.veya/remote_control_hosts.json` | Host registry ledger. |
| `VEYA_REMOTE_CONTROL_SESSIONS_PATH` | `~/.veya/remote_control_sessions.json` | Session ledger. |
| `VEYA_REMOTE_CONTROL_WAKE_PATH` | `~/.veya/remote_control_wake.json` | Wake-policy settings. |
| `VEYA_REMOTE_CONTROL_FILES_PATH` | `~/.veya/remote_control_files.json` | File-handoff ledger (metadata only). |
| `VEYA_REMOTE_CONTROL_POLICY_PATH` | `~/.veya/remote_control_policy.json` | Identity policy. |
| `VEYA_REMOTE_CONTROL_LINK_TTL` | `86400` | Attachment link lifetime (s). |
| `VEYA_REMOTE_CONTROL_MAX_BYTES` | `33554432` | Max staged attachment size. |
| `VEYA_REMOTE_CONTROL_RATE_LIMIT` | `5` | Wakes per sender+agent per window. |
| `VEYA_REMOTE_CONTROL_RATE_WINDOW` | `300` | Rate-limit window (s). |
| `VEYA_REMOTE_CONTROL_BATCH_INTERVAL` | `15` | Default `every Nm` for batched wakes. |

### 1.2 New model-callable tools (`server/remote_control/tools.py`)

Registered **only when the flag is on** (registration-time gate, wired in
`server/coordinator_master.py` next to `approval_tools`). Caller identity is
`current_bot_id()` → `BotRegistry` owner — **never** a model argument.

| Tool | Side effect | `effect_capability` | Approval |
|---|---|---|---|
| `remote_host_list` | `pure_read` | `none` | — |
| `remote_agent_start` | `external_mutation` | `manual_only` | `approved:true` required |
| `remote_agent_cancel` | `external_mutation` | `manual_only` | `approved:true` required |
| `remote_agent_handoff` | `external_mutation` | `manual_only` | `approved:true` + approved record |
| `remote_task_status` | `pure_read` | `none` | — |

### 1.3 REST surface (`server/routes/remote_control.py`)

Prefix `/api/v1/remote`, mounted unconditionally in `server/app.py`; every
endpoint 404s while the flag is off (`require_remote_control`). Every handler
takes `Depends(auth_mod.get_current_user)`; a record owned by another user is
**404, never 403**.

| Method | Path |
|---|---|
| `GET` | `/api/v1/remote/hosts` |
| `POST` | `/api/v1/remote/hosts` |
| `DELETE` | `/api/v1/remote/hosts/{host_id}` |
| `GET` | `/api/v1/remote/sessions` |
| `POST` | `/api/v1/remote/sessions` (202; records accepted + pending approval) |
| `GET` | `/api/v1/remote/sessions/{session_id}` |
| `POST` | `/api/v1/remote/sessions/{session_id}/apply` |
| `POST` | `/api/v1/remote/sessions/{session_id}/cancel` |
| `POST` | `/api/v1/remote/sessions/{session_id}/handoff` |
| `POST` | `/api/v1/remote/files` |
| `GET` | `/api/v1/remote/files/{token}` |
| `GET` | `/api/v1/remote/pending-approvals` |
| `GET` | `/api/v1/remote/wake-policy` |
| `PUT` | `/api/v1/remote/wake-policy` |

`/apply` is the one addition to the spec's list (§12): it promotes an
accepted session to `woken` (and launches) once its approval is resolved as
approved — without it an approved session could never leave `accepted`.

### 1.4 Web (`apps/web`)

`src/routes/remote/+page.svelte` (mobile-first) + `src/lib/remote/api.ts`, plus
a sidebar link in `src/routes/+page.svelte`. Hosts, running agents, start-task
form (text + `webkitSpeechRecognition`, progressive enhancement), approve/deny
cards resolved through the existing `/api/v1/approvals/{id}/resolve`, done
notifications through the app-wide notification stream.

---

## 2. Review of the previous worker's WIP (`server/remote_control/`, 13 modules)

The WIP was kept where correct and rewritten where not. Fixes, all now covered
by tests:

| File | Finding | Fix |
|---|---|---|
| `approval.py` | `DENIED_DECISIONS` used `"denied"`; the store's vocabulary is `deny` (`veya/obase/approvals.py`) — a denied approval read as **approved** | Use `deny` |
| `service.py` | `request_start` called the `async` `_request` **without `await`** → `TypeError` at runtime | Made `request_start/cancel/handoff` `async` |
| `service.py` | `_sync_request` raised whenever a loop was running (i.e. always on the REST path) | Removed; all mutation paths are `async` |
| `service.py` | hand-off target taken from the apply-time argument → an approved hand-off could be **redirected** | Target fixed at request time; `handoff()` takes no target |
| `service.py` | `launched` used the process exit code (a long agent with `returncode=None` read as "not launched") | `launched = pid is not None` |
| `service.py` | `_launch` moved `accepted → woken → acked`; a failed launch left no honest record | Caller records `woken` first; failure marks `failed` |
| `sessions.py` | `mark_done` from `accepted` was **allowed** (only forward-only was enforced) | Stepwise order: `done` needs `acked`, `acked` needs `woken` |
| `wake.py` | quiet hours **dropped** a `now` wake (returned `batched` but left `effective=now`) | Downgrade `now → batch`; rate-limit `0` = unlimited (was always-reject) |
| `wake.py` | routine id embedded raw `sender|agent` (`\|`, `:`, `@`); `goal_template` was a dict, `objective` empty | Charset-safe slug; `goal_template` is a JSON string |
| `wake.py` | `disable_batch` used the wrong registry bot scope | Takes the owning `bot_id` |
| `notify.py` | approval card used the right `request_id` field — **no bug**; only lifted it into a local for readability | (cosmetic) |

---

## 3. Verification (evidence)

Commands run on this branch; all green.

| Check | Result |
|---|---|
| `pytest tests/test_w13_remote_core.py` | **29 passed** |
| `pytest tests/test_w13_remote_service.py` | **21 passed** |
| `pytest tests/test_product_shell.py` | **13 passed** |
| `pytest tests/test_w12_supervisor.py` | **21 passed** |
| `scripts/check_no_direct_io.py . --baseline scripts/baseline_direct_io.txt` | **61 new** (== baseline; **0 from W13**) |
| `scripts/check_3o_gates.py --strict` | `ACTION_GATEWAY BYPASS_VIOLATIONS=5`, `DIRECT_MEMORY_WRITE_VIOLATIONS=0` (== baseline; **0 from W13**) |
| `pnpm --dir apps/web check` | **0 errors** (32 pre-existing warnings) |
| `pnpm --dir apps/web build` | **succeeds** |
| Real app mount: flag off → `404`; flag on → `200` | verified via `TestClient(server.app.app)` |

Tailscale is always mocked (the `identity.resolve_identity` seam takes an
injectable runner); no test forks a process or touches a real peer.

**3O / direct-IO posture:** the only physical I/O in the package is
`server/remote_control/_ledger.py` (reads + `atomic_write_json`) and
`server/remote_control/process.py` (the single `subprocess` seam), each with an
explicit `# 3O-IO-ALLOW` rationale. Both gates report the pre-existing baseline
counts unchanged.

**Licence hygiene:** Paseo (AGPL-3.0) was **clean-room** — no file opened, no
code or prose copied. The three Yobiru (MIT) designs are recorded in
`THIRD_PARTY_NOTICES.md`.

---

## 4. What is left / known gaps

1. **Real `tailscale whois` is never exercised by the suite.** The concrete
   runner (`process.run_tailscale`) is correct but only the mock path is
   tested. First run against a live tailnet is unverified.
2. **`ssh` transport needs a working `sshd` + key** on the target; only the
   `local` transport is exercised end-to-end. The `ssh` argv shape is unit-tested.
3. **No HTTP runner transport.** `executor.py` documents the existing
   `server/bot_team/runner_client.py` as a third transport; not implemented.
4. **Session/file pruning is not scheduled.** `sessions.prune_finished` and
   `files.prune_expired` exist but nothing calls them on a cadence; wiring them
   into the routine clock is a follow-up (kept out to avoid a second timer).
5. **`remote_agent_handoff` tool** resolves its session from the approval record;
   the direct REST hand-off path is fully tested, the tool path is covered by
   the approval-contract tests but not by an end-to-end tool call.
6. **Voice input** relies on `webkitSpeechRecognition` (Chrome/Safari); the text
   box is the fallback and works everywhere.
7. **`apps/web` `node_modules`** was installed to run `pnpm check`/`build`; it is
   gitignored and not committed.
8. **Batched-wake cadence**: a batch fires on the routine clock's own tick
   (default 30 s); the `every Nm` interval is the routine's schedule, so a batch
   set to 15 m fires at the first tick after 15 m, not exactly on the minute.
