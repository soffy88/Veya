from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def test_daemon_process_kill_resume_preserves_goalrun_owner(tmp_path: Path):
    env = os.environ.copy()
    env["VEYA_PROJECT_ROOT"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(Path.cwd()),
            str(Path.cwd() / "platform/3O/oprim"),
            str(Path.cwd() / "platform/3O/oskill"),
            str(Path.cwd() / "platform/3O/omodul"),
            str(Path.cwd() / "platform/3O/obase"),
            str(Path.cwd() / "platform/3O/oservi"),
        ]
    )
    child = """
import asyncio
from server.daemon_goal_run import DaemonGoalRunBridge
from veya.oservi.daemon_engine import DaemonEngine

class SlowLlm:
    async def complete(self, messages, **kwargs):
        await asyncio.sleep(30)
        return {"choices": [{"message": {"role": "assistant", "content": "never"}}]}

async def main():
    engine = DaemonEngine(llm=SlowLlm(), goal_bridge=DaemonGoalRunBridge())
    state = await engine.create_task("process-kill-resume")
    print(state.task_id, flush=True)
    await asyncio.sleep(60)

asyncio.run(main())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    task_id = process.stdout.readline().strip()
    assert task_id
    deadline = time.time() + 10
    while time.time() < deadline and not list(
        (tmp_path / ".veya-project" / "goal-runs").glob("*/taskgraph.json")
    ):
        time.sleep(0.05)
    process.kill()
    process.wait(timeout=5)

    recovery = f"""
import asyncio
from server.daemon_goal_run import DaemonGoalRunBridge
from veya.oservi.daemon_engine import DaemonEngine

class FastLlm:
    async def complete(self, messages, **kwargs):
        return {{"choices": [{{"message": {{"role": "assistant", "content": "resumed"}}}}]}}

async def main():
    engine = DaemonEngine(llm=FastLlm(), goal_bridge=DaemonGoalRunBridge())
    await engine.start()
    for _ in range(300):
        status = await engine.status({task_id!r})
        if status["status"] in {{"completed", "failed"}}:
            print(status, flush=True)
            return
        await asyncio.sleep(0.02)
    raise SystemExit("daemon recovery timed out")

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", recovery], env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "'status': 'completed'" in result.stdout
    assert "'task_id':" in result.stdout
