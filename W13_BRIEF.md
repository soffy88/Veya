# W13 worker brief

Read W13_REMOTE_CONTROL_SPEC_DRAFT.md (in this dir). Step 1: expand it into W13_REMOTE_CONTROL_SPEC.md after studying the existing code it must reuse (agent mailbox, GoalRun, routines, approval / policy_resolver, W12 server/supervisor, push/notifications in server/bot_team/push.py, tool_registry side effects, flags registration like VEYA_SUPERVISOR). Commit it. Step 2: implement phase A (backend) fully, then phase B (web) if time allows.

Phase A — backend, flag VEYA_REMOTE_CONTROL default OFF:
- Remote host registry entry stored via existing stores (atomic_write_json pattern; no os.replace / direct IO violations — run scripts/check_no_direct_io.py and scripts/check_3o_gates.py --strict, zero new entries).
- Identity: resolve peer by `tailscale whois --json <ip>` (wrapped, mockable); policy file: tailnet default, allowlist, deny wins (hostname / tag: / user:). Unknown identity → deny.
- Remote agent sessions: start task on a host's agent (pi / claude-wb / codex etc. as an executor command template), list, stream status, cancel, hand off to another agent with context. Every remote start/cancel/handoff goes through the existing approval path (declared side effect external_mutation / privileged). Model-callable tools must declare side effects.
- Wake policy per sender+agent: now / batch / none, quiet hours, rate limit; implemented with existing routines, no new scheduler.
- States accepted → woken → acked → done; done only with verifier evidence (reuse W12 supervisor verifier).
- File handoff: expiring token link (24h default), recipient-bound, size + SHA-256 verified; contents tagged untrusted.
- Notifications: done / needs-approval via existing push.
- REST routes with owner checks.
Phase B — web (apps/web): mobile-friendly "Remote" page: hosts, running agents, start task (text + browser speech-to-text), approve/deny cards, done notifications. pnpm check 0 errors + build.

Rules: Paseo is AGPL — clean-room only, never copy code or text. Yobiru MIT ideas → add attribution in NOTICE. Do not touch platform/3O/*, .venv, or uncommitted veya/remote/* files (someone else's WIP; put new code in server/remote_control/). No new scheduler, event store, approval, capability registry. Tests in tests/test_w13_*.py (mock tailscale/network). Run only those plus tests/test_product_shell.py and tests/test_w12_supervisor.py, one or two files per pytest call. Commit after each passing step with `git add -A -- . ':!.venv' ':!platform/3O'`. If resumed, read git log and uncommitted files first. Finish with W13_REPORT.md committed listing flags, new model-callable tools, routes, what is left.

## HARD SAFETY RULES (added after an incident)
- Only create/modify/delete files inside this worktree (/data/soffy/projects/veya-w13). NEVER delete, move or clean anything outside it (other projects, ~/.cache, /tmp/* of others, venvs, node_modules). Never run rm -rf with globs. Never touch .venv (it is a symlink to the shared venv).
- If the disk is full or space is low: STOP, write the problem to W13_BLOCKED.md and exit. Do not free space yourself.
- server/remote_control/ contains uncommitted work from a weaker previous worker: review it critically, keep what is correct, rewrite what is not, and add tests.
