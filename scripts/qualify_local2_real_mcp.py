#!/usr/bin/env python3
"""Run Local2 fault/restart qualification through the real MCP HTTP surface.

The fault selector is process environment owned by the qualification harness;
it is never sent as a MCP argument and is disabled when this process exits.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8790/mcp"
WORKSPACE = str(ROOT)
SERVICE = "veya-remote-mcp.service"
TERMINAL = {"COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED", "BLOCKED"}


def token() -> str:
    return (
        os.environ.get("VEYA_REMOTE_TOKEN", "").strip()
        or Path("~/.veya/remote_mcp_token.secret").expanduser().read_text(encoding="utf-8").strip()
    )


def systemd(*args: str) -> None:
    subprocess.run(
        ["systemctl", "--user", *args],
        check=True,
        stdout=subprocess.DEVNULL,
        timeout=15,
    )


def wait_ready() -> None:
    with httpx.Client(timeout=1) as client:
        for _ in range(100):
            try:
                if client.get("http://127.0.0.1:8790/mcp/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise RuntimeError("Local2 MCP service did not become ready")


def restart(*, force: bool = False) -> None:
    if force:
        subprocess.run(
            ["systemctl", "--user", "kill", "--kill-who=all", "--signal=KILL", SERVICE],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        systemd("reset-failed", SERVICE)
        systemd("start", SERVICE)
    else:
        systemd("restart", SERVICE)
    wait_ready()


def set_fault(*, run_id: str, control: Path, checkpoint: str, action: str) -> None:
    systemd(
        "set-environment",
        "VEYA_QUALIFICATION_FAULT_INJECTION=1",
        f"VEYA_QUALIFICATION_RUN_ID={run_id}",
        f"VEYA_QUALIFICATION_CONTROL_DIR={control}",
        f"VEYA_QUALIFICATION_FAULT_CHECKPOINT={checkpoint}",
        f"VEYA_QUALIFICATION_FAULT_ACTION={action}",
    )
    restart()


def clear_fault() -> None:
    systemd(
        "unset-environment",
        "VEYA_QUALIFICATION_FAULT_INJECTION",
        "VEYA_QUALIFICATION_RUN_ID",
        "VEYA_QUALIFICATION_CONTROL_DIR",
        "VEYA_QUALIFICATION_FAULT_CHECKPOINT",
        "VEYA_QUALIFICATION_FAULT_ACTION",
    )
    restart(force=True)


def service_pid() -> str:
    return subprocess.run(
        ["systemctl", "--user", "show", SERVICE, "-p", "MainPID", "--value"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()


def rpc(session_id: str | None, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if session_id and method in {"tools/call", "tools/list"}:
        params = {**params, "session_id": session_id}
    headers = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
    with httpx.Client(timeout=60) as client:
        response = client.post(
            URL,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        return response.json()


def initialize() -> str:
    result = rpc(
        None, "initialize", {"workspace": WORKSPACE, "clientInfo": {"name": "local2-qualification"}}
    )
    return str((result.get("result") or {}).get("sessionId") or "")


def call(session_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    body = rpc(session_id, "tools/call", {"name": name, "arguments": arguments})
    structured = (body.get("result") or {}).get("structuredContent")
    return structured if isinstance(structured, dict) else {"ok": False, "raw": body}


def dispatch(
    session_id: str, dispatch_id: str, task: str, *, worker: str = "opencode", timeout: int = 30
) -> dict[str, Any]:
    return call(
        session_id,
        "worker.dispatch",
        {
            "dispatch_id": dispatch_id,
            "workspace": WORKSPACE,
            "tasks": [
                {"worker": worker, "task": task, "timeout_sec": timeout, "task_kind": "READ"}
            ],
        },
    )


def status(session_id: str, execution_id: str) -> dict[str, Any]:
    return call(
        session_id, "process.status", {"execution_id": execution_id, "workspace": WORKSPACE}
    )


def cancel(session_id: str, execution_id: str) -> dict[str, Any]:
    return call(
        session_id, "process.cancel", {"execution_id": execution_id, "workspace": WORKSPACE}
    )


def wait_terminal(session_id: str, execution_id: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.time() + timeout
    result: dict[str, Any] = {}
    while time.time() < deadline:
        result = status(session_id, execution_id)
        if result.get("status") in TERMINAL:
            return result
        time.sleep(0.25)
    raise TimeoutError(f"execution did not become terminal: {execution_id}")


def handle(result: dict[str, Any]) -> dict[str, Any]:
    return result.get("result", result)


def durable(handle_data: dict[str, Any]) -> dict[str, Any]:
    execution_id = handle_data.get("execution_id")
    goal_run_id = handle_data.get("goal_run_id")
    result: dict[str, Any] = {"handle": handle_data}
    if execution_id:
        path = Path("~/.veya/remote_executions").expanduser() / f"{execution_id}.json"
        result["execution"] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        )
    if goal_run_id:
        path = ROOT / ".veya-project" / "goal-runs" / str(goal_run_id) / "taskgraph.json"
        result["goalrun"] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return result


def write_fake_worker() -> Path:
    path = Path(tempfile.mkdtemp(prefix="veya-local2-fake-")) / "opencode"
    path.write_text(
        '#!/bin/sh\ncase "$*" in *SLEEP*) exec sleep 30;; *FAIL*) exit 23;; *) printf \'%s\\n\' \'{"type":"text","text":"LOCAL2_FAULT_OK"}\';; esac\n',
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def report(case: str, result: dict[str, Any]) -> None:
    print(json.dumps({"case": case, **result}, sort_keys=True, default=str), flush=True)


def wait_checkpoint(control: Path, timeout: float = 30) -> dict[str, Any]:
    marker = control / "checkpoint.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker.exists():
            return json.loads(marker.read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise TimeoutError(f"checkpoint was not reached: {control}")


def dispatch_thread(
    session: str, dispatch_id: str, task: str, *, timeout: int = 30
) -> tuple[threading.Thread, dict[str, Any]]:
    result: dict[str, Any] = {}

    def request() -> None:
        try:
            result.update(dispatch(session, dispatch_id, task, timeout=timeout))
        except Exception as exc:
            result["client_error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=request, daemon=True)
    thread.start()
    return thread, result


def run_pause_restart(
    case: str,
    *,
    checkpoint: str,
    task: str = "normal",
    timeout: int = 30,
    fake: Path,
) -> None:
    control = Path(tempfile.mkdtemp(prefix=f"veya-local2-{case.lower()}-"))
    dispatch_id = f"local2-{case.lower()}"
    try:
        set_fault(run_id=dispatch_id, control=control, checkpoint=checkpoint, action="PAUSE")
        session = initialize()
        thread, result = dispatch_thread(session, dispatch_id, task, timeout=timeout)
        marker = wait_checkpoint(control)
        state = marker.get("state", {})
        before = durable(state)
        clear_fault()
        thread.join(timeout=5)
        recovered = result
        if not recovered.get("execution_id") and state.get("execution_id"):
            session = initialize()
            recovered = status(session, state["execution_id"])
        after = durable(
            {
                "execution_id": recovered.get("execution_id") or state.get("execution_id"),
                "goal_run_id": recovered.get("goal_run_id") or state.get("goal_run_id"),
                "goal_task_id": recovered.get("goal_task_id") or state.get("goal_task_id"),
            }
        )
        execution_id = recovered.get("execution_id") or state.get("execution_id")
        if execution_id:
            session = initialize()
            terminal = wait_terminal(session, execution_id, timeout=60)
        else:
            terminal = recovered
        report(
            case,
            {
                "checkpoint": marker,
                "before_restart": before,
                "after_restart": after,
                "result": recovered,
                "terminal": terminal,
                "same_ids": {
                    key: not state.get(key) or state.get(key) == recovered.get(key)
                    for key in ("execution_id", "goal_run_id", "goal_task_id")
                },
            },
        )
    finally:
        with contextlib.suppress(Exception):
            clear_fault()
        shutil.rmtree(control, ignore_errors=True)


def run_before_execution_persist_restart() -> int:
    """Bounded single-case crash-window qualification.

    The old MCP request is intentionally abandoned after checkpoint ACK.  The
    only post-kill observations use the new service generation and durable
    GoalRun files; no old request/thread is part of the handshake.
    """
    control = Path(tempfile.mkdtemp(prefix="veya-local2-before-execution-persist-"))
    dispatch_id = "local2-before-execution-persist-restart"
    timeline: dict[str, float] = {}
    result: dict[str, Any] = {}
    timeline["REQUEST_SENT"] = time.time()
    try:
        set_fault(
            run_id=dispatch_id,
            control=control,
            checkpoint="BEFORE_EXECUTION_PERSIST",
            action="PAUSE",
        )
        old_pid = service_pid()
        session = initialize()
        thread, result = dispatch_thread(session, dispatch_id, "normal")
        marker = wait_checkpoint(control, timeout=30)
        timeline["CHECKPOINT_REACHED"] = time.time()
        state = marker["state"]
        before = durable(state)
        (control / "ack").touch()
        timeline["HARNESS_ACK"] = time.time()
        # Unset first, then hard-kill the blocked generation.  This never waits
        # for the old HTTP request, release, stream, or server shutdown hook.
        systemd(
            "unset-environment",
            "VEYA_QUALIFICATION_FAULT_INJECTION",
            "VEYA_QUALIFICATION_RUN_ID",
            "VEYA_QUALIFICATION_CONTROL_DIR",
            "VEYA_QUALIFICATION_FAULT_CHECKPOINT",
            "VEYA_QUALIFICATION_FAULT_ACTION",
        )
        timeline["SERVICE_STOP_BEGIN"] = time.time()
        restart(force=True)
        timeline["SERVICE_HEALTHY"] = time.time()
        new_pid = service_pid()
        timeline["NEW_PID"] = time.time()
        new_session = initialize()
        timeline["NEW_MCP_CONNECTED"] = time.time()
        after = durable(state)
        timeline["RECOVERY_END"] = time.time()
        thread.join(timeout=0.5)
        report(
            "BEFORE_EXECUTION_PERSIST_RESTART",
            {
                "timeline": timeline,
                "old_pid": old_pid,
                "new_pid": new_pid,
                "new_generation": new_session,
                "before_restart": before,
                "after_restart": after,
                "checkpoint_state": state,
                "old_request_finished": not thread.is_alive(),
                "client_result": result,
                "missing_execution_is_valid": not state.get("execution_id"),
                "orphan_free": bool(
                    after.get("goalrun") and after["goalrun"].get("status") == "failed"
                ),
            },
        )
        return 0
    finally:
        with contextlib.suppress(Exception):
            clear_fault()
        shutil.rmtree(control, ignore_errors=True)


def run_matrix() -> int:
    fake = write_fake_worker()
    try:
        systemd("set-environment", f"VEYA_OPENCODE_BIN={fake}")
        clear_fault()
        session = initialize()
        normal_id = "local2-normal-success"
        normal = dispatch(session, normal_id, "normal")
        normal_terminal = wait_terminal(session, normal["execution_id"])
        report(
            "01_NORMAL_SUCCESS",
            {"handle": normal, "terminal": normal_terminal, "durable": durable(normal)},
        )

        invalid = dispatch(session, "local2-admission-failure", "normal", worker="not-an-executor")
        report(
            "02_ADMISSION_FAILURE", {"result": invalid, "known": not invalid.get("execution_id")}
        )

        run_pause_restart(
            "03_GOALRUN_PRECREATE_FAILURE", checkpoint="BEFORE_GOALRUN_PRECREATE", fake=fake
        )
        run_pause_restart(
            "04_EXECUTION_PERSIST_FAILURE", checkpoint="BEFORE_EXECUTION_PERSIST", fake=fake
        )
        run_pause_restart("05_WORKER_LAUNCH_FAILURE", checkpoint="BEFORE_WORKER_LAUNCH", fake=fake)

        provider = dispatch(session, "local2-provider-failure", "FAIL")
        report(
            "06_PROVIDER_FAILURE",
            {"handle": provider, "terminal": wait_terminal(session, provider["execution_id"])},
        )

        bad_headers = {"Authorization": "Bearer invalid", "Content-Type": "application/json"}
        with httpx.Client(timeout=5) as client:
            response = client.post(
                URL,
                headers=bad_headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
        report(
            "07_AUTH_FAILURE",
            {"http_status": response.status_code, "known": response.status_code in {401, 403}},
        )

        timed = dispatch(session, "local2-timeout", "SLEEP", timeout=1)
        report(
            "08_EXECUTION_TIMEOUT",
            {"handle": timed, "terminal": wait_terminal(session, timed["execution_id"], 60)},
        )

        running = dispatch(session, "local2-cancel-running", "SLEEP", timeout=30)
        cancelled = cancel(session, running["execution_id"])
        report(
            "10_CANCEL_RUNNING",
            {
                "handle": running,
                "cancel": cancelled,
                "terminal": wait_terminal(session, running["execution_id"]),
            },
        )

        for case in (
            "11_CLIENT_TIMEOUT_AFTER_ADMISSION",
            "12_MCP_DISCONNECT_AFTER_ADMISSION",
            "13_RESPONSE_LOST_AFTER_ADMISSION",
        ):
            dispatch_id = f"local2-{case.lower()}"
            try:
                with httpx.Client(timeout=0.01) as client:
                    client.post(
                        URL,
                        headers={
                            "Authorization": f"Bearer {token()}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"workspace": WORKSPACE},
                        },
                    )
            except httpx.HTTPError:
                pass
            retry = dispatch(session, dispatch_id, "normal")
            replay = dispatch(session, dispatch_id, "normal")
            report(
                case,
                {
                    "first": retry,
                    "retry": replay,
                    "same": all(
                        retry.get(k) == replay.get(k)
                        for k in ("execution_id", "goal_run_id", "goal_task_id")
                    ),
                    "terminal": wait_terminal(session, retry["execution_id"]),
                },
            )

        run_pause_restart(
            "14_TERMINAL_PERSISTENCE_DELAY", checkpoint="BEFORE_TERMINAL_PERSIST", fake=fake
        )
        run_pause_restart(
            "15_PARENT_RECONCILIATION_DELAY", checkpoint="BEFORE_PARENT_RECONCILIATION", fake=fake
        )
        return 0
    finally:
        with contextlib.suppress(Exception):
            clear_fault()
        with contextlib.suppress(Exception):
            systemd("unset-environment", "VEYA_OPENCODE_BIN")
            restart()
        shutil.rmtree(fake.parent, ignore_errors=True)




def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case", default="smoke", choices=("smoke", "matrix", "before-execution-persist")
    )
    args = parser.parse_args()
    if args.case == "before-execution-persist":
        return run_before_execution_persist_restart()
    if args.case == "matrix":
        return run_matrix()
    if args.case != "smoke":
        return 2
    # The smoke path proves activation, checkpoint publication, and cleanup.
    control = Path(tempfile.mkdtemp(prefix="veya-local2-control-"))
    fake = write_fake_worker()
    try:
        systemd("set-environment", f"VEYA_OPENCODE_BIN={fake}")
        set_fault(
            run_id="local2-smoke",
            control=control,
            checkpoint="AFTER_GOALRUN_PRECREATE",
            action="PAUSE",
        )
        session = initialize()
        result: dict[str, Any] = {}

        def request() -> None:
            nonlocal result
            try:
                result = dispatch(session, "local2-smoke", "smoke")
            except Exception as exc:  # the harness intentionally kills this request
                result = {"client_error": f"{type(exc).__name__}: {exc}"}

        thread = threading.Thread(target=request, daemon=True)
        thread.start()
        checkpoint_file = control / "checkpoint.json"
        deadline = time.time() + 20
        while not checkpoint_file.exists() and time.time() < deadline:
            time.sleep(0.1)
        if not checkpoint_file.exists():
            raise RuntimeError("qualification checkpoint was not reached")
        payload = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        report(
            "AFTER_GOALRUN_PRECREATE", {"checkpoint": payload, "durable": durable(payload["state"])}
        )
        (control / "release").touch()
        thread.join(timeout=2)
        clear_fault()
        systemd("unset-environment", "VEYA_OPENCODE_BIN")
        restart()
        return 0
    finally:
        with contextlib.suppress(Exception):
            clear_fault()
        with contextlib.suppress(Exception):
            systemd("unset-environment", "VEYA_OPENCODE_BIN")
            restart()
        shutil.rmtree(fake.parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
