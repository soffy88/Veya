# SEC_FIX10 (worker brief, opencode)
Worktree: /data/soffy/projects/veya-sec (branch fix/security-audit). Do NOT touch main repo /data/soffy/projects/veya, do not push, do not restart any service/container, do not touch hevi-* or ports 7766/7767/8788. Never print secrets/.env.

## Facts (verified by reviewer)
- Commit 8a76bbb3 (F9) added ONLY SEC_REPORT_F9.md; it contains NO code change. Its "root cause / fix" text is false. Do not repeat this: any report without a real diff + raw pasted output will be rejected.
- Independent rerun (bwrap ON, VEYA_SANDBOX_BWRAP unset) of
  `timeout -s INT 400 .venv/bin/python -m pytest tests/remote/test_local2_unlimited_concurrency.py -x -q -p no:cacheprovider -o faulthandler_timeout=90 --basetemp=.gsk-tmp/pt`
  gave: runs 1-3 = 13 passed; run 4 HUNG after 7 passed (rc=124). Raw log: /home/soffy/gptlogs/close/f9x/s4.log
- Hang location: teardown of test #8, inside pytest_asyncio `_scoped_runner` -> asyncio.runners `_cancel_all_tasks` -> run_until_complete never returns. Pending task:
  `<Task cancelling name='veya-remote-admission-direct_...' coro=DurableJobManager._admit_goal_run() at veya/remote/execution.py:3195 wait_for=<Future pending ...> cb=[gather._done_callback]>`
  i.e. a task that was cancelled but never finishes: something under `_admit_goal_run` (project_run_goal(wait=True), the heartbeat monitor task, a semaphore/admission future, an asyncio.shield, an `except CancelledError`/`except BaseException` that swallows and keeps waiting, or a subprocess wait without timeout) does not honour cancellation.

## Task
1. Reproduce: loop the command above (use distinct --basetemp per run) until it hangs; add temporary diagnostics if needed (e.g. dump `asyncio.all_tasks()` stacks with task.print_stack() at teardown) to find exactly which await swallows cancellation.
2. Fix the real root cause in product code (veya/remote/*, veya/goal*, sandbox) so cancellation propagates and every spawned task/subprocess is awaited/killed with a bounded timeout. Test-only band-aids (skips, xfail, bigger timeouts, VEYA_SANDBOX_BWRAP=0, disabling the test, removing assertions) are NOT acceptable. Keep all SEC controls on: default deny, owner checks, env whitelist, bwrap network isolation, tool gate enforce.
3. Remove temporary diagnostics. Commit with message `fix(sec): F10 <short root cause>`.
4. Prove: run the command 8 times sequentially AND 3 runs concurrently (distinct basetemps); all must be `13 passed`. Also run `tests/test_sec_audit_*.py tests/remote/` once and paste the summary line.
5. Write SEC_REPORT_F10.md: root cause (file:line), the diff summary (`git show --stat HEAD`), and the RAW last lines of every run pasted verbatim. Last line of the report must be exactly `F10 DONE` or `F10 BLOCKED: <reason>`. Commit it.
