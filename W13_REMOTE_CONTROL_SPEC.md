# W13 — Cross-device agent control

Status: **implementation-ready** (expanded from `W13_REMOTE_CONTROL_SPEC_DRAFT.md`, 2026-10-13)
Owner: W13 · Branch: `feat/w13-remote` · Flag: `VEYA_REMOTE_CONTROL` (default **OFF**)

---

## 1. Goal

From a phone or a browser tab, drive agents that run on `ubuntu26` and other
tailnet hosts: start a task (including by voice), watch several agents in
parallel, hand a task from one agent to another with its context, and get
notified when an agent finishes or needs approval.

Success for W13 = *Veya is release-ready* (plus a stable brain decision).

## 2. Non-goals

- No second user-facing agent mainline. The frozen ReAct main chain
  (`server/coordinator_master.py` → `oservi.MasterAgent`) is untouched.
- No second scheduler, event store, approval authority, or capability
  registry. Every one of those is reused (§5).
- No program-side semantic router, tool-surface slicing, or URL pre-fetch.
- No cross-machine **agent runtime** work: `veya/remote/*` is another wave's
  WIP and is read-only for W13.
- No new deployment surface beyond the REST routes and tools in §7/§8.

## 3. Inspiration and licence hygiene

| Upstream | Licence | What W13 takes |
|---|---|---|
| `getpaseo/paseo` | AGPL-3.0 | **Clean-room only.** No upstream file was opened, no code and no prose was copied. Nothing in this repository derives from Paseo source. |
| `chekusu/yobiru` | MIT | Three design ideas, reimplemented in Veya's own architecture: machine identity via Tailscale whois (§6), receiver-side wake policy per sender+agent (§9), file handoff with expiring recipient-bound links and size+SHA-256 verification (§10). Recorded in `THIRD_PARTY_NOTICES.md` (the repository's notice file — append-only). |
| `cmux` | n/a (inspiration prose) | The done / needs-approval notification vocabulary and session restore. |

Anything taken from an MIT upstream is credited in `THIRD_PARTY_NOTICES.md`.
Because Paseo is AGPL, the W13 code is written from the requirements in this
document and from Veya's own code only.

## 4. What exists and is reused (the reuse map)

W13 adds **no** authority of its own. Every store, gate and scheduler below
already exists in this repository; W13 is a projection over them.

| Concern | Reused authority | Anchor |
|---|---|---|
| Flag registry | `server/feature_flags.py` (`FLAGS`, `enabled()`), runtime > env > file > default via `config/adapters.feature_flag` | `server/feature_flags.py:22-84` |
| Durable JSON store | `atomic_write_json` (tmp + `fsync` + `os.replace`) | `server/bot_team/conversations.py:50-59` |
| Store shape precedent | `SupervisorBinding` — lock, defensive `from_dict`, corrupt file → empty | `server/supervisor/binding.py:52-174` |
| Approval path | `ApprovalService.request/resolve/get/list_pending` + `/api/v1/approvals/*` | `server/approvals.py:90-200`, `:325-405` |
| Approval gating predicate | `user_control_policy` / `HIGH_IMPACT` / `GATED_MARKERS` | `server/user_control.py:287-295`, `:694-752` |
| Human-only authority | `HUMAN_ONLY_MARKERS` (an agent principal may never resolve these) | `veya/oskill/pure/permission.py:14-23` |
| Tool registry + side effects | `MasterToolRegistry.register(..., side_effect=…, effect_capability=…)` | `server/tool_registry.py:543-601` |
| Side-effect vocabulary | `SideEffect.EXTERNAL_MUTATION` / `SideEffect.PRIVILEGED` | `server/tool_registry.py:125-134` |
| Model tool registration shape | `_TOOLS` tuple + `register_tools(registry) -> int`, flag-gated at registration | `server/approval_tools.py:27-66` |
| Routine (schedule) authority | `RoutineRegistry` + `RoutineClock` (`schedule.trigger`, `every Nm`) | `server/goal_run/routine_registry.py:115`, `server/bot_team/routine_clock.py:468` |
| Existing dispatcher seam (no new scheduler) | `_default_handoff_consumer` interception, same hook W12 uses | `server/bot_team/routine_clock.py:210, 232-233, 343-378` |
| Quiet hours | `is_quiet_hours(settings, now=…)`, `classify_wake` | `veya/oskill/pure/wake_policy.py:47, 178` |
| Completion evidence (verifier) | `verify_task` → `VerifyResult`; `GoalStatus.completed`; `build_progress_report(...)["is_done"]` | `server/goal_run/verify.py:26-75`, `server/supervisor/reporting.py:73-74` |
| Mailbox / delivery bookkeeping | `AgentMailbox.send/poll/acknowledge`, idempotent by `message_id` | `server/goal_run/agent_mailbox.py:89-160` |
| Notification fan-out | `global_notifier.push(type, title, content, payload, user_id)` | `server/notification_center.py:56-63` |
| Device push | `WEB_PUSH_FLAG` + `PushNotifier.observe` observer | `server/notification_center.py:21-27`, `server/bot_team/push.py:550-568` |
| Owner auth | `auth_mod.require_user` (router) + `Depends(auth_mod.get_current_user)` (handler) + 404-not-403 ownership | `server/auth.py:179-205`, `server/routes/supervised.py:54-79` |
| Router mount | `app.include_router(...)` in `server/app.py` (router always mounted, 404 while flag off) | `server/app.py:514, 574`; `server/routes/channels.py:55-69` |

### Gaps that W13 must fill itself (no existing code to reuse)

1. **Machine identity.** Nothing in `server/` addresses a peer by tailnet
   address; `server/bot_team/runners.py:43` and `server/bot_team/conversations.py:29`
   even reject `:` and `@` in ids. W13 therefore adds its own host registry and
   its own identity resolution (§6, §11).
2. **The delivery vocabulary** `accepted → woken → acked → done` does not exist.
   The mailbox's `unconfirmed | delivered | started` is a *different* value
   space and is not reused as-is.
3. **Rate limiting** does not exist anywhere in `server/`. Quiet hours do.
4. `RoutineRegistry` has **no `delete()`** (`server/routes/routines.py:117-132`
   works around it). W13 disables rather than deletes, exactly as W12 does in
   `server/supervisor/handoff.py:113-117`.

## 5. Delivery semantics

A remote task is accepted in stages, and the stages are never conflated.

| Stage | Meaning | Evidence required to enter it |
|---|---|---|
| `accepted` | The request is durably recorded and the sender got HTTP 202. Nothing has run yet. | record persisted |
| `woken` | The receiving side decided to actually wake a local agent, under the wake policy (§9) and after approval was granted. | approval decision `approved` + wake record |
| `acked` | The woken agent demonstrably started the task (first token / tool call / event observed). | `delivery_state = "started"` evidence |
| `done` | Verifier evidence says the work passed. | `server/goal_run/verify.py` `VerifyResult.passed is True`, or `GoalStatus.completed` / `build_progress_report(...)["is_done"] is True` |

`done` may **never** be entered on a timer, on a sender's claim, or on an agent's
self-report. A terminal state that is not `done` is `failed` or `cancelled`,
both of which require no verifier.

Monotonicity: the four states are a strict total order (`accepted` < `woken` <
`acked` < `done`) and every transition is forward-only except
`accepted → cancelled` and `woken → cancelled`. `mark_done` refuses to move a
record that has not reached `acked`, and refuses when the verifier evidence is
absent.

## 6. Identity and policy

### 6.1 Resolution

A peer address is resolved with `tailscale whois --json <ip>`. The call is
wrapped in `server/remote_control/identity.py::whois()` behind a single
injectable seam (`runner: Callable[[Sequence[str]], str]`) so tests mock the
process, never the network. Non-zero exit, unparsable JSON, or output without a
`node` is **unknown identity**.

### 6.2 Policy file

```jsonc
{
  "default": "tailnet",        // "tailnet" | "deny"
  "allowlist": ["ubuntu26", "tag:trusted", "user:alice@example.com"],
  "denylist":  ["tag:ci"],     // deny always wins, regardless of order
  "matchers": [                // optional explicit rules, first match wins,
                               // deny wins inside a rule
    {"match": "host:dev-*", "action": "deny"}
  ]
}
```

Resolution order: `matchers` (first match wins) → `denylist` → `allowlist` →
`default`. **Deny wins** at every level, including when a host appears in both
lists. `default: "tailnet"` means "any resolvable node on my tailnet"; anything
that does not resolve is denied regardless of the default.

Matcher vocabulary: `host:<glob>`, `fqdn:<glob>`, `tag:<tag>`, `user:<login>`.
A bare string in `allowlist`/`denylist` is shorthand for `host:<string>`.

**Unknown identity → deny.** There is no implicit-trust branch.

### 6.3 The trust asymmetry (borrowed from Yobiru)

Being able to post a message to the mailbox **never** implies permission to wake,
execute, or auto-reply. Message delivery and action authority are separate
decisions: `policy_allows(identity, action)` where `action ∈ {"message",
"wake", "execute"}`, and only `{"message"}` is granted by the tailnet default.
`wake` and `execute` additionally require an explicit allowlist/rule match.

## 7. Host registry

Stored with the existing `atomic_write_json` pattern; no `os.replace` in W13
code, no `subprocess`, no `open()`. Path: `VEYA_REMOTE_CONTROL_HOSTS_PATH` or
`~/.veya/remote_control_hosts.json`. The file carries an explicit
`# 3O-IO-ALLOW` header with a rationale, matching `server/supervisor/binding.py:1-8`.

```python
@dataclass
class RemoteHost:
    host_id: str  # ^[a-z0-9][a-z0-9-]{0,62}$
    display_name: str
    tailnet_ip: str
    agent_command: str  # executor command template, see §8
    agent_label: str  # "pi" | "claude-wb" | "codex" | …
    owner_id: str  # the Veya user who may drive it
    workspace_path: str = ""
    enabled: bool = True
    last_seen_at: float | None = None
    created_at: float
```

API: `save_host`, `get_host`, `list_hosts(*, owner_id=None, enabled_only=False)`,
`delete_host`, `touch_seen`. Owner scoping is enforced on every read that a
route or tool performs — the store itself is not an authorisation boundary.

## 8. Remote agent sessions

### 8.1 Executor command template

`agent_command` is an operator-configured template with two placeholders, split
by `shlex.split` (never a shell string):

- `{task}` — the task text, passed as **one** argv element
- `{workspace}` — the host's workspace path, one argv element

Built-in defaults per `agent_label` (`pi`, `claude-wb`, `codex`) are used when
`agent_command` is empty. Rendering is pure (`render_command`) and unit-tested;
`start` additionally refuses any argv element containing a newline or NUL, so a
template can never smuggle a second command.

### 8.2 Operations

| Operation | Route | Approval | Side effect |
|---|---|---|---|
| list running | `GET …/sessions` | no | read |
| start task | `POST …/sessions` | **yes** (`external_mutation`) | declared |
| stream status | `GET …/sessions/{id}` | no | read |
| cancel | `POST …/sessions/{id}/cancel` | **yes** (`external_mutation`) | declared |
| hand off | `POST …/sessions/{id}/handoff` | **yes** (`external_mutation`) | declared |

Cancel and handoff are refused outright when the record is not in a cancellable
state (`woken`/`acked`), so a cancel cannot race a completion.

### 8.3 Handoff with context

`handoff(target_host_id, target_agent_label, note)` carries the source session's
context bundle — task text, state, timestamps, last verifier evidence summary,
and any verified file-handoff references — into a **new** session on the target
host. The source record moves to `handed_off`; the new record starts at
`accepted` and must walk the same state order. Nothing is copied implicitly:
only the listed fields travel, and a handoff whose context contains an unverified
attachment reference is rejected.

### 8.4 Model-callable tools

Registered **only** when the flag is on (registration-time gate, per
`server/approval_tools.py:44`), so a deployment with the flag off does not grow
a model-visible surface it cannot serve:

| Tool | Side effect | `effect_capability` |
|---|---|---|
| `remote_host_list` | `pure_read` | `none` |
| `remote_agent_start` | `external_mutation` | `manual_only` |
| `remote_agent_cancel` | `external_mutation` | `manual_only` |
| `remote_agent_handoff` | `external_mutation` | `manual_only` |
| `remote_task_status` | `pure_read` | `none` |

The three mutating tools additionally declare `approved: boolean` in `required`
and state the refusal contract in their description, matching
`server/github_pr_tools.py:95-165`. Inside the tool body, the approval record is
created through `ApprovalService.request(...)`; a missing `approved` never
produces a remote mutation.

Identity of the caller comes from the running bot (`current_bot_id()`) and its
`BotRegistry` owner — **never** from a model argument.

## 9. Wake policy (receiver side)

Per **sender + agent** key. The sender may *request* `now` / `batch` / `none`;
the receiver decides. Implemented on top of the existing routine registry and
clock — W13 adds no scheduler, no thread, no `asyncio` loop of its own.

Decision inputs, in order:

1. **Policy ceiling** — the operator-set maximum for that sender+agent. A
   requested `now` is downgraded to `batch` when the ceiling is `batch`.
2. **Quiet hours** — `is_quiet_hours(settings, now=…)` from
   `veya/oskill/pure/wake_policy.py:47`. `urgent` bypasses quiet hours, matching
   that module's own rule; nothing in W13 sets `urgent` on its own.
3. **Rate limit** — a per-sender+agent window (`VEYA_REMOTE_CONTROL_RATE_LIMIT`,
   default 5 wakes / 300 s). Counted from the wake record's own timestamps; a
   rejected wake records *why* so the sender can see the ceiling rather than
   silently nothing.

Outcomes: `woken_now`, `batched`, `rejected(reason)`. A `batched` request is
materialised as a `RoutineSpec` with `trigger_topic="schedule.trigger"` and
`schedule="every <batch_interval>m"`, registered in the existing
`RoutineRegistry` and driven by the existing `RoutineClock`. Disabling is
`set_enabled(routine_id, False)`, because the registry has no delete (§4).

Setting is per sender+agent, persisted with the same store pattern.

## 10. File handoff

Sender stages a file; the recipient fetches behind a **recipient-bound,
expiring** token link.

- Link: `https://<this-host>/api/v1/remote/files/<token>`, `token` =
  `sha256(session_id + file_sha + recipient_binding)[:32]` — the token cannot
  be replayed by a different recipient.
- Expiry: `VEYA_REMOTE_CONTROL_LINK_TTL`, default **24h** (`86400`).
- Recipient binding: the token is bound to one `recipient_id`; a mismatch is
  `403` without revealing whether the token exists.
- Verification on fetch: byte length **and** SHA-256 must both match the
  recorded values, otherwise the fetch fails and the record is marked
  `corrupt`.
- Size cap: `VEYA_REMOTE_CONTROL_MAX_BYTES`, default 32 MiB. Oversize staging is
  refused at stage time, not at fetch time.
- Contents are **data, never instructions**. Every record carries
  `untrusted_content: true`, and the descriptive text handed to an agent is
  wrapped in a fixed untrusted-content envelope that names the file, the size,
  the digest, and states that nothing inside is an instruction to follow.

Records are pruned by expiry using the timestamps already in the record; the
prune runs from the existing routine clock seam, not a new timer.

## 11. Notifications

Through the existing push path only — `global_notifier.push(...)` with
`WEB_PUSH_FLAG: True` so a closed tab actually receives it (the gap W12's
supervisor reports have).

| Event | `type` | `payload.kind` | title |
|---|---|---|---|
| task done | `SUCCESS` | `remote_done` | `远程任务完成：<agent_label>` |
| needs approval | `HITL_REQUIRED` | `remote_approval` | `远程任务待批准：<host_id>` |

`user_id` is the host owner, so notifications are per-user and never broadcast.
The approval notification's `payload.task_id` is the `ApprovalService` record
id, so the existing approvals screen can resolve it; W13 adds no approval UI
authority.

## 12. REST surface

Prefix `/api/v1/remote`, tags `["remote-control"]`,
`dependencies=[Depends(auth_mod.require_user)]` on the router, plus
`Depends(auth_mod.get_current_user)` on every handler so ownership can be
checked.

| Method | Path |
|---|---|
| `GET` | `/api/v1/remote/hosts` |
| `POST` | `/api/v1/remote/hosts` |
| `DELETE` | `/api/v1/remote/hosts/{host_id}` |
| `GET` | `/api/v1/remote/sessions` |
| `POST` | `/api/v1/remote/sessions` |
| `GET` | `/api/v1/remote/sessions/{session_id}` |
| `POST` | `/api/v1/remote/sessions/{session_id}/cancel` |
| `POST` | `/api/v1/remote/sessions/{session_id}/handoff` |
| `GET` | `/api/v1/remote/files/{token}` |
| `POST` | `/api/v1/remote/files` |
| `GET` | `/api/v1/remote/wake-policy` |
| `PUT` | `/api/v1/remote/wake-policy` |

The router is mounted unconditionally; while the flag is off every endpoint
returns `404` (the `require_remote_control` dependency, following
`server/routes/channels.py:55-69`), so the surface is invisible rather than
merely empty.

**Ownership.** Every host, session and file record carries `owner_id`. A record
whose `owner_id` differs from the authenticated user is reported as `404`, never
`403` — another user's record must not even be confirmed to exist. Listing
filters by owner; it never filters by "whatever exists".

## 13. Flag and env surface

| Name | Default | Meaning |
|---|---|---|
| `VEYA_REMOTE_CONTROL` | `False` | Registered in `server/feature_flags.py` as W13's flag, appended after the W12 entries (append-only, never reordered). Read through `server.feature_flags.enabled`, which resolves runtime > env > file > default. A `KeyError` (flag not registered) is treated as **off**, so a partial deploy cannot half-enable the surface. |
| `VEYA_REMOTE_CONTROL_HOSTS_PATH` | `~/.veya/remote_control_hosts.json` | Host registry file. |
| `VEYA_REMOTE_CONTROL_SESSIONS_PATH` | `~/.veya/remote_control_sessions.json` | Session ledger. |
| `VEYA_REMOTE_CONTROL_WAKE_PATH` | `~/.veya/remote_control_wake.json` | Wake-policy settings. |
| `VEYA_REMOTE_CONTROL_FILES_PATH` | `~/.veya/remote_control_files.json` | File-handoff ledger (metadata only; payloads stay where the sender staged them). |
| `VEYA_REMOTE_CONTROL_POLICY_PATH` | `~/.veya/remote_control_policy.json` | Identity policy (§6.2). |
| `VEYA_REMOTE_CONTROL_LINK_TTL` | `86400` | Attachment link lifetime, seconds. |
| `VEYA_REMOTE_CONTROL_MAX_BYTES` | `33554432` | Max staged attachment size. |
| `VEYA_REMOTE_CONTROL_RATE_LIMIT` | `5` | Wakes per sender+agent per window. |
| `VEYA_REMOTE_CONTROL_RATE_WINDOW` | `300` | Rate-limit window, seconds. |
| `VEYA_REMOTE_CONTROL_BATCH_INTERVAL` | `15` | Default `every Nm` for batched wakes. |

`server/supervisor/__init__.py:17-28` reads `os.environ` directly and therefore
ignores the config-file layer; W13 deliberately does **not** copy that. It uses
the canonical authority so `feature_flags.snapshot()` and the settings surface
stay truthful.

## 14. 3O / direct-IO posture

- W13 lives in `server/remote_control/` only. `platform/3O/*` is untouched, as
  is `veya/remote/*` (another wave's WIP).
- No `subprocess` import in W13 code. `tailscale whois` is invoked through an
  injected runner whose *default* implementation lives outside `server/remote_control/`
  (`server/remote_control/identity.py` declares the seam; the concrete
  subprocess-backed default is `server/remote_control/tailscale_cmd.py`) and is
  the single place a `# 3O-IO-ALLOW` marker is needed. Tests inject a fake, so
  the subprocess path is never exercised by the suite.
- All four JSON ledgers are written through
  `server.bot_team.conversations.atomic_write_json`; no `open()`, no
  `os.replace`, no `tempfile` in W13 code.
- **Acceptance:** `scripts/check_no_direct_io.py` and
  `scripts/check_3o_gates.py --strict` must show **zero new entries** relative to
  the pre-W13 baseline measured on this branch: `DIRECT_IO_NEW_VIOLATIONS=61`,
  `ACTION_GATEWAY_BYPASS_VIOLATIONS=5`, `DIRECT_MEMORY_WRITE_VIOLATIONS=0`.
  The gates are already red on this branch (pre-existing debt in
  `server/routes/review.py`, `server/goal_run/routine_dispatch.py`, etc.); W13's
  criterion is **61 → 61**, not 61 → 0. The baseline file is not regenerated.

## 15. Phase B — web (`apps/web`)

A mobile-first "Remote" page:

- hosts list with online/offline/never-seen and enabled state
- running remote agents, live-updating status
- start-task form: text plus browser speech-to-text (`webkitSpeechRecognition`,
  progressive enhancement — the text box works without it)
- approve / deny cards fed by the existing approvals list, resolved through the
  existing `/api/v1/approvals/{id}/resolve` route
- done notifications via the existing SSE notification stream

Ships only if phase A is green. `pnpm --dir apps/web check` must report 0 errors
and `pnpm --dir apps/web build` must succeed.

## 16. Tests

`tests/test_w13_*.py`, run one or two files per pytest invocation, together with
`tests/test_product_shell.py` and `tests/test_w12_supervisor.py` (regression for
the surfaces W13 reuses). Tailscale and the network are always mocked — no test
touches a real peer.

Negative-assertion discipline: every "must refuse" rule has a paired positive
case that shows the rule is load-bearing (e.g. a valid SHA-256 that *does*
verify, next to a tampered one that does not), so a test cannot pass by the
assertion never running.

## 17. What W13 deliberately does not do

- No `hi`/keyword routing, no intent classification, no tool-surface curation.
- No auto-reply to a remote sender without an explicit wake policy match.
- No `done` without verifier evidence, ever, including on a clean-looking exit
  code.
- No deletion of routines (the registry cannot delete) and no second scheduler
  to work around that.
