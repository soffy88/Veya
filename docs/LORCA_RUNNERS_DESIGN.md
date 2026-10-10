# Lorca P6 design — run a bot's turns on another machine (runners)

Status: chosen design, approved for implementation on `feat/lorca-runners`.
Nothing was read or imported from `/data/soffy/projects/veya`, and no code was
copied from `egoist/lorca` (GPL-3.0). Every reuse below is code already
committed on this branch.

## 1. What already exists (investigated first)

| existing code | what it gives us | reuse decision |
|---|---|---|
| `server/routes/remote_mcp.py` | the canonical shape of a remote HTTP surface in this repo: transport only, everything else in `veya/remote/*`; `GET /mcp/health` is a no-secret readiness probe | copied as a *pattern* (thin router, secrets never in the body/logs), not as a protocol |
| `veya/remote/auth.py` — `RemoteAuth` | bearer auth with SHA-256-digested secrets (the raw token is never persisted or logged), `hmac.compare_digest` per candidate with no early exit, **fail closed when nothing is configured**, first-class revoke/rotate, plus an OAuth access-token path | **reused as-is** for the runner-facing turn endpoint. No new auth scheme, no new token store. |
| `veya/remote/models.py` — `RemotePermissions` | explicit capability grant (`read/write/shell/git/network/destructive/service_control`) | the runner turn endpoint requires `write` on the presenting token (fail closed, operator-visible) |
| `server/app.py:505` — `GET /health` | a readiness probe with no auth and no secrets on the root app (and therefore on `veya/server/app.py`, which reuses the same app object) | the health check target: `GET {base_url}/health` |
| `server/routes/bot_team.py` + `server/bot_team/turns.py` | the product path for a bot turn: `_open_task_projection` → `_run_product_task` → `master_coordinator.execute_structured` → GoalRun | the runner side calls exactly this; the hub side dispatches into it |
| `server/dsh_runtime.py`, `server/opencode_client.py` | the repo's established httpx client style (async, explicit timeout) | followed |

No new protocol is invented: a runner turn is one authenticated JSON POST.

## 2. What is built

```
hub (keeps the catalog)                        runner (executes the turn)
──────────────────────                         ────────────────────────
POST /api/v1/bots/{id}/messages
  └─ run_bot_turn(bot)                         POST {runner.base_url}/api/v1/runners/turn
       ├─ runner_id == "local"  → local path        Authorization: Bearer <resolved token_ref>
       │    (unchanged: product shell               └─ run_bot_turn(bot from payload)
       │     → MasterAgent → GoalRun)                   → same product shell → MasterAgent
       │                                                → the runner's own GoalRun
       └─ runner_id != "local"  → runner_client
            forwards, retries once, times out        returns {reply, status, task_id, session_id}
            and records a visible error turn
```

### New modules

* **`server/bot_team/runners.py`** — the runner registry.
  `RunnerRecord {runner_id, name, base_url, token_ref, last_seen, status}`,
  persisted as JSON at `~/.veya/runners.json` (`VEYA_RUNNERS_PATH` overrides).
  `"local"` is implicit and never stored: it means *this server*. Token refs are
  **an environment variable name or a file path** — the only secret material
  that is ever persisted. `resolve_token_ref()` reads env first, then the file,
  and returns `""` when neither holds anything; the raw value never leaves that
  function. `last_seen`/`status` are written only by a health check (the one
  place a runner report exists); an unreachable runner keeps its record.
* **`server/bot_team/runner_client.py`** — the forwarding client.
  A `RunnerTransport` protocol (`post`/`get` → `TransportResponse`) with one
  production implementation (`HttpxTransport`) so tests can inject a fake
  transport; the client never reads a socket itself. `forward_bot_turn()`
  builds the payload, applies the per-request timeout
  (`VEYA_RUNNER_TIMEOUT_S`, default 300 s), retries **once** on a transport
  failure or a 5xx (a 4xx is a refusal and is not retried), maps a failure to a
  visible error turn, and records both sides of the exchange in the hub's
  conversation store. `redact_secret()` scrubs the resolved token out of every
  error string, including httpx exception text.
* **`server/routes/runners.py`** — two things in one router:
  * `/api/v1/runners` management: `GET ""` (list, always includes the implicit
    `local`), `POST ""` (add; rejects `local` and duplicate ids), `DELETE
    /{runner_id}`, `POST /{runner_id}/health` (GET the runner's `/health` with a
    timeout, then write `last_seen`/`status`), `POST /{runner_id}/bots/{bot_id}`
    (set a bot's `runner_id`, 404 when the runner is unknown, 404 when the bot
    is unknown). Same auth as `bots.py` (`Depends(auth_mod.get_current_user)`).
  * `POST /api/v1/runners/turn` — the endpoint a remote hub calls. Bearer token
    verified by the reused `RemoteAuth`, fail closed; requires `write`; runs the
    turn locally through `run_bot_turn` (the normal MasterAgent → GoalRun path)
    and returns the reply. It is declared before the `/{runner_id}` paths so a
    static segment wins, the same trick `bots.py` uses for `/groups`.
* **`server/bot_team/turns.py`** — one additive dispatch at the top of
  `run_bot_turn`: when `bot.runner_id` is set and is not `"local"`, the turn is
  forwarded instead of executed. `trace_id` becomes an optional parameter so a
  forwarded turn can carry the hub's trace id into the runner's task projection.

### `/api/v1/runners/turn` contract

Request: `{version: 1, runner_id, trace_id, user, workspace_id, bot{...}, text,
context}`. The **hub** owns the bot catalog, so the payload carries the bot's
identity/instructions/LLM binding rather than a bare id: the runner does not
need the bot in its own registry to execute it faithfully.

Responses: `200 {"ok": true, "turn": {...}}`; `401` bad/missing token; `403`
token without `write`; `422` malformed payload; `502` the local turn raised.

## 3. Routing and failure semantics

* No runner configured → `BotSpec.runner_id` defaults to `"local"` → the
  existing code path runs byte-for-byte as before. Default behaviour unchanged.
* One bot's turn is atomic per runner: it either runs on the runner or it
  produces an error turn. There is no silent fallback to local execution — a
  bot whose runner is offline must look offline, not quietly become local.
* The error turn names the bot, the runner id, the `base_url` and the reason,
  and says nothing was executed. It is stored in the hub's conversation, so the
  user sees the failure in the transcript.
* Execution authority stays with the runner's own GoalRun. The hub creates no
  task, no session and no store for a forwarded turn: the `task_id` /
  `session_id` it returns are the **runner's**, and the hub's `workbench_url` is
  therefore only emitted for a local run.

## 4. Frozen authorities

No second scheduler, no second task store, no new executor, no change to
`MasterToolRegistry` (a bot messaging another machine's bot is still the
existing `message_bot` service + REST, not a model-visible tool). Edits to
existing files are three one-line-ish additions (`server/app.py` router
include, `turns.py` dispatch, `bots.py` response fields) plus the runner_id
validation on `PATCH /api/v1/bots/{id}`.

Secrets: the runner record stores a **ref**; the resolved secret lives only in
the request header of the outbound POST and in the `RemoteAuth` verification on
the runner. No secret is written to a repo file, a response body, or a log line.
