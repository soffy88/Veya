# W11 — Veya Bot 完整落地（Bot Landing）

Base: canonical main 693ed9f5 (W6–W10 merged, deployed at veya.aiinote.com).
Owner decision (user, 2026-10-11): "授权你全权处理，把bot给我完整落地，能让我直接创建使用".
Goal: a logged-in user opens veya.aiinote.com (often on an iPhone), goes to Team, creates a bot in
under a minute, chats with it, and the bot actually works like an always-on agent (Grok-Bot style,
see W10): answers, uses tools, queues/merges rapid messages, accepts corrections mid-turn, remembers,
runs routines on its own, posts results, asks for approval on side effects.

## Live defects found by the supervisor on prod (must be fixed, each with a regression test)

D1  Plain chat is blocked. `POST /api/v1/bots/{id}/messages {"text":"你好，2+3等于几？"}` returns
    `{"status":"blocked","reply":"Missing required evidence: ['ac-canonical_action_observed']"}`.
    Cause: bot turns go through GoalRun + Verification OS (server/goal_run/canonical_worker.py
    finalize_candidate, runtime/verification/engine.py default feature map requires
    `canonical_action_observed`). A turn whose correct outcome is a direct answer has no canonical
    action, so the verifier says INSUFFICIENT. Fix in the existing authority (do NOT add a bypass
    flag or a second runtime): a direct answer / reply-only turn must be a valid, verifiable outcome
    (e.g. a `reply` canonical action or answer evidence the verifier accepts), while turns that
    claim side effects still need real action evidence. Verified-L2 tasks keep their strictness.
D2  Ownership holes in server/routes/bot_team.py:
    - `create_bot` trusts `req.owner_id` from the client → anyone can create bots owned by others.
      Owner must always be the authenticated caller.
    - `list_bots` returns every user's bots (`registry.list()` unfiltered). Must filter to the caller
      (+ shared system bots like Chef seeded per owner, not global leakage).
    - Anonymous (no token) can create/chat. Creating/chatting must require login (`require_user`);
      read-only health stays public.
    Audit every /api/v1/bots* and group route for the same.
D3  W10 features are all OFF in prod. Production must run with the full always-on set ON (see
    "Production flag set"). Group A's flags live in server/nano_w10/flags.py instead of the shared
    table server/feature_flags.py — register them in the shared table too (single source of truth;
    keep nano_w10/flags.py as a thin reader of the shared table, no second flag registry).

## Production flag set (ON in prod via backend env; code defaults may stay OFF)
VEYA_TURN_QUEUE, VEYA_WAKE_ROUTER, VEYA_LOOP_MODE, VEYA_FINISH_CHECKPOINT, VEYA_LIVE_STREAM,
VEYA_PROMPT_CACHE, VEYA_CONTEXT_STEWARD, VEYA_MEMORY_REVIEW, VEYA_READ_OWN_CHAT, VEYA_BOT_MEMORY_MD,
VEYA_PERSISTENT_WORKERS, VEYA_AUTO_REVIEW, VEYA_GROUP_ROLES, VEYA_BOT_SECRETS, VEYA_A11Y_FIRST,
VEYA_DOM_READER, VEYA_CHANNEL_LADDER, VEYA_TOOL_ERROR_FRIENDLY, VEYA_SEARCH_FALLBACK, VEYA_CHANNEL_STATUS,
VEYA_APPROVAL_GATES (side effects ask first). Leave OFF: VEYA_RUNNER_SELFUPDATE, VEYA_DEVICE_PAIRING
unless the scenario needs it. Write the list to `deploy/bot_landing_flags.env` (committed, no secrets);
the supervisor wires it into the prod container.

## Acceptance scenarios (all must pass, automated)
Write `tests/test_bot_landing_e2e.py` (FastAPI TestClient against the real app, real stores in a tmp
HOME, a deterministic fake LLM provider) AND `scripts/bot_landing_live.py` (same scenarios over HTTP
against a running server with a real model: `--base http://127.0.0.1:8767 --user e2e_tester`; it
registers/logs in its own test user via /api/auth/register + /api/auth/login, never needs secrets
on the command line, prints PASS/FAIL per scenario, exits non-zero on any FAIL, cleans up its bots).
S1  create bot (name, instructions, model default) → ask "2+3等于几" → status completed, reply has 5.
S2  tool use: "读取 https://example.com 的标题" → bot uses read/browse/search tool, reply contains
    "Example Domain"; live stream shows the tool step.
S3  turn queue: send 3 messages within 1s → one coalesced turn (or ordered turns), no lost message,
    final reply addresses all three.
S4  correction: start a long task, POST /corrections "改成用英文" → following output in English.
S5  memory: "记住我喜欢黑咖啡" → new conversation "我喜欢什么咖啡？" → answers 黑咖啡; forget works.
S6  routine/always-on: create routine "每2分钟报一次时间" (or shortest allowed) → within one period the
    bot posts a message on its own without a user turn; routine can be paused/deleted.
S7  loop goal: goal with 2 rounds → per-round checkpoint card → finish checkpoint card; user verdict
    route accepts it.
S8  group: 2 bots in a group with roles; @mention routes to the right bot; admin-only action refused
    for non-admin.
S9  secrets: request_secret flow; secret value never appears in replies, logs, or message history.
S10 isolation: user B cannot list/read/chat/delete user A's bots (403/404), cannot spoof owner_id.
S11 side effect approval: "在 /tmp 写一个文件" style action → approval card; nothing happens until
    approved; approve → done.
S12 failure UX: model error / FEATURE_DISABLED → the bot replies with a clear, friendly message, never
    a raw "Missing required evidence" string.

## Web (apps/web, SvelteKit) — the user must be able to do all of this from a phone
W-1 Team page: "新建 Bot" button → dialog/sheet: name, one-line job, instructions (with 4 presets:
    通用助手 / 研究员 / 每日简报 / 代码助手), model picker (server models from the existing provider
    registry; default preselected), create → lands in the bot chat. Validation errors shown inline;
    bot_id auto-generated from the name (snake_case, unique), never typed by the user.
W-2 Bot chat: streaming reply, live tool steps (W4), correction box while running, checkpoint cards
    with approve/reject, approval cards for side effects, retry on error, scroll/keyboard behave on
    iOS Safari, works at 375px width.
W-3 Bot list cards: status (idle/running/needs approval), last message, unread; delete with confirm.
W-4 Tabs: Memory (view/forget), Routines (create from natural language or form, pause, delete, next
    run time in user's timezone), Settings (rename, instructions, model, secrets list masked).
W-5 Group page: create group from existing bots, roles, @mention.
W-6 Empty states + first-run hint on Team page; all strings Chinese-first.
W-7 `pnpm run check` 0 errors, build OK; Playwright smoke test (apps/web/tests/bot_landing.spec.ts)
    covering W-1/W-2 against a mocked API.

## Rules (same as W6–W10)
3O layering (gates.sh zero new FAIL/VIOLATION vs canonical), no second scheduler / approval system /
event store / capability registry / flag registry / MasterAgent; reuse GoalRun, ActionGateway,
routine_clock, bot_team stores. Side effects through the existing approval path. New model-callable
tools must be listed in the report. Never commit platform/3O/*, .venv, node_modules, logs, prompts.
Regression must stay green: tests/test_nano_w*.py tests/test_lorca_*.py tests/test_orca_*.py
tests/test_p3a_bot_isolation.py tests/test_product_shell.py + origin tests (tests/test_sse_envelope.py,
tests/test_stop_reentry_race.py, tests/remote/test_local2_*.py, tests/security/test_sf002_sandbox_boundary.py).
Commit small and often. Finish with BOT_LANDING_<GROUP>_REPORT.md: what works, test output, new tools,
known gaps — only claims you actually ran.
