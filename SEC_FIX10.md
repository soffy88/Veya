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

---
## F10 第2轮（2026-10-16 10:00 CST，主管编写）——必须改代码，只写报告不算

### 背景
- 上一外部工人提交 45a50583 用 asyncio.shield 包 project_run_goal：这是错误修复（屏蔽取消、留下孤儿任务）。**先撤掉该改动**（`git revert --no-edit 45a50583` 或手工还原 execution.py 那段），再做正确修复。
- 现象：tests/remote/test_local2_unlimited_concurrency.py 在 bwrap 下偶发挂起（第 4 次串行跑 7 个测试后卡在 teardown _cancel_all_tasks，栈在 _admit_goal_run execution.py:~3195）。

### 已查到的根因线索
1. `contextlib.suppress(asyncio.CancelledError, Exception)` 包 `await task` 会吞掉**外层**对当前协程的取消：veya/remote/execution.py:1750/4075/4090/4100，server/goal_run/runner.py:171。
   正确写法：`task.cancel(); try: await task except asyncio.CancelledError: if asyncio.current_task().cancelling(): raise  except Exception: log`（Python≥3.11 用 cancelling()；或用 `await asyncio.wait({task})` 代替 await task，不吞外层取消）。
2. server/goal_run/runner.py `_run_loop_and_finalize`（~1883 起）在 `await asyncio.wait(active...)`（~2147）被取消时，没有 finally 去取消并 await 子任务 → 子任务（含 bwrap 子进程）泄漏，teardown 卡住。加 try/finally：取消所有 active 任务并 `await asyncio.gather(*active, return_exceptions=True)`，然后重新抛出取消。
3. 再检查 bwrap 子进程 kill 路径：veya/remote/direct_exec.py、veya/remote/tool_adapter.py:4615–4721、executor_adapters.py:368–456。要求：取消时对进程组 kill（start_new_session/killpg），并 `await proc.wait()` 带超时，不能无限等待管道读。

### 验收（必须全部满足）
- 有真实代码改动并 git commit（message 以 `fix(sec): F10b` 开头）。
- 临时文件：`export TMPDIR=/data/soffy/tmp/sec`，不得写 /tmp、/home/soffy/gptlogs/tmp。
- 自测：`bash /home/soffy/gptlogs/close/f10x.sh`（串行8+并发3，结果在 /home/soffy/gptlogs/close/f10x/sum.txt），11 次全部 `13 passed` 才算过。
- 再跑 `.venv/bin/python -m pytest tests/remote -q -x -p no:cacheprovider --basetemp=/data/soffy/tmp/sec/f10r` 无新增失败。
- 最后把结果写入 SEC_REPORT_F10.md 末尾，末行写 `F10B DONE` 或 `F10B BLOCKED: 原因`。
- 不要改 main 仓库、不碰 docker、不碰其他项目。
