# SEC_FIX6 — one real regression blocks the SEC merge (2026-10-16)

Worktree /data/soffy/projects/veya-sec, branch fix/security-audit (HEAD 653a7adc, already contains main e8157acd).
Everything else in the full regression is clean vs canonical; ONLY this remains.

## Symptom (reproducible, SEC-only)
```
cd /data/soffy/projects/veya-sec
TMPDIR=/home/soffy/gptlogs/tmp timeout -s INT 400 .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/remote/test_local2_unlimited_concurrency.py -o faulthandler_timeout=120
```
- On canonical /data/soffy/projects/veya (main e8157acd): `13 passed in 77.81s`.
- On fix/security-audit: hangs forever after 7 dots, i.e. in/after test #8
  `test_no_session_registry_growth_after_reads_and_cycles`. Happened 3 times (full regression, chunk run, isolated file run).
- faulthandler stack: main thread in asyncio `runners.py:_cancel_all_tasks` → `run_until_complete` → `selectors.select`
  (event-loop teardown of the test waits for a task that never finishes cancelling).
  On interrupt, pytest prints: `Task was destroyed but it is pending! ... DurableJobManager._admit_goal_run() running at veya/remote/execution.py:3195`.
- Each test alone passes on SEC, and every pair (test_k + test #8, k=1..7) passes. So it is cumulative state across tests in one process
  (leaked task / lock / blocking sync call inside the event loop / cached module-level state), introduced by the SEC diff.

## Suspects in the SEC diff (check first, don't guess — reproduce and prove)
- veya/remote/tool_adapter.py `_remote_principal_is_owner()` → `server.auth.list_users()` is a synchronous sqlite call on the event loop
  inside worker.dispatch / shell paths, and it reads the REAL auth DB path unless monkeypatched (tests must never touch ~/.veya/auth.db).
- veya/obase/sandbox.py: new `_bwrap_available()` probe (`subprocess.run(..., timeout=5)` blocking) and `bwrap --unshare-net` wrapping
  of children when network_blocked — a wrapped child that ignores cancellation/kill (bwrap not forwarding signals, process group) would
  leave `_admit_goal_run`/execution tasks pending at teardown.
- veya/remote/mcp_server.py: initialize() order change (auth before adapter.initialize()).

## Required
1. Find the real root cause (bisect commits of `git log main..fix/security-audit` or bisect the suspects; prove with the command above).
2. Fix it in product code without weakening any SEC control (default deny, owner check, verify_command server-side, env allowlist, network isolation).
   E.g. make owner lookup non-blocking/cached and not touch the real auth DB under tests; make bwrap-wrapped children killable
   (kill process group / `--die-with-parent`), etc. — whatever the evidence shows.
3. Proof: the command above passes on SEC (13 passed) twice in a row; `tests/test_sec_audit_*.py` all pass;
   `tests/remote/test_local2_real_mcp_surface.py tests/remote/test_local2_u4_shell_batching.py tests/remote/test_remote_gateway.py` pass.
4. Commit on fix/security-audit (no push). Write SEC_REPORT_F6.md: root cause with evidence, fix commit sha(s), the exact commands and results.
No push, no service restarts, no deletion outside the worktree, no paid models, never print secrets.

## Findings so far (previous executor, 01:20 CST 10-16) — start from here
- Proven by bisect: with `VEYA_SANDBOX_BWRAP=0` the whole file passes on SEC (13 passed, ~78 s), also with an isolated auth DB.
  With bwrap enabled it hangs after test #8. So the cause is the SEC H5 `bwrap --unshare-net --bind / / --` wrapping in veya/obase/sandbox.py
  (`_isolation_argv` / `_bwrap_available`), most likely bwrap children not being killed/reaped on cancel/timeout (bwrap is the direct child;
  signals to it don't reach the grandchild; no `--die-with-parent`, no new session/process-group kill), leaving tasks pending at loop teardown.
- Fix the bwrap path (e.g. add `--die-with-parent` and `--new-session`/start_new_session + kill the whole process group on cancel/timeout,
  make sure the async wait/reap completes), keep network isolation ON by default. Do NOT fix it by disabling bwrap.
- The real auth-DB / list_users hypothesis did not change the outcome (still worth making the owner lookup not block the loop, optional).
