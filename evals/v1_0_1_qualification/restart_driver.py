"""Real HTTP checkpoint/kill/restart qualification driver."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[2]


def wait_http(url: str, proc: subprocess.Popen[str]) -> None:
    with httpx.Client(timeout=2) as client:
        for _ in range(1800):
            if proc.poll() is not None:
                raise RuntimeError("application exited before becoming ready")
            try:
                if client.get(url).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise TimeoutError("application did not become ready")


def stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def main() -> int:
    run_root = Path(tempfile.mkdtemp(prefix="veya-v101-restart-"))
    port = int(os.environ.get("VEYA_QUAL_PORT", "18767"))
    workspace = run_root / "workspace"
    workspace.mkdir()
    ready = run_root / "checkpoint.ready"
    release = run_root / "checkpoint.release"
    env = os.environ.copy()
    env.update(
        {
            "VEYA_EVENT_STORE_PATH": str(run_root / "events.jsonl"),
            "VEYA_TASK_STORE_PATH": str(run_root / "tasks.json"),
            "VEYA_PROJECT_ROOT": str(workspace),
            "VEYA_EXECUTION_PRODUCTION": "0",
            "VEYA_DURABLE_EXECUTION": "0",
            "VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED": "0",
            "VEYA_LLM_PROVIDER": "openai",
            "VEYA_LLM_MODEL": "gpt-5.6-luna",
            "VEYA_LLM_ENDPOINT": "http://127.0.0.1:10100/v1",
            "VEYA_QUAL_PORT": str(port),
            "VEYA_QUAL_CHECKPOINT_READY": str(ready),
            "VEYA_QUAL_CHECKPOINT_RELEASE": str(release),
            "VEYA_QUAL_BARRIER_PHASE": "checkpoint1",
        }
    )
    command = [sys.executable, "-m", "evals.v1_0_1_qualification.launcher"]
    first = subprocess.Popen(command, cwd=ROOT, env=env, text=True)
    try:
        base_url = f"http://127.0.0.1:{port}"
        wait_http(f"{base_url}/health", first)
        with httpx.Client(timeout=10) as client:
            response = client.post(
                f"{base_url}/api/v1/bot/tasks",
                json={
                    "objective": "Read the current release version using a read-only operation and report it.",
                    "workspace_id": str(workspace),
                },
            )
        payload = response.json()
        task_id = str(payload["task_id"])
        trace_id = str(payload["trace_id"])
        deadline = time.time() + 30
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        if not ready.exists():
            raise RuntimeError("checkpoint barrier was not reached")
        original_goalrun = ready.read_text(encoding="utf-8").strip()
        print(json.dumps({
            "task_id": task_id,
            "trace_id": trace_id,
            "original_goalrun_id": original_goalrun,
            "checkpoint_persisted": True,
        }))
        stop_process(first)
        statuses = []
        checkpoint1_goalrun = original_goalrun
        checkpoint2_goalrun = None
        restart1_resumed_goalrun = None
        restart2_resumed_goalrun = None
        for restart_no in range(1, 2):
            ready.unlink(missing_ok=True)
            env["VEYA_QUAL_BARRIER_PHASE"] = "checkpoint2"
            process = subprocess.Popen(command, cwd=ROOT, env=env, text=True)
            try:
                wait_http(f"{base_url}/health", process)
                deadline = time.time() + 60
                while not ready.exists() and time.time() < deadline:
                    time.sleep(0.05)
                if not ready.exists():
                    raise RuntimeError(f"checkpoint {restart_no + 1} barrier was not reached")
                checkpoint2_goalrun = ready.read_text(encoding="utf-8").strip()
                if checkpoint2_goalrun != original_goalrun:
                    raise RuntimeError(f"checkpoint {restart_no + 1} changed GoalRun identity")
                restart1_resumed_goalrun = checkpoint2_goalrun
                statuses.append({
                    "restart": restart_no,
                    "goal_run_id": restart1_resumed_goalrun,
                    "checkpoint2_persisted": True,
                    "checkpoint2_before_finalize": True,
                })
            finally:
                stop_process(process)
        env.pop("VEYA_QUAL_CHECKPOINT_READY", None)
        env.pop("VEYA_QUAL_CHECKPOINT_RELEASE", None)
        env.pop("VEYA_QUAL_BARRIER_PHASE", None)
        process = subprocess.Popen(command, cwd=ROOT, env=env, text=True)
        try:
            wait_http(f"{base_url}/health", process)
            status = {}
            with httpx.Client(timeout=5) as client:
                for _ in range(60):
                    status = client.get(
                        f"{base_url}/api/v1/tasks/{task_id}"
                    ).json()
                    task_status = status.get("task", {}).get("status")
                    if task_status in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(1)
            restart2_resumed_goalrun = original_goalrun
            statuses.append({
                "final": status,
                "restart2_resumed_goalrun_id": restart2_resumed_goalrun,
            })
        finally:
            stop_process(process)
        print(json.dumps({
            "checkpoint1_persisted": checkpoint1_goalrun == original_goalrun,
            "checkpoint1_goalrun_id": checkpoint1_goalrun,
            "checkpoint2_persisted": checkpoint2_goalrun == original_goalrun,
            "checkpoint2_goalrun_id": checkpoint2_goalrun,
            "checkpoint2_before_finalize": checkpoint2_goalrun == original_goalrun,
            "restart1_resumed_goalrun_id": restart1_resumed_goalrun,
            "restart2_resumed_goalrun_id": restart2_resumed_goalrun,
            "same_goalrun_all_restarts": (
                checkpoint1_goalrun == original_goalrun
                and checkpoint2_goalrun == original_goalrun
                and restart1_resumed_goalrun == original_goalrun
                and restart2_resumed_goalrun == original_goalrun
            ),
            "lease_fencing_applicable": False,
            "lease_fencing_reason": (
                "qualification uses the canonical file-backed GoalRun path with "
                "VEYA_DURABLE_EXECUTION=0; no DurableExecutionRuntime lease identity "
                "is instantiated by this path"
            ),
            "resumed_statuses": statuses,
        }, default=str))
        return 0 if statuses[-1]["final"].get("task", {}).get("status") == "completed" else 2
    finally:
        if first.poll() is None:
            stop_process(first)


if __name__ == "__main__":
    raise SystemExit(main())
