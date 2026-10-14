"""P8: full L0 execution control plane acceptance.

End-to-end acceptance over the real Local2 substrate (no mocks for the
execution itself). Covers L0-A .. L0-J from the P3–P8 spec.

These are runtime/L0 tests, not unit tests: they launch real subprocesses,
write real files, and read real Git state.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tests._executor_health import _healthy_admission_executor  # noqa: F401 (autouse)

from server.goal_run.execution_admission import (
    AdmissionStatus,
    ExecutionAdmissionRequest,
    MutationIntent,
    get_admission,
    reset_admissions,
    reset_scopes,
)
from server.goal_run.execution_control import ExecutionControl
from server.goal_run.execution_recovery import ExecutionRecovery, RecoveryOutcome
from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
from server.goal_run.store import load_goal_run, save_goal_run
from veya.remote.action_gateway import ActionCategory, ActionGateway, classify_action
from veya.remote.approval import ApprovalStore
from veya.remote.executor_adapters import register_default_adapters
from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.tool_adapter import RemoteToolAdapter


def _repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "file.txt").write_text("hello\n", encoding="utf-8")
    (path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (path / "tests").mkdir()
    (path / "tests" / "test_smoke.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _session(path: Path) -> RemoteSession:
    now = time.time()
    return RemoteSession(
        session_id="p8-session",
        principal="p8-owner",
        token_id="p8-token",
        workspaces=(str(path.resolve()),),
        active_workspace=str(path.resolve()),
        permissions=RemotePermissions(
            read=True,
            write=True,
            shell=True,
            git=True,
            network=True,
            destructive=False,
            service_control=True,
        ),
        created_at=now,
        expires_at=now + 3600,
    )


@pytest.fixture(autouse=True)
def _reset():
    reset_admissions()
    reset_scopes()
    yield
    reset_admissions()
    reset_scopes()


def _counting_adapter(tmp_path: Path) -> tuple[RemoteToolAdapter, list[int]]:
    """Adapter whose approval store counts verify_and_consume calls."""
    adapter = RemoteToolAdapter(None)
    store = ApprovalStore()
    adapter.action_gateway = ActionGateway(approval_store=store)
    calls = [0]
    original = store.verify_and_consume

    def counting(*a, **k):
        calls[0] += 1
        return original(*a, **k)

    store.verify_and_consume = counting  # type: ignore[method-assign]
    return adapter, calls


def _goal(
    tmp_path: Path, *, executor: str = "local2", status: GoalStatus = GoalStatus.pending_execution
) -> str:
    goal_id = f"goal_{uuid.uuid4().hex[:8]}"
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    state = GoalRunState(
        goal_id=goal_id, goal_text="p8", status=status, requested_executor=executor
    )
    state.tasks[task_id] = TaskNode(
        id=task_id,
        title="t",
        instruction="i",
        acceptance=["a"],
        depends_on=[],
        assignee=executor,
        status=TaskStatus.pending,
    )
    save_goal_run(state, str(tmp_path))
    return goal_id


# ── L0-A: normal direct execution (DIRECT, approval_calls = 0) ──────────


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("rg") is None, reason="file.search needs ripgrep (rg) on PATH")
async def test_L0_A_normal_direct_execution_zero_approvals(tmp_path: Path):
    _repo(tmp_path)
    adapter, calls = _counting_adapter(tmp_path)
    session = _session(tmp_path)

    # read / search / write / patch
    r = await adapter._call_impl(
        session, "file.read", {"path": "file.txt", "workspace": str(tmp_path)}
    )
    assert r.ok, r
    r = await adapter._call_impl(
        session, "file.search", {"pattern": "hello", "workspace": str(tmp_path)}
    )
    assert r.ok, r
    r = await adapter._call_impl(
        session,
        "file.write",
        {"path": "new.txt", "content": "world\n", "workspace": str(tmp_path)},
    )
    assert r.ok, r

    # shell / test / build (real subprocesses)
    r = await adapter._call_impl(
        session, "shell.exec", {"command": "echo l0a", "workspace": str(tmp_path), "wait": True}
    )
    assert r.ok, r
    r = await adapter._call_impl(
        session,
        "test.run",
        {"command": "python3 -c \"print('tests ok')\"", "workspace": str(tmp_path), "wait": True},
    )
    assert r.ok, r
    r = await adapter._call_impl(
        session,
        "build.run",
        {"command": "python3 -c \"print('build ok')\"", "workspace": str(tmp_path), "wait": True},
    )
    assert r.ok, r

    # git status/diff
    r = await adapter._call_impl(session, "git.status", {"workspace": str(tmp_path)})
    assert r.ok, r
    r = await adapter._call_impl(session, "git.diff", {"workspace": str(tmp_path)})
    assert r.ok, r

    # Normal direct execution never touches the approval store.
    assert calls[0] == 0, f"normal operation called verify_and_consume {calls[0]} times"


# ── L0-B: full GoalRun chain (live) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_L0_B_full_goalrun_chain_completes(tmp_path: Path):
    from server.goal_run import bind_execution, create_pending_run

    register_default_adapters()
    # Full chain: GoalRun -> PermissionEngine -> Admission -> Scope -> Binding -> Receipt
    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="l0b",
        session_id="s",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    bind_execution(create, "ex_l0b", str(tmp_path), admit=True)
    receipts = (
        tmp_path / ".veya-project" / "goal-runs" / create.goal_run_id / "execution_receipts.jsonl"
    )
    assert receipts.exists()

    # Real Local2 execution over the bound run.
    control = ExecutionControl(str(tmp_path))
    result = await control.run_task(create.goal_run_id, command="echo l0b", executor_id="local2")
    assert result.goal_status == GoalStatus.completed


# ── L0-C: async control (start/status/wait/stream) ──────────────────────


@pytest.mark.asyncio
async def test_L0_C_async_control_lifecycle(tmp_path: Path):
    register_default_adapters()
    goal_id = _goal(tmp_path)
    control = ExecutionControl(str(tmp_path))

    # status before start
    pre = await control.status(goal_id)
    assert pre.goal_status == GoalStatus.pending_execution

    await control.run_task(goal_id, command="echo l0c", executor_id="local2")

    # wait returns terminal immediately
    final = await control.wait(goal_id, timeout=5)
    assert final.goal_status == GoalStatus.completed

    events, cursor = await control.stream(goal_id, cursor=0)
    assert cursor >= len(events) >= 3
    types = [e.event_type for e in events]
    assert "PROCESS_STARTED" in types and "GOALRUN_COMPLETED" in types


# ── L0-D: steer records evidence ────────────────────────────────────────


@pytest.mark.asyncio
async def test_L0_D_steer_records_evidence(tmp_path: Path):
    register_default_adapters()
    goal_id = _goal(tmp_path, status=GoalStatus.running)
    control = ExecutionControl(str(tmp_path))
    result = await control.steer(goal_id, "continue with the fix")
    assert result.goal_status == GoalStatus.running
    events, _ = await control.stream(goal_id, cursor=0)
    assert any(e.event_type == "STEER_REQUESTED" for e in events)
    state = load_goal_run(str(tmp_path), goal_id)
    assert any("steer:" in item for item in state.unfinished_work)


# ── L0-E: real cancel of a long-running process ─────────────────────────


@pytest.mark.asyncio
async def test_L0_E_real_cancel_terminates_process(tmp_path: Path):
    _repo(tmp_path)
    adapter, _ = _counting_adapter(tmp_path)
    session = _session(tmp_path)

    started = await adapter._call_impl(
        session,
        "shell.exec",
        {"command": "sleep 30", "workspace": str(tmp_path)},
    )
    assert started.ok, started
    execution_id = started.execution_id
    assert execution_id

    status = await adapter._call_impl(session, "process.status", {"execution_id": execution_id})
    assert status.ok, status

    cancelled = await adapter._call_impl(session, "process.cancel", {"execution_id": execution_id})
    assert cancelled.ok, cancelled

    # Poll until the durable record reports a terminal cancelled state.
    deadline = time.time() + 10
    final_state = None
    while time.time() < deadline:
        snap = await adapter._call_impl(session, "process.status", {"execution_id": execution_id})
        payload = snap.result if isinstance(snap.result, dict) else {}
        final_state = payload.get("status") or payload.get("phase")
        if final_state in ("CANCELLED", "FAILED", "COMPLETED"):
            break
        await asyncio.sleep(0.2)
    assert final_state == "CANCELLED", f"expected CANCELLED, got {final_state}"


# ── L0-F: recovery of an interrupted run ────────────────────────────────


@pytest.mark.asyncio
async def test_L0_F_recovery_of_interrupted_run(tmp_path: Path):
    from server.goal_run import bind_execution, create_pending_run

    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="l0f",
        session_id="s",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    bind_execution(create, "ex_l0f", str(tmp_path), admit=True)
    state = load_goal_run(str(tmp_path), create.goal_run_id)
    state.status = GoalStatus.running
    save_goal_run(state, str(tmp_path))

    reset_admissions()
    reset_scopes()
    verdict = await ExecutionRecovery(str(tmp_path)).reconcile(create.goal_run_id)
    assert verdict.outcome in (
        RecoveryOutcome.RESUMABLE,
        RecoveryOutcome.RECOVERY_REQUIRED,
        RecoveryOutcome.LOST,
    )
    assert verdict.outcome.value != "COMPLETED"
    after = load_goal_run(str(tmp_path), create.goal_run_id)
    assert after.status != GoalStatus.completed


# ── L0-G: real concurrency (A1 running, A2 queued, B1 running) ──────────


def test_L0_G_concurrency_queue_and_parallel(tmp_path: Path):
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    adm = get_admission(str(tmp_path))

    def req(goal, ws, install, queue=False):
        return ExecutionAdmissionRequest(
            goal_run_id=goal,
            executor_id="codex",
            executor_installation_id=install,
            project_id="p",
            workspace_id=str(ws),
            project_root=str(tmp_path),
            workspace_root=str(ws),
            mutation_intent=MutationIntent.MUTATION,
            queue_if_busy=queue,
        )

    a1 = adm.request(req("A1", ws_a, "install-1"))
    a2 = adm.request(req("A2", ws_a, "install-1", queue=True))
    b1 = adm.request(req("B1", ws_b, "install-2"))
    assert a1.status == AdmissionStatus.ADMITTED
    assert a2.status == AdmissionStatus.QUEUED
    assert b1.status == AdmissionStatus.ADMITTED


# ── L0-H: privileged boundary + normal approval_calls = 0 ───────────────


def test_L0_H_privileged_boundary(tmp_path: Path):
    session = _session(tmp_path)
    cases = [
        ("sudo apt install vim", ActionCategory.HUMAN_GATED),
        # Local2 R3 own-machine policy (main 4bd942bd): plain reads are open.
        ("cat /etc/passwd", ActionCategory.AUTO_OPEN),
        ("systemctl restart ssh.service", ActionCategory.HUMAN_GATED),
        ("git push --force origin main", ActionCategory.HUMAN_GATED),
        ("echo normal", ActionCategory.AUTO_OPEN),
    ]
    for cmd, expected in cases:
        cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
        assert cl.category == expected, f"{cmd}: {cl.category} != {expected}"

    # Local2 R3 own-machine policy (main 4bd942bd): reading outside the
    # workspace is open, but a host write is never silently allowed.
    from veya.remote.action_gateway import ActionGateway

    gw = ActionGateway(approval_store=ApprovalStore())
    ok, code, _, _ = gw.check_action(
        "file.write", {"path": "/etc/veya-l0h.txt", "content": "x"}, session, str(tmp_path)
    )
    assert ok is False and str(code) == "APPROVAL_REQUIRED"


# ── L0-I: restart reconciliation is explicit (no unknown/silent loss) ───


@pytest.mark.asyncio
async def test_L0_I_restart_reconciliation_explicit(tmp_path: Path):
    from server.goal_run import bind_execution, create_pending_run

    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="l0i",
        session_id="s",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    bind_execution(create, "ex_l0i", str(tmp_path), admit=True)
    state = load_goal_run(str(tmp_path), create.goal_run_id)
    state.status = GoalStatus.running
    save_goal_run(state, str(tmp_path))

    reset_admissions()
    reset_scopes()
    verdict = await ExecutionRecovery(str(tmp_path)).reconcile(create.goal_run_id)
    # Explicit outcome — never "unknown".
    assert verdict.outcome in set(RecoveryOutcome)
    assert verdict.reason


# ── L0-J: receipt completeness ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_L0_J_receipt_has_required_fields(tmp_path: Path):
    from server.goal_run import bind_execution, create_pending_run

    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="l0j",
        session_id="s",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    bind_execution(create, "ex_l0j", str(tmp_path), admit=True)
    receipts = (
        tmp_path / ".veya-project" / "goal-runs" / create.goal_run_id / "execution_receipts.jsonl"
    )
    row = json.loads(receipts.read_text().splitlines()[0])
    required = {
        "goal_run_id",
        "admission_id",
        "binding_id",
        "executor_id",
        "installation_id",
        "provider_session_id",
        "provider_run_id",
        "workspace_id",
        "permission_decision",
        "resource_leases",
        "start_time",
        "final_state",
    }
    missing = required - set(row)
    assert not missing, f"receipt missing fields: {missing}"
