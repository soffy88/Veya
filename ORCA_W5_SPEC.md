# Task ORCA-W5 — Design Mode: click an element in the built-in browser, hand it to the agent
Worktree /data/soffy/projects/veya-orca-w5, branch feat/orca-design-mode. Report: ORCA_W5_REPORT.md. Tests: tests/test_orca_w5_design_mode.py.
Investigate first: server/browser_computer_adapter.py (3O Browser Computer — driver code lives in platform/3O, do not edit it), any browser panel in apps/web, how chat attachments reach an agent session. Write docs/ORCA_W5_DESIGN.md (short) first.
1. Design Mode toggle on the workspace browser panel. While on, hovering highlights elements; click captures: outerHTML (trimmed to ~8KB), selector path, computed styles subset (box model, display/flex/grid, font, color, position), bounding box, a cropped screenshot of the element, page URL.
2. Source location: when the page is a local dev server, resolve file:line — add a dev-only Vite plugin for apps/web that stamps data-veya-src="file:line" on Svelte elements (off in production builds); generic: read data-veya-src / data-source / React __source if present; else "unknown".
3. "Send to agent": POST /api/v1/design/capture stores the capture as an artifact/attachment and posts one message to the current session/goal run with the user's instruction plus the capture.
4. Works both with the 3O server-side browser (screenshot via its API) and an iframe of a same-origin dev server (DOM capture script); document limits (cross-origin pages: no source mapping).
Tests: capture payload normalisation/trimming, source resolution from attributes, message composition, endpoint auth; svelte-check + build for UI.
## HARD RULES (sibling worktrees veya-orca-* are worked by other agents at the same time — never touch them)
- Work ONLY in this worktree, branch named above, based on main edd2619a. Build ONLY on committed code. Do NOT read/copy/import from /data/soffy/projects/veya (canonical checkout has other agents' uncommitted WIP). Do not create server/bot_runtime/.
- Clean-room: do not copy code from Orca (stablyai/orca) or Lorca; implement from this spec.
- Frozen Veya authorities: MasterAgent = semantic, GoalRun = durable execution, PermissionEngine = policy. No second scheduler, no second task store, no new executor — extend existing paths. Adding a model-visible tool to MasterToolRegistry is allowed only if clearly needed; list every new model tool prominently in the report.
- Keep edits to existing files minimal and additive (server/app.py: router include lines only). New code in new modules. New feature flags default OFF only where behaviour would otherwise change for existing users.
- Don't touch platform/3O (symlinks; never commit), .venv, node_modules symlinks, systemd services, production config. Never commit secrets/keys/.env. Don't push or merge.
- Python: ./.venv/bin/python -m pytest -p no:cacheprovider <one or two files>. NEVER run the whole tests/ dir (OOM). Web: one `pnpm run check` and one `pnpm run build` in apps/web with NODE_OPTIONS=--max-old-space-size=2048; never start dev servers.
- Commit WIP early and after every passing step (small logical commits). git status needs --ignore-submodules=all.
- Regression set to run at the end (2 pre-existing fails in test_product_shell resume are known): tests/test_lorca_p1_bots.py tests/test_lorca_p2_groups.py tests/test_p3a_bot_isolation.py tests/test_product_shell.py plus any existing test file covering a module you edited.
- Finish with the report file named above committed: what was built, files, API routes, UI entry points, exact test commands + results, known gaps.
