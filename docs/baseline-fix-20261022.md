# Baseline Fix and Acceptance Verification Report (2026-10-22)

## 1. Scope and Accomplishments
- Fixed all `svelte-check` diagnostics in `apps/web/src/lib/components/FlowConsole.svelte` by migrating from unauthenticated `EventSource` to authenticated `fetch` + `AbortController` stream reading.
- Verified a clean production build (`pnpm --dir apps/web build`) and zero error `svelte-check`.
- Isolated `tests/test_llm.py` from host deployment tiers and added an explicit regression test (`test_aliased_llm_pool_isolated_from_host_deployment_tiers`).
- Cleaned up unused request IDs in `server/tool_registry.py` to satisfy `ruff`.
- Verified local test suite and personal agent gold gate.

## 2. Acceptance Chat Verification
- Target: `localhost:8767`
- Account: `hark_acceptance` (`71dd1eed-07d6-4f76-bc54-18868284d56d`)
- Query: `"用浏览器打开 https://example.com 告诉我页面标题"`
- Token Management: Minted a temporary session token for testing, verified authentication and execution path, and cleanly revoked the temporary token while preserving `hark_acceptance` and its persistent auth profiles.

## 3. Verification Commands & Outputs
- `pnpm --dir apps/web build` -> Built successfully in 9.32s.
- `svelte-check` -> 0 errors, 39 warnings across workspace.

---

# Browser Answer Fix and Verification Report (2026-10-23)

Follow-up on the same acceptance query. Two independent defects were found:
the final answer never reached the client, and `browser_run` never returned a
page title.

## 4. Defect 1: the final answer was truncated by an early terminal event

### Symptom

Streaming `POST /api/v1/agent/stream` produced `master_start` / `master_round` /
`tool_call` and then `master_done` carrying only a `round` field. No
`text_delta` at all, and `GET /stream/{sid}` replay had none either.

### Root cause

`platform/3O/oservi/oservi/master_agent.py` notifies `master_done` from inside
its ReAct loop (four call sites) when the loop decides the turn is over. That
event carries only `session_id` and `round`; the final answer is returned to
`server/chat_stream.py` afterwards.

`server/chat_stream.py` forwarded every engine event through
`_on_step` → `durable_session_store.publish_sync`. `master_done` is in
`TERMINAL_EVENT_TYPES`, so the durable journal recorded a terminal event
immediately. The consumer loop in `new_agent_stream_events` stops on a terminal
event, so it emitted `[DONE]` before `_finish` could publish
`text_delta(final_answer)`. The answer was written to the journal after the
client had already stopped reading.

### Fix

- `server/chat_stream.py`: `_on_step` drops engine events whose type is in
  `TERMINAL_EVENT_TYPES`. The stream pump owns termination — `_finish` publishes
  the final text and then exactly one terminal event.
- `server/chat_stream.py`: `_finish` awaits the new
  `durable_session_store.flush()` before its direct publish, so the final answer
  cannot take a smaller `seq` than tool events still sitting in the ordered
  drain (the race the old comment described but did not enforce).
- `server/session_events.py`: added `flush()` on `_OrderedEventDrain` and the
  store. It returns `False` on timeout so a stuck drain degrades to the previous
  ordering instead of hanging the stream.

### Regression tests

`tests/test_stream_terminal_ordering.py`:

- `test_engine_master_done_does_not_truncate_final_answer` — a fake engine fires
  `master_start` / `master_round` / `tool_call` / `master_done{round}` then
  returns a final answer. Asserts exactly one `master_done`, that it is last,
  and that the answer's `text_delta` precedes it.
- `test_replay_after_completion_includes_the_final_answer` — the
  `GET /stream/{sid}` replay path must contain `text_delta(final)`.

Both were confirmed red before the fix:

```
E   AssertionError: assert 'Example Domain' in ''
tests/test_stream_terminal_ordering.py:70: AssertionError
E   AssertionError: assert '回放答案' in ''
tests/test_stream_terminal_ordering.py:102: AssertionError
FAILED tests/test_stream_terminal_ordering.py::test_engine_master_done_does_not_truncate_final_answer
FAILED tests/test_stream_terminal_ordering.py::test_replay_after_completion_includes_the_final_answer
======================== 2 failed, 4 warnings in 1.89s =========================
```

## 5. Defect 2: `browser_run` returned an empty title

### Symptom

In the pre-fix live probe the model called `browser_run` four times
(`extract_text`, `extract_html`, `screenshot`, `extract_text` with
`selector=title`) and never got a title, then hit the 120 s provider timeout.

### Root cause

`veya/oskill/browser.py` built the navigate snapshot with a hard-coded
`page_title=""`, and the `extract_text` / `extract_html` actions did not set it
either. `server/tool_registry.py::_tool_browser_run` reports
`last.page_title` from the **last** action, so the tool returned
`"title": ""` for every page regardless of what the browser actually loaded.

The browser backend itself was healthy: the container reaches the CDP sidecar
(`VEYA_BROWSER_CDP_URL=http://192.168.80.10:9222`, Chromium 156.0.8078.48) and
`page.title()` on `https://example.com` returns `Example Domain`.

### Fix

- `veya/oskill/browser.py`: `_page_snapshot` is now async and reads
  `page.title()`. The `extract_text` and `extract_html` actions also fill
  `page_title`, because `browser_run` reports the last action's result.
- `server/tool_registry.py`: `browser_run` now also returns the `html` field
  that the `extract_html` action produces. Previously the action ran and its
  result was discarded, which is why the model kept trying different actions.

### Regression test

`tests/server/test_browser_run_backend.py::test_navigate_snapshot_reports_the_page_title`
asserts `navigate` returns `page_title == "Example Domain"` and that a
subsequent `extract_text` carries the title as well.

## 6. Browser sidecar findings (configuration, not changed)

- Sidecar container: `veya-browser` (`chromedp/headless-shell:latest`), 38 h
  uptime, on network `veya-net` at `192.168.80.10:9222`.
- `docker exec veya-backend curl http://192.168.80.10:9222/json/version` →
  `{"Browser": "Chrome/156.0.8078.48", "Protocol-Version": "1.3", ...}`.
- Backend env: `VEYA_BROWSER_CDP_URL=http://192.168.80.10:9222`,
  `VEYA_BROWSER_NO_SANDBOX=1`, `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`.
- `docker exec veya-backend python -c "...BrowserSession()..."` on
  `https://example.com` returns `title='Example Domain'`.

No production configuration was changed for the browser. The sidecar wiring was
already correct; only the snapshot code dropped the title.

## 7. Live verification (real, same method as the task description)

Deployed image `veya-backend:7d0b6faf` (built with
`deploy/Dockerfile.overlay` from base `veya-backend:9172671f`, container code
md5-verified equal to the worktree). Health:
`{"status":"ok","version":"0.6.0-7d0b6faf",...}`.

Method: inside the `veya-backend` container,
`server.auth.issue_token("ff7955113d054a62ab6ced5de522b174")` →
`POST http://127.0.0.1:8765/api/v1/agent/stream`
`{"text":"用浏览器打开 https://example.com 告诉我页面标题"}` →
`server.auth.revoke_token(token)` in a `finally` block. No token was inserted
into `~/.veya/auth.db` by hand.

### 7.1 Pre-fix reproduction (image `veya-backend:9172671f`)

```
issued token prefix: ad619251 ...
HTTP 200
token revoked
elapsed_s=211.6
event_count= 14
{"type": "master_start", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "id": "1:1", "epoch": 1, "seq": 1}
{"type": "master_round", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "round": 1, "id": "1:2", "epoch": 1, "seq": 2}
{"type": "tool_call", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "tool_name": "browser_run", "tool_args": {"url": "https://example.com", "action": "extract_text"}, "round": 1, "id": "1:3", "epoch": 1, "seq": 3}
{"type": "master_round", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "round": 2, "id": "1:4", "epoch": 1, "seq": 4}
{"type": "tool_call", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "tool_name": "browser_run", "tool_args": {"action": "extract_html", "url": "https://example.com"}, "round": 2, "id": "1:5", "epoch": 1, "seq": 5}
{"type": "master_round", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "round": 3, "id": "1:6", "epoch": 1, "seq": 6}
{"type": "tool_call", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "tool_name": "browser_run", "tool_args": {"action": "screenshot", "url": "https://example.com"}, "round": 3, "id": "1:7", "epoch": 1, "seq": 7}
{"type": "master_round", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "round": 4, "id": "1:8", "epoch": 1, "seq": 8}
{"type": "tool_call", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "tool_name": "browser_run", "tool_args": {"selector": "title", "url": "https://example.com", "action": "extract_text"}, "round": 4, "id": "1:9", "epoch": 1, "seq": 9}
{"type": "master_round", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "round": 5, "id": "1:10", "epoch": 1, "seq": 10}
{"type": "master_error", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "error": "LLM call timed out after 120s", "round": 5, "reason": "provider_timeout", "id": "1:11", "epoch": 1, "seq": 11}
{"type": "text_delta", "squad_id": "master", "delta": "⚠ LLM call timed out after 120s", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "id": "1:12", "epoch": 1, "seq": 12}
{"type": "master_done", "session_id": "sess_01a16880-8cd2-7718-a48f-3b41ac86c8e6", "status": "fatal_error", "cost_usd": 0, "rounds": 5, "id": "1:13", "epoch": 1, "seq": 13}
{"type": "[DONE]"}
HAS_EXAMPLE_DOMAIN= False
```

The run burned five rounds on `browser_run` because every result had
`"title": ""`, then timed out. No answer.

### 7.2 Post-fix run 1 (image `veya-backend:7d0b6faf`)

```
issued token prefix: 57d86aef ...
HTTP 200
token revoked
elapsed_s=38.3
event_count= 7
{"type": "master_start", "session_id": "sess_01a16886-dcc9-77da-8d56-4469f77afeee", "id": "1:1", "epoch": 1, "seq": 1}
{"type": "master_round", "session_id": "sess_01a16886-dcc9-77da-8d56-4469f77afeee", "round": 1, "id": "1:2", "epoch": 1, "seq": 2}
{"type": "tool_call", "session_id": "sess_01a16886-dcc9-77da-8d56-4469f77afeee", "tool_name": "browser_run", "tool_args": {"url": "https://example.com", "action": "extract_text"}, "round": 1, "id": "1:3", "epoch": 1, "seq": 3}
{"type": "master_round", "session_id": "sess_01a16886-dcc9-77da-8d56-4469f77afeee", "round": 2, "id": "1:4", "epoch": 1, "seq": 4}
{"type": "text_delta", "squad_id": "master", "delta": "The page title is **\"Example Domain\"**.", "session_id": "sess_01a16886-dcc9-77da-8d56-4469f77afeee", "id": "1:5", "epoch": 1, "seq": 5}
{"type": "master_done", "session_id": "sess_01a16886-dcc9-77da-8d56-4469f77afeee", "status": "success", "cost_usd": 0, "rounds": 2, "id": "1:6", "epoch": 1, "seq": 6}
{"type": "[DONE]"}
HAS_EXAMPLE_DOMAIN= True
```

### 7.3 Post-fix run 2, including `GET /stream/{sid}` replay

```
POST /api/v1/agent/stream -> 200
elapsed_s=34.7
live_events=7
LIVE {"type": "master_start", "session_id": "sess_01a16888-9424-7089-8257-ede1fdb8b9ba", "id": "1:1", "epoch": 1, "seq": 1}
LIVE {"type": "master_round", "session_id": "sess_01a16888-9424-7089-8257-ede1fdb8b9ba", "round": 1, "id": "1:2", "epoch": 1, "seq": 2}
LIVE {"type": "tool_call", "session_id": "sess_01a16888-9424-7089-8257-ede1fdb8b9ba", "tool_name": "browser_run", "tool_args": {"url": "https://example.com", "action": "extract_text"}, "round": 1, "id": "1:3", "epoch": 1, "seq": 3}
LIVE {"type": "master_round", "session_id": "sess_01a16888-9424-7089-8257-ede1fdb8b9ba", "round": 2, "id": "1:4", "epoch": 1, "seq": 4}
LIVE {"type": "text_delta", "squad_id": "master", "delta": "页面标题：**Example Domain**", "session_id": "sess_01a16888-9424-7089-8257-ede1fdb8b9ba", "id": "1:5", "epoch": 1, "seq": 5}
LIVE {"type": "master_done", "session_id": "sess_01a16888-9424-7089-8257-ede1fdb8b9ba", "status": "success", "cost_usd": 0, "rounds": 2, "id": "1:6", "epoch": 1, "seq": 6}
LIVE {"type": "[DONE]"}
GET replay -> 200
replay_events=7
REPLAY {"id": "1:5", "event_id": "def24b84-8830-438c-a74f-a0a4ce05312f", "session_id": "sess_01a16888-9424-7089-8257-ede1fdb8b9ba", "epoch": 1, "seq": 5, "event": "text_delta", "data": {"type": "text_delta", "squad_id": "master", "delta": "页面标题：**Example Domain**", "session_id": "sess_01a16888-9424-7089-8257-ede1fdb8b9ba"}, "created_at": 1792755178.3721626}
REPLAY {"id": "1:6", "event_id": "0d57704b-d2d4-4424-b06d-da0018f0b2b1", "session_id": "sess_01a16888-9424-7089-8257-ede1fdb8b9ba", "epoch": 1, "seq": 6, "event": "master_done", "data": {"type": "master_done", "session_id": "sess_01a16888-9424-7089-8257-ede1fdb8b9ba", "status": "success", "cost_usd": 0, "rounds": 2}, "created_at": 1792755178.3752353}
REPLAY {"type": "[DONE]"}
token revoked
LIVE_HAS_EXAMPLE_DOMAIN= True
```

(Replay frames carry the answer inside `data.delta`, so the probe's
`REPLAY_HAS_EXAMPLE_DOMAIN=False` line reads the wrong field; `seq 5` above is
the answer in the journal.)

Both post-fix runs completed in one `browser_run` round instead of five, and
delivered the title as `text_delta` before `master_done`.

## 8. Test and lint results

| Check | Result |
|---|---|
| `tests/test_stream_terminal_ordering.py` | 2 passed (2 failed before the fix) |
| `tests/test_stream_mirror_sync.py` | 5 passed |
| `tests/test_sse_disconnect.py` | 13 passed |
| `tests/test_sse_cross_epoch_race.py` | passed |
| `tests/test_cancel_epoch_capture.py` | passed |
| `tests/test_stop_reentry_race.py` | passed |
| `tests/server/test_browser_run_backend.py` | 7 passed |
| `tests/server/test_browser_session_tools.py` | passed |
| `tests/test_react_loop_guard.py` | passed |
| `tests/runtime/test_execution_runtime.py` | 13 passed |
| `tests/goal_run` | 292 passed, 1 skipped |
| `evals.personal_agent_gold.gate` | `{"status": "PASS", "failures": []}` |
| `ruff check` (changed files) | All checks passed |
| `ruff format --check` (changed files) | 6 files already formatted |

Harness suite: 49 passed, 1 xfailed, 1 failed —
`tests/runtime/test_coding_sandbox_profiles.py::test_command_parser_rejects_shell_escape_and_runner_captures_redacted_artifact`
fails identically with the fix stashed, so it is pre-existing and unrelated.

`tests/test_master_tools.py` has 6 pre-existing failures (tool-inventory prompt
assertion, a 400 from the router, and two `veya_loop._assembly` import errors);
they fail identically with the fix stashed.

Repo-wide `ruff check .` reports 321 pre-existing errors and
`ruff format --check .` reports 74 pre-existing unformatted files; the changed
files are clean.

## 9. Deployment note

`fix/baseline` was behind `origin/main` (the deployed `9172671f`). The branch
was merged with `origin/main` before building so the fix sits on the code the
production image actually runs. The deployed image is
`veya-backend:7d0b6faf`, built from that merge commit with
`deploy/Dockerfile.overlay` on base `veya-backend:9172671f`.

Pre-deploy override backup: `/tmp/backend-override.before-7d0b6faf.yml`
(image was `veya-backend:9172671f`). To roll back, restore
`image: veya-backend:9172671f` in
`/home/soffy/.config/veya/backend-override.yml` and recreate the `backend`
service.
