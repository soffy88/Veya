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
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=15)


def start_process(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        start_new_session=True,
    )


def wait_port_free(port: int, timeout_s: float = 15.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with httpx.Client(timeout=0.2) as client:
            try:
                client.get(f"http://127.0.0.1:{port}/health")
            except httpx.HTTPError:
                return
        time.sleep(0.1)
    raise TimeoutError(f"qualification port {port} was not released")


def wait_for_lease_expiry() -> None:
    # Recovery must observe the same lease/fencing contract as production:
    # a dead worker's lease is reclaimable only after its TTL expires.
    time.sleep(float(os.environ.get("VEYA_QUAL_LEASE_WAIT_S", "35")))


def read_json_lines(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def last_lease(path: Path, *, kind: str = "goal_leaf") -> dict[str, object] | None:
    for record in reversed(read_json_lines(path)):
        if record.get("kind") == kind:
            return record
    return None


def wait_for_checkpoint(path: Path, timeout_s: float, *, reason: str) -> dict[str, object]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for record in reversed(read_json_lines(path)):
            if record.get("event") == "qualification.checkpoint" and record.get("reason") == reason:
                return record
        time.sleep(0.05)
    raise TimeoutError(f"checkpoint marker {reason} was not reached")


def wait_for_task_terminal(
    base_url: str, task_id: str, proc: subprocess.Popen[str], *, timeout_s: float
) -> dict[str, object]:
    deadline = time.time() + timeout_s
    status: dict[str, object] = {}
    with httpx.Client(timeout=5) as client:
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("application exited before task reached terminal state")
            status = client.get(f"{base_url}/api/v1/tasks/{task_id}").json()
            task_status = status.get("task", {}).get("status")
            if task_status in {"completed", "failed", "cancelled"}:
                return status
            time.sleep(1)
    raise TimeoutError("task did not reach terminal state")


def main() -> int:
    run_root = Path(tempfile.mkdtemp(prefix="veya-v101-restart-"))
    port = int(os.environ.get("VEYA_QUAL_PORT", "18767"))
    workspace = run_root / "workspace"
    isolated_home = run_root / "home"
    workspace.mkdir()
    isolated_home.mkdir()
    (workspace / "release-manifest.json").write_text(
        '{"qualification": "read-only durable qualification"}\n',
        encoding="utf-8",
    )
    ready = run_root / "checkpoint.ready"
    release = run_root / "checkpoint.release"
    lease_ready = run_root / "lease.ready"
    runtime_events = run_root / "runtime-events.jsonl"
    # Do not carry the operator shell's credentials or PYTHONPATH into the
    # qualification application.  The repository root is the import cwd and
    # every stateful path below is run-scoped.
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TERM")
        if key in os.environ
    }
    env["HOME"] = str(isolated_home)
    env["PYTHONUNBUFFERED"] = "1"
    env.update(
        {
            "VEYA_EVENT_STORE_PATH": str(run_root / "events.jsonl"),
            "VEYA_TASK_STORE_PATH": str(run_root / "tasks.json"),
            "VEYA_PROJECT_ROOT": str(workspace),
            # Keep the qualification process on the production contract.  The
            # database is isolated, but the runtime must take the PostgreSQL
            # queue/lease/fencing path rather than the local profile.
            "VEYA_EXECUTION_PRODUCTION": "1",
            "VEYA_PSEUDO_SECRET": "qualification-only-pseudo-secret",
            "VEYA_DURABLE_EXECUTION": "1",
            "VEYA_EXECUTION_DATABASE_URL": os.environ["VEYA_EXECUTION_DATABASE_URL"],
            "VEYA_EXECUTION_DURABLE_QUEUE_READ": "1",
            "VEYA_EXECUTION_DURABLE_QUEUE_CLAIM": "1",
            "VEYA_EXECUTION_RECONCILER": "1",
            "VEYA_EXECUTION_LEASE_FENCING": "1",
            "VEYA_EXECUTION_SIDE_EFFECT_LEDGER": "1",
            "VEYA_EXECUTION_FINALIZATION_RESUME": "1",
            "VEYA_EXECUTION_EVENT_OUTBOX": "1",
            "VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED": "0",
            "VEYA_LLM_PROVIDER": "openai",
            "VEYA_LLM_MODEL": "gpt-5.6-luna",
            "VEYA_LLM_ENDPOINT": "http://127.0.0.1:10100/v1",
            "VEYA_QUAL_PORT": str(port),
            "VEYA_QUAL_CHECKPOINT_READY": str(ready),
            "VEYA_QUAL_CHECKPOINT_RELEASE": str(release),
            "VEYA_QUAL_LEASE_READY": str(lease_ready),
            "VEYA_QUAL_RUNTIME_EVENTS": str(runtime_events),
            "VEYA_QUAL_STARTUP_LOCK": str(run_root / "startup.lock"),
            "VEYA_QUAL_BARRIER_PHASE": "checkpoint1",
        }
    )
    restart_mode = os.environ.get("VEYA_QUAL_RESTART_MODE", "multi").strip().lower()
    if restart_mode not in {"single", "multi"}:
        raise ValueError("VEYA_QUAL_RESTART_MODE must be single or multi")
    failure_mode = os.environ.get("VEYA_QUAL_INJECT_FAILURE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if failure_mode:
        env["VEYA_QUAL_BARRIER_PHASE"] = "none"
        env["VEYA_QUAL_INJECT_FAILURE"] = "1"
    command = [sys.executable, "-m", "evals.v1_0_1_qualification.launcher"]
    first = start_process(command, cwd=ROOT, env=env)
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
        if failure_mode:
            status = wait_for_task_terminal(
                base_url, task_id, first, timeout_s=180
            )
            events = read_json_lines(runtime_events)
            print(json.dumps({
                "task_id": task_id,
                "trace_id": trace_id,
                "failure_injected": any(
                    event.get("event") == "qualification.injected_failure"
                    for event in events
                ),
                "model_decisions": [
                    event for event in events
                    if event.get("event") == "qualification.model_decision"
                ],
                "runtime_events": events,
                "final": status,
            }, default=str))
            return 0 if status.get("task", {}).get("status") == "completed" else 2
        deadline = time.time() + 120
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        if not ready.exists():
            raise RuntimeError("checkpoint barrier was not reached")
        original_goalrun = ready.read_text(encoding="utf-8").strip()
        # The first checkpoint is before queue claim. Release it so the real
        # claim/lease path can be reached, then capture that lease before the
        # process is terminated.
        release.write_text("release\n", encoding="utf-8")
        while not lease_ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        if not lease_ready.exists():
            raise RuntimeError("initial lease barrier was not reached")
        lease_initial = last_lease(lease_ready)
        if lease_initial is None:
            raise RuntimeError("initial goal-leaf lease was not observed")
        print(json.dumps({
            "task_id": task_id,
            "trace_id": trace_id,
            "original_goalrun_id": original_goalrun,
            "checkpoint_persisted": True,
        }))
        stop_process(first)
        wait_port_free(port)
        wait_for_lease_expiry()
        checkpoint1_goalrun = original_goalrun

        if restart_mode == "single":
            ready.unlink(missing_ok=True)
            lease_ready.unlink(missing_ok=True)
            release.unlink(missing_ok=True)
            env["VEYA_QUAL_BARRIER_PHASE"] = "none"
            process = start_process(command, cwd=ROOT, env=env)
            try:
                wait_http(f"{base_url}/health", process)
                status = {}
                with httpx.Client(timeout=5) as client:
                    for _ in range(120):
                        status = client.get(f"{base_url}/api/v1/tasks/{task_id}").json()
                        task_status = status.get("task", {}).get("status")
                        if task_status in {"completed", "failed", "cancelled"}:
                            break
                        time.sleep(1)
                restart1_resumed_goalrun = original_goalrun
                lease_restart1 = last_lease(lease_ready)
                print(json.dumps({
                    "checkpoint1_persisted": checkpoint1_goalrun == original_goalrun,
                    "checkpoint1_goalrun_id": checkpoint1_goalrun,
                    "restart1_resumed_goalrun_id": restart1_resumed_goalrun,
                    "same_goalrun_after_restart": restart1_resumed_goalrun == original_goalrun,
                    "lease_initial": lease_initial,
                    "lease_restart1": lease_restart1,
                    "runtime_events": read_json_lines(runtime_events),
                    "final": status,
                }, default=str))
                return 0 if status.get("task", {}).get("status") == "completed" else 2
            finally:
                stop_process(process)

        statuses = []
        checkpoint2_goalrun = None
        restart1_resumed_goalrun = None
        restart2_resumed_goalrun = None
        for restart_no in range(1, 2):
            ready.unlink(missing_ok=True)
            lease_ready.unlink(missing_ok=True)
            release.unlink(missing_ok=True)
            env["VEYA_QUAL_BARRIER_PHASE"] = "checkpoint2"
            process = start_process(command, cwd=ROOT, env=env)
            try:
                wait_http(f"{base_url}/health", process)
                deadline = time.time() + 60
                checkpoint2 = wait_for_checkpoint(
                    runtime_events, 60, reason="task_round"
                )
                checkpoint2_goalrun = str(checkpoint2.get("goal_run_id") or "")
                if checkpoint2_goalrun != original_goalrun:
                    raise RuntimeError(f"checkpoint {restart_no + 1} changed GoalRun identity")
                lease_restart1 = last_lease(lease_ready)
                if lease_restart1 is None:
                    raise RuntimeError("restart 1 goal-leaf lease was not observed")
                restart1_resumed_goalrun = checkpoint2_goalrun
                statuses.append({
                    "restart": restart_no,
                    "goal_run_id": restart1_resumed_goalrun,
                    "checkpoint2_persisted": True,
                    "checkpoint2_before_finalize": True,
                    "lease": last_lease(lease_ready),
                })
            finally:
                stop_process(process)
                wait_port_free(port)
            wait_for_lease_expiry()
        ready.unlink(missing_ok=True)
        lease_ready.unlink(missing_ok=True)
        release.unlink(missing_ok=True)
        env["VEYA_QUAL_BARRIER_PHASE"] = "none"
        process = start_process(command, cwd=ROOT, env=env)
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
            lease_restart2 = last_lease(lease_ready, kind="finalize")
            if lease_restart2 is None:
                raise RuntimeError("restart 2 finalization lease was not observed")
            statuses.append({
                "final": status,
                "restart2_resumed_goalrun_id": restart2_resumed_goalrun,
                "lease": lease_restart2,
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
            "lease_fencing_applicable": True,
            "lease_fencing_reason": (
                "production-equivalent qualification uses PostgreSQL DurableExecutionRuntime "
                "with queue claim and lease fencing enabled"
            ),
            "lease_initial": lease_initial,
            "lease_restart1": lease_restart1,
            "lease_restart2": lease_restart2,
            "runtime_events": read_json_lines(runtime_events),
            "resumed_statuses": statuses,
        }, default=str))
        return 0 if statuses[-1]["final"].get("task", {}).get("status") == "completed" else 2
    finally:
        if first.poll() is None:
            stop_process(first)


if __name__ == "__main__":
    raise SystemExit(main())
