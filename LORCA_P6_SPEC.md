# Task: P6 — run bots on different machines (runners), Lorca-style
Worktree /data/soffy/projects/veya-lorca-p6, branch feat/lorca-runners, based on feat/lorca-team-groupchat (BotSpec now has runner_id; see LORCA_P1P2_REPORT.md).

## Goal
The user has several machines on a private Tailscale tailnet, each can run a Veya server. A bot whose runner_id names another machine should execute its turns there, while the hub keeps the bot catalog, conversations, and group transcripts.
1. Investigate first: server/routes/remote_mcp.py and any existing remote-execution/auth code on this branch; reuse it rather than inventing a protocol. Write docs/LORCA_RUNNERS_DESIGN.md (short) with the chosen design before coding.
2. Runner registry: new server/bot_team/runners.py; runners persisted under ~/.veya/runners.json (override VEYA_RUNNERS_PATH): {runner_id, name, base_url (tailnet), token secret REF (env var name or file path — never store raw tokens in repo or logs), last_seen, status}. "local" runner is implicit = this server.
3. REST /api/v1/runners: list, add, remove, health check (GET runner /health with timeout), and set a bot's runner_id (validate exists).
4. Routing: when a bot turn (1:1, group, message_bot) is for a bot whose runner_id != local, the hub forwards the turn to the runner's Veya over HTTP (an authenticated endpoint on the runner that executes the turn locally through the normal MasterAgent -> GoalRun path and returns the reply), with timeout, retry-once, and a clear error turn if the runner is offline. Execution authority stays with the runner's own GoalRun; the hub does not create a second task store. Default (no runners configured) behavior must be unchanged.
- Tests: tests/test_lorca_p6_runners.py (registry CRUD, health check with fake transport, routing local vs remote, offline runner error, token never appears in responses/logs). Also run the P1/P2 tests + baseline (tests/test_lorca_p1_bots.py tests/test_lorca_p2_groups.py tests/test_p3a_bot_isolation.py tests/test_p3b_bot_delegation.py tests/test_p3c_multi_bot_coordination.py tests/test_product_shell.py; currently 2 pre-existing fails in test_product_shell resume) — no new failures.
- Report: LORCA_P6_REPORT.md.

## HARD RULES (other agents work in sibling worktrees veya-lorca, veya-lorca-p3, veya-lorca-p4, veya-lorca-p5/p6 — never touch them)
- Work ONLY in this worktree. Build ONLY on code committed on this branch. Do NOT read/copy/import from /data/soffy/projects/veya (canonical checkout has other agents' uncommitted WIP like server/bot_runtime/ that is incompatible) and do not create server/bot_runtime/.
- Do not copy code from github.com/egoist/lorca (GPL-3.0); implement from this spec.
- Frozen Veya authorities: MasterAgent = semantic, GoalRun = durable execution, PermissionEngine = policy. No second scheduler, no second task store, no new executor. Do not add model-visible tools to MasterToolRegistry (tool surface is frozen per AGENTS.md).
- Keep edits to existing files minimal and additive (server/app.py: router include lines only). Put new code in new modules.
- Don't touch platform/3O (symlinks; never commit), .venv, systemd services, or production config. Never commit secrets/keys. Don't push or merge. Python: ./.venv/bin/python; pytest with -p no:cacheprovider.
- Small logical commits. Finish with the report file (named above) committed: what was built, files, exact test commands + results, known gaps.
