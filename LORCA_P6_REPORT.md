# LORCA P6 report — run a bot's turns on another machine (runners)

Branch: `feat/lorca-runners` (base `feat/lorca-team-groupchat` `dd6071f6`). Work
only in this worktree; nothing was read or imported from
`/data/soffy/projects/veya`, and no code was copied from `egoist/lorca`
(GPL-3.0). Commits: `a5b09c5a` (design), `730c7fff` (registry + REST +
forwarding), `04b062e8` (spec kept in the worktree), `4b9a37a2` (tests).

The previous session on this task was killed (OOM) after committing its
implementation; its commits were intact and this session finished the task
(tests + report) on top of them. Nothing was lost and nothing was rewritten.

## What was built

A bot whose `runner_id` names another machine executes its turns there, over
one authenticated JSON POST, while this server (the hub) keeps the bot catalog,
the conversations and the group transcripts. `"local"` is implicit and means
this server. Design decisions and the investigation of existing code are in
`docs/LORCA_RUNNERS_DESIGN.md`.

**1. Runner registry** — `server/bot_team/runners.py`
`RunnerRecord {runner_id, name, base_url, token_ref, last_seen, status}`,
persisted as JSON at `~/.veya/runners.json` (`VEYA_RUNNERS_PATH` overrides;
same atomic-write pattern as `BotRegistry`). A token is stored only as a
**reference** — an environment variable name or a file path; a raw token is
refused as a `token_ref`, so a 40-character blob can never be persisted.
`resolve_token_ref()` (env first, then file) is the only function that turns a
ref into material, and nothing else in the package ever sees the result.
Refused, never repaired: bad id/url/token-ref, duplicate id, `local`. A corrupt
file degrades to "no runners". An unreachable runner keeps its record; only a
health check writes `last_seen`/`status`.

**2. Forwarding client** — `server/bot_team/runner_client.py`
`RunnerTransport` is a protocol (`post`/`get` → `TransportResponse`) with one
production implementation (`HttpxTransport`), so tests inject a fake transport
and no test opens a socket. `RunnerClient.forward_turn` sends
`POST {base_url}/api/v1/runners/turn` with
`Authorization: Bearer <resolved token>`, applies the per-request timeout
(`VEYA_RUNNER_TIMEOUT_S`, default 300 s), retries **once** on a transport
failure or a 5xx (a 4xx is a refusal and is not retried), and `health()` probes
`GET {base_url}/health` with a 5 s timeout without resolving any token.
`redact_secret()` scrubs the resolved token out of every error string, httpx
exception text included.

**3. Routing** — one additive dispatch at the top of
`server/bot_team/turns.py::run_bot_turn`: `bot.runner_id != "local"` →
`forward_bot_turn`, which returns the same `BotTurnResult` with the **runner's**
`task_id`/`session_id`. Because every product turn (1:1 message, group round,
`message_bot`) goes through `run_bot_turn`, one dispatch point covers all three.
An unreachable runner produces a **visible error turn** (status `error`, names
the bot, the runner id, the `base_url`, says the turn was NOT executed here)
recorded in the hub's conversation — never a silent fallback to local
execution. The hub creates no task, session or store for a forwarded turn:
execution authority stays with the runner's own GoalRun.

**4. REST** — `server/routes/runners.py` (mounted in `server/app.py` with two
router-include lines only)
* `/api/v1/runners` management (`Depends(auth_mod.get_current_user)`, same as
  the bot endpoints): `GET ""` (the implicit `local` runner is always first),
  `POST ""` (add), `DELETE /{runner_id}`, `POST /{runner_id}/health` (probe
  with a timeout, then persist the health projection), `POST
  /{runner_id}/bots/{bot_id}` (bind a bot to a runner or back to `local`).
* `POST /api/v1/runners/turn` — the endpoint a remote hub calls. Bearer token
  verified through the existing `veya/remote/auth.py::RemoteAuth` (fail closed,
  no new auth scheme), requires the `write` permission, rebuilds the bot from
  the payload with `runner_id` forced to `local` (a forwarded turn must never
  forward again), and runs it through the normal `run_bot_turn` → product shell
  → `MasterAgent` → GoalRun path with this machine's own task projection. It is
  declared before the `/{runner_id}` paths so the static `/turn` segment wins,
  the same trick `bots.py` uses for `/groups`.

**5. Guardrails on the existing bot API** — `server/routes/bot_team.py`
`PATCH /api/v1/bots/{bot_id}` refuses a `runner_id` that is not registered
(422), and `POST /{bot_id}/messages` now reports `runner_id` and omits
`workbench_url` for a remote turn (the task lives on the runner).

## Files

New: `server/bot_team/runners.py`, `server/bot_team/runner_client.py`,
`server/routes/runners.py`, `docs/LORCA_RUNNERS_DESIGN.md`,
`tests/test_lorca_p6_runners.py`, `LORCA_P6_REPORT.md`.

Changed: `server/app.py` (two router-include lines), `server/bot_team/turns.py`
(one dispatch + optional `trace_id`), `server/bot_team/__init__.py` (exports),
`server/routes/bot_team.py` (runner_id validation + response fields).

No `server/bot_runtime/`, no `platform/3O` change, no `.venv` in any commit,
no secrets committed. No second scheduler, task store or executor, and no
model-visible tools were added to `MasterToolRegistry` (the tool surface stays
frozen).

## Tests

Exact commands (one or two files per call, as run):

```
./.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_lorca_p6_runners.py
./.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_lorca_p1_bots.py tests/test_lorca_p2_groups.py
./.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_p3a_bot_isolation.py tests/test_p3b_bot_delegation.py
./.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_p3c_multi_bot_coordination.py
./.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_product_shell.py
```

Results:

```
tests/test_lorca_p6_runners.py                     21 passed
tests/test_lorca_p1_bots.py tests/test_lorca_p2_groups.py   28 passed
tests/test_p3a_bot_isolation.py tests/test_p3b_bot_delegation.py   22 passed
tests/test_p3c_multi_bot_coordination.py            3 passed
tests/test_product_shell.py                      2 failed, 11 passed
```

The only failures are the two known pre-existing `test_product_shell` resume
failures documented in `LORCA_P1P2_REPORT.md` (`server/routes/tasks.py::
resume_task` no longer forwards `max_rounds` to `chat_stream`; unrelated to
Lorca, not fixed, out of scope). No new failures.

New coverage (21 tests): registry CRUD with persistence across instances and a
corrupt-file fallback; refusals (bad id/url/duplicate/`local`/raw token); token
refs resolving env-then-file and `token_ref_label` never echoing a secret;
health probe online/offline/non-2xx with a fake transport and the proof it
sends no token; forward payload carrying the whole bot plus the hub's trace id,
bearer header, timeouts, retry-once on transport failure and 5xx, no retry on
4xx; routing local (unchanged product path with the faked
`execute_structured`) vs remote (nothing runs here, the runner's task/session
ids come back); the offline / unknown-runner / unreachable-token error turns
and the retry count; the runner token never reaching a reply, a log line, the
conversation file or the runner file; the runner-facing turn endpoint
(200 with the write token, 401 for no/wrong/unconfigured credentials, 403 for
a read-only token, bot identity and LLM binding reaching MasterAgent); the
management REST surface; and a remote bot message reporting `runner_id` with
`workbench_url` null.

Lint/format and the web check:

```
./.venv/bin/python -m ruff check server/bot_team server/routes/runners.py server/routes/bot_team.py server/app.py tests/test_lorca_p6_runners.py   # All checks passed
./.venv/bin/python -m ruff format --check <same paths>   # all formatted
NODE_OPTIONS=--max-old-space-size=2048 pnpm --dir apps/web check   # svelte-check found 0 errors and 0 warnings
```

## Known gaps

1. **Forwarded turns hold the request open**, exactly like a local bot turn
   (P1's gap): `POST /{bot_id}/messages` returns when the runner answered
   (timeout 300 s). No streaming/202-Accepted variant was added.
2. **No runner monitor.** Health is checked on demand
   (`POST /api/v1/runners/{id}/health`); there is no background prober and no
   status on the bot list beyond `runner_id`.
3. **The management endpoints inherit the bots' auth posture**
   (`get_current_user`, anonymous fallback). Runner *turns* are gated by
   `RemoteAuth` + `write`; the catalog is not.
4. **Group rounds and `message_bot` with remote members** work through the same
   `run_bot_turn` dispatch, but a remote member's turn is sequential within the
   round (each hop is one HTTP round trip), and a remote error turn in a group
   round looks like any member's error turn.
5. **Web frontend untouched.** `apps/web` has no runner UI; the REST endpoints
   are the contract. `svelte-check` passes; no frontend files changed.
6. **Not pushed, not merged** (per the spec).
