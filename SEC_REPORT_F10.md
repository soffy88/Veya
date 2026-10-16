
---
## F10b (2026-10-16, supervisor-side fix; opencode deepseek worker stalled twice with no output)

### Root cause (verified with a teardown diagnostic that dumps every pending task's await chain)
1. CPython 3.12 asyncio bug: when a task is cancelled while `asyncio.create_subprocess_*` is still connecting the child's pipes, `unix_events._make_subprocess_transport` runs `transp.close(); await transp._wait()`. At loop shutdown (`asyncio.runners._cancel_all_tasks`) the internal `_connect_pipes` task is cancelled too, a pipe slot stays `None`, `BaseSubprocessTransport._try_finish` never fires, and `_wait()` never resolves -> the cancelled task is pinned forever.
   Observed chains:
   - `_admit_goal_run (execution.py) -> project_run_goal -> _run_loop_and_finalize -> finalize_candidate -> runtime/verification/engine.py collect_evidence_bundle -> _run_command -> create_subprocess_shell -> base_subprocess._wait`
   - `_run_guarded -> spawn_guard.run -> execute_current -> _process_one_task -> execute_semantic_task -> tool_adapter runner -> direct_exec.run_direct_command -> create_subprocess_exec -> base_subprocess._wait`
2. Contributing: `contextlib.suppress(asyncio.CancelledError, Exception)` around `await task` swallowed the caller's own cancellation (execution.py suspend/cancel paths, goal_run/runner.py lease heartbeat); `_run_loop_and_finalize` did not cancel/await its child tasks when itself cancelled; tool_adapter/executor_adapters did not kill the process group / cancel pipe pumps on cancel.

### Code changes (commits on fix/security-audit)
- adff0241 revert 45a50583 (asyncio.shield misfix: hid cancellation, orphaned work).
- 818773ff `_await_cancelled_task` helper (re-raises when `current_task().cancelling()`), used in execution.py + runner.py; runner main loop wrapped so cancellation cancels children and waits <=10s; executor_adapters kills group on CancelledError and cancels leftover readers; tool_adapter cancels leftover pump/wait tasks on cancel.
- 67333cc6 new `veya/remote/_proc.py::spawn_process` (plain Popen in its own session + StreamReader pipes + pidfd exit watch; no child watcher / transport._wait; honours a patched `asyncio.create_subprocess_exec` so test hermeticity guards still apply) used by direct_exec, tool_adapter CLI workers and executor_adapters; `VerificationEngine._run_command` now Popen + `to_thread(communicate)` with killpg on cancel and an 1800s bound.
- SEC controls unchanged (bwrap still on, default deny, owner checks, tool gate).

### Evidence
- Diagnostic 6x parallel before fix: 2-5/6 hung (diag dumps above); after fix: 6/6 `13 passed`.
- Independent f10x (/home/soffy/gptlogs/close/f10x.sh, bwrap ON, 8 serial + 3 parallel): all 11 runs `13 passed` (s1..s8 ~34s each, p1..p3 ~41s). Raw: /home/soffy/gptlogs/close/f10x/sum.txt
- tests/remote/test_service_control_capability.py: 7 failed / 42 passed = identical to main baseline base1016 (pre-existing).

F10B DONE
