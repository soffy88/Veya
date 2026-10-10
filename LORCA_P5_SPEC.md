# Task: P5 — phone-friendly web app (PWA) + Web Push for the Lorca-style bot team
Worktree /data/soffy/projects/veya-lorca-p5, branch feat/lorca-pwa-push, based on feat/lorca-team-groupchat (P1/P2 already here: server/bot_team/, server/routes/bots.py, see LORCA_P1P2_REPORT.md for endpoints).
Sibling branches being built in parallel (code not here; just target their endpoints, behind feature checks so the UI degrades if a 404 comes back):
- memory: GET/PUT/DELETE /api/v1/bots/{bot_id}/memory[/{name}] (Markdown files)
- routines: GET/POST /api/v1/bots/{bot_id}/routines, PATCH/DELETE /{id}, POST /{id}/run_now; body {objective, schedule ("every 30m" | "daily 09:00" | cron), timezone, enabled}

## Goal
1. apps/web (SvelteKit): new routes under /team: bot list + create/edit; Chef chat (proposal -> confirm); 1:1 chat with a bot; group chats (create 1–6 bots, transcript hiding [PASS]); per-bot memory editor; per-bot routines list/create/pause/run-now. Mobile-first layout. Follow existing apps/web conventions (lib/api client, auth token handling).
2. PWA: web app manifest, icons (simple generated SVG/PNG), service worker for offline shell + push handling + notification click opens the relevant chat. Installable on iOS/Android home screen.
3. Web Push backend: new server/bot_team/push.py + server/routes/push.py (prefix /api/v1/push): VAPID keys generated once and stored under ~/.veya/push/ (never commit keys), GET public key, POST/DELETE subscription per user. Send via pywebpush if available in the venv (else add it to pyproject optional deps and skip send with a clear log). Hook: when a bot turn, group round, or routine finishes, push "<bot name>: first line" — hook via server/notification_center.py, not by editing GoalRun.
4. P1 gap: bot and group message POSTs currently run inline. Add an opt-in async mode (?async=1 -> 202 with task_id; result fetched via existing GET endpoints / push), without changing default behavior.
- Tests: tests/test_lorca_p5_push.py (subscription CRUD, VAPID key persistence, notification hook triggers send with a fake sender, async 202 path). Web: `npm run check` / build in apps/web must pass (use existing package manager in repo; don't upgrade deps broadly).
- Report: LORCA_P5_REPORT.md.

## HARD RULES (other agents work in sibling worktrees veya-lorca, veya-lorca-p3, veya-lorca-p4, veya-lorca-p5/p6 — never touch them)
- Work ONLY in this worktree. Build ONLY on code committed on this branch. Do NOT read/copy/import from /data/soffy/projects/veya (canonical checkout has other agents' uncommitted WIP like server/bot_runtime/ that is incompatible) and do not create server/bot_runtime/.
- Do not copy code from github.com/egoist/lorca (GPL-3.0); implement from this spec.
- Frozen Veya authorities: MasterAgent = semantic, GoalRun = durable execution, PermissionEngine = policy. No second scheduler, no second task store, no new executor. Do not add model-visible tools to MasterToolRegistry (tool surface is frozen per AGENTS.md).
- Keep edits to existing files minimal and additive (server/app.py: router include lines only). Put new code in new modules.
- Don't touch platform/3O (symlinks; never commit), .venv, systemd services, or production config. Never commit secrets/keys. Don't push or merge. Python: ./.venv/bin/python; pytest with -p no:cacheprovider.
- Small logical commits. Finish with the report file (named above) committed: what was built, files, exact test commands + results, known gaps.
