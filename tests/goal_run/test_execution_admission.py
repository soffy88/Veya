"""P1 execution admission / executor binding tests (spec §20)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from server.goal_run import bind_execution, create_pending_run
from server.goal_run.execution_admission import (
    AdmissionStatus,
    BindingStatus,
    ExecutionAdmission,
    ExecutionAdmissionRequest,
    MutationIntent,
    get_admission,
    reset_admissions,
)
from server.goal_run.models import GoalStatus
from server.goal_run.store import load_goal_run
from tests._executor_health import _healthy_admission_executor  # noqa: F401 (autouse)


@pytest.fixture(autouse=True)
def _reset_admission_registry():
    reset_admissions()
    yield
    reset_admissions()


def _req(
    tmp_path: Path,
    *,
    goal: str = "g1",
    executor: str = "codex",
    intent: MutationIntent = MutationIntent.MUTATION,
    queue: bool = False,
    workspace: Path | None = None,
    isolated: bool = False,
    installation: str = "install-1",
) -> ExecutionAdmissionRequest:
    ws = workspace or tmp_path
    return ExecutionAdmissionRequest(
        goal_run_id=goal,
        executor_id=executor,
        executor_installation_id=installation,
        project_id="proj",
        workspace_id=str(ws),
        project_root=str(tmp_path),
        workspace_root=str(ws),
        mutation_intent=intent,
        queue_if_busy=queue,
        workspace_isolated=isolated,
    )


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


# ---------------- Admission ----------------


def test_A1_single_mutation_admitted(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    result = adm.request(_req(tmp_path))
    assert result.status == AdmissionStatus.ADMITTED
    assert result.resource_leases
    assert result.expires_at


def test_A2_second_mutation_queued_when_queue_true(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    adm.request(_req(tmp_path, goal="g1"))
    second = adm.request(_req(tmp_path, goal="g2", queue=True))
    assert second.status == AdmissionStatus.QUEUED


def test_A3_second_mutation_denied_when_queue_false(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    adm.request(_req(tmp_path, goal="g1"))
    second = adm.request(_req(tmp_path, goal="g2", queue=False))
    assert second.status == AdmissionStatus.DENIED
    assert "resource_conflict" in second.reason


def test_A4_read_read_concurrent(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    a = adm.request(_req(tmp_path, goal="g1", intent=MutationIntent.READ_ONLY))
    b = adm.request(_req(tmp_path, goal="g2", intent=MutationIntent.READ_ONLY))
    assert a.status == AdmissionStatus.ADMITTED
    assert b.status == AdmissionStatus.ADMITTED


def test_A5_mutation_release_readmit(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    first = adm.request(_req(tmp_path, goal="g1"))
    assert adm.request(_req(tmp_path, goal="g2")).status == AdmissionStatus.DENIED
    assert adm.release(first.admission_id, project_root=str(tmp_path))
    assert adm.request(_req(tmp_path, goal="g3")).status == AdmissionStatus.ADMITTED


def test_A6_stale_project_identity_rejected(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    result = adm.request(_req(tmp_path), registered_project_root=str(other))
    assert result.status == AdmissionStatus.DENIED
    assert result.reason == "project_identity_mismatch"


def test_A7_stale_workspace_rejected(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    other = tmp_path / "ws2"
    other.mkdir()
    result = adm.request(_req(tmp_path), registered_workspace_root=str(other))
    assert result.status == AdmissionStatus.DENIED
    assert result.reason == "workspace_identity_mismatch"


def test_mutation_isolated_worktree_parallel(tmp_path):
    _git_init(tmp_path)
    w1 = tmp_path / "w1"
    w2 = tmp_path / "w2"
    w1.mkdir()
    w2.mkdir()
    adm = ExecutionAdmission(str(tmp_path))
    a = adm.request(_req(tmp_path, goal="g1", workspace=w1, isolated=True))
    b = adm.request(
        _req(tmp_path, goal="g2", workspace=w2, isolated=True, installation="install-2")
    )
    assert a.status == AdmissionStatus.ADMITTED
    assert b.status == AdmissionStatus.ADMITTED


# ---------------- Binding ----------------


def test_B1_B2_B3_session_and_run_are_distinct(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    result = adm.request(_req(tmp_path))
    binding = adm.create_binding(
        executor_id="codex",
        installation_id="install-1",
        provider_id="codex",
        goal_run_id="g1",
        admission_id=result.admission_id,
        provider_session_id="sess_1",
        provider_run_id="run_1",
        project_root=str(tmp_path),
    )
    assert binding.provider_session_id == "sess_1"
    assert binding.provider_run_id == "run_1"
    assert binding.provider_session_id != binding.provider_run_id
    assert binding.status == BindingStatus.CREATED


def test_B4_B5_binding_links_admission_and_goal(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    result = adm.request(_req(tmp_path, goal="gX"))
    binding = adm.create_binding(
        executor_id="codex",
        installation_id="install-1",
        provider_id="codex",
        goal_run_id="gX",
        admission_id=result.admission_id,
        project_root=str(tmp_path),
    )
    assert binding.admission_id == result.admission_id
    assert binding.goal_run_id == "gX"


def test_B6_lost_provider_detected_on_restart(tmp_path):
    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="d1",
        session_id="s1",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "do it"}],
    )
    state = load_goal_run(str(tmp_path), create.goal_run_id)
    state.status = GoalStatus.running
    from server.goal_run.store import save_goal_run

    save_goal_run(state, str(tmp_path))
    bind_execution(create, "ex_1", str(tmp_path), admit=True)

    # simulate crash: fresh in-memory registry, disk bindings remain
    reset_admissions()
    adm = get_admission(str(tmp_path))
    recovered = adm.reconcile_restart(str(tmp_path))
    assert recovered
    assert recovered[0]["status"] == str(BindingStatus.LOST)
    state = load_goal_run(str(tmp_path), create.goal_run_id)
    assert state.status == GoalStatus.recovering
    assert state.status != GoalStatus.completed


# ---------------- Recovery ----------------


def test_R6_unrecoverable_run_not_completed(tmp_path):
    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="d2",
        session_id="s1",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    bind_execution(create, "ex_2", str(tmp_path), admit=True)
    reset_admissions()
    get_admission(str(tmp_path)).reconcile_restart(str(tmp_path))
    state = load_goal_run(str(tmp_path), create.goal_run_id)
    assert state.status != GoalStatus.completed


# ---------------- Isolation ----------------


def test_I2_admission_scoped_to_request_workspace(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    result = adm.request(_req(tmp_path, workspace=tmp_path / "w1" if False else tmp_path))
    ws_keys = [
        lease.resource for lease in result.resource_leases if lease.resource.kind == "workspace"
    ]
    assert len(ws_keys) == 1


def test_I3_nested_repo_identity_distinct(tmp_path):
    _git_init(tmp_path)
    nested = tmp_path / "sub"
    nested.mkdir()
    from server.goal_run.execution_admission import resolve_workspace_identity

    root_id = resolve_workspace_identity(str(tmp_path))
    nested_id = resolve_workspace_identity(str(nested))
    # Same git common dir, but canonical roots differ -> not equal identity.
    assert not root_id.matches(nested_id)


def test_I4_stale_admission_cannot_execute(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    adm.request(_req(tmp_path, goal="g1"))
    other = tmp_path / "elsewhere"
    other.mkdir()
    result = adm.request(
        _req(tmp_path, goal="g2", workspace=other),
        registered_workspace_root=str(tmp_path),
    )
    assert result.status == AdmissionStatus.DENIED


# ---------------- Receipt ----------------


def test_E1_E2_E3_admission_binding_receipt_chain(tmp_path):
    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="d3",
        session_id="s1",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    bind_execution(create, "ex_3", str(tmp_path), admit=True)
    receipt_path = (
        tmp_path / ".veya-project" / "goal-runs" / create.goal_run_id / "execution_receipts.jsonl"
    )
    assert receipt_path.exists()
    rows = [json.loads(line) for line in receipt_path.read_text().splitlines()]
    assert rows
    row = rows[0]
    assert row["admission_id"]
    assert row["binding_id"]
    assert row["provider_run_id"] == "ex_3"
    assert row["resource_leases"]
    assert row["final_state"] == "running"


def test_E5_failed_run_has_no_success_receipt(tmp_path):
    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="d4",
        session_id="s1",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    bind_execution(create, "ex_4", str(tmp_path), admit=True)
    # A second mutation on the same workspace is denied -> no binding receipt.
    adm = get_admission(str(tmp_path))
    result = adm.request(_req(tmp_path, goal="g_other"))
    assert result.status == AdmissionStatus.DENIED
    receipt_path = tmp_path / ".veya-project" / "goal-runs" / "g_other" / "execution_receipts.jsonl"
    assert not receipt_path.exists()


def test_bind_denied_marks_run_failed(tmp_path):
    first = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="d5",
        session_id="s1",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    bind_execution(first, "ex_5", str(tmp_path), admit=True)
    second = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="d6",
        session_id="s1",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "y"}],
    )
    with pytest.raises(RuntimeError, match="admission"):
        bind_execution(second, "ex_6", str(tmp_path), admit=True)
    state = load_goal_run(str(tmp_path), second.goal_run_id)
    assert state.status == GoalStatus.failed


# ---------------- P2: executor validity / provider identity / approval ----------------


def test_V1_healthy_executor_admitted(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    result = adm.request(_req(tmp_path, executor="codex"))
    assert result.status == AdmissionStatus.ADMITTED


def test_V2_unknown_executor_denied(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    result = adm.request(_req(tmp_path, executor="no_such_executor_xyz"))
    assert result.status == AdmissionStatus.DENIED
    assert result.reason == "executor_unknown"


def test_V3_unavailable_executor_denied(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    # grok is UNAVAILABLE in this environment (no launcher).
    result = adm.request(_req(tmp_path, executor="grok"))
    assert result.status == AdmissionStatus.DENIED
    assert result.reason.startswith("executor_unhealthy")


def test_V4_capability_mismatch_denied(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    req = _req(tmp_path, executor="codex")
    req.requested_capabilities = ["definitely_not_a_capability"]
    result = adm.request(req)
    assert result.status == AdmissionStatus.DENIED
    assert result.reason.startswith("executor_capability_mismatch")


def test_V5_capability_match_admitted(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    from veya.remote.executor_registry import get_executor_registry

    identity = get_executor_registry().identity("codex")
    cap = next(iter(identity.capability.capabilities))
    req = _req(tmp_path, executor="codex")
    req.requested_capabilities = [cap]
    result = adm.request(req)
    assert result.status == AdmissionStatus.ADMITTED


def test_P2_provider_snapshot_on_binding(tmp_path):
    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="d7",
        session_id="s1",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    bind_execution(create, "ex_7", str(tmp_path), admit=True)
    record = json.loads(
        next((tmp_path / ".veya" / "execution_admission").glob("binding_*.json")).read_text()
    )
    assert record["provider_status"]
    assert record["provider_health"]


def test_AP1_approval_evidence_on_binding(tmp_path):
    state = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="d8",
        session_id="s1",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    saved = load_goal_run(str(tmp_path), state.goal_run_id)
    saved.runtime_checkpoint = {
        "approval_id": "appr_123",
        "approval_decision": "approved",
        "permission_decision": "ALLOW",
    }
    from server.goal_run.store import save_goal_run

    save_goal_run(saved, str(tmp_path))
    bind_execution(state, "ex_8", str(tmp_path), admit=True)
    record = json.loads(
        next((tmp_path / ".veya" / "execution_admission").glob("binding_*.json")).read_text()
    )
    assert record["approval_id"] == "appr_123"
    assert record["approval_evidence"]["decision"] == "approved"
    receipt = (
        tmp_path / ".veya-project" / "goal-runs" / state.goal_run_id / "execution_receipts.jsonl"
    )
    row = json.loads(receipt.read_text().splitlines()[0])
    assert row["permission_decision"] == "ALLOW"


# ---------------- LOCAL2_AUTH_REFORM: scope direct-execution ----------------


def test_scope_registered_on_bind_and_covers_workspace(tmp_path):
    from server.goal_run.execution_admission import scope_for

    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="d9",
        session_id="s1",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    bind_execution(create, "ex_9", str(tmp_path), admit=True)
    scope = scope_for(str(tmp_path))
    assert scope is not None
    assert scope.allows("file.write")
    assert scope.allows("shell.exec")
    assert scope.allows("git.write")
    assert scope.goal_run_id == create.goal_run_id


def test_scope_removed_on_release(tmp_path):
    from server.goal_run.execution_admission import get_admission, scope_for

    adm = get_admission(str(tmp_path))
    result = adm.request(_req(tmp_path, goal="g_rel"))
    from server.goal_run.execution_admission import (
        DEFAULT_NORMAL_CAPABILITIES,
        ExecutionScope,
        register_scope,
    )

    register_scope(
        ExecutionScope(
            goal_run_id="g_rel",
            project_id="p",
            workspace_id="w",
            workspace_root=str(tmp_path),
            repository_root=str(tmp_path),
            allowed_capabilities=list(DEFAULT_NORMAL_CAPABILITIES),
            mutation_intent="mutation",
            network_policy="project",
            executor_binding=None,
            admission_id=result.admission_id,
            expires_at=result.expires_at,
        ),
        project_root=str(tmp_path),
    )
    assert scope_for(str(tmp_path)) is not None
    adm.release(result.admission_id, project_root=str(tmp_path))
    assert scope_for(str(tmp_path)) is None


def test_scope_survives_restart_via_restore(tmp_path):
    from server.goal_run.execution_admission import (
        reset_scopes,
        restore_scopes,
        scope_for,
    )

    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="d10",
        session_id="s1",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "x"}],
    )
    bind_execution(create, "ex_10", str(tmp_path), admit=True)
    reset_scopes()
    assert scope_for(str(tmp_path)) is None
    assert restore_scopes(str(tmp_path)) == 1
    assert scope_for(str(tmp_path)) is not None


def test_capability_for_tool_mapping():
    from server.goal_run.execution_admission import capability_for_tool

    assert capability_for_tool("file.write") == "file.write"
    assert capability_for_tool("file.patch") == "file.patch"
    assert capability_for_tool("shell.exec", {"command": "pytest"}) == "shell.exec"
    assert (
        capability_for_tool("shell.exec", {"command": "systemctl --user status x"})
        == "systemd.user"
    )
    assert capability_for_tool("test.run") == "test.run"
    assert capability_for_tool("git.commit") == "git.write"
    assert capability_for_tool("git.status") == "git.read"
    assert capability_for_tool("worktree.create") == "worktree"
    assert capability_for_tool("process.cancel") == "shell.exec"


def test_scope_denies_foreign_target():
    from server.goal_run.execution_admission import (
        DEFAULT_NORMAL_CAPABILITIES,
        ExecutionScope,
    )

    scope = ExecutionScope(
        goal_run_id="g",
        project_id="p",
        workspace_id="w",
        workspace_root="/tmp/veya_scope_ws",
        repository_root="/tmp/veya_scope_ws",
        allowed_capabilities=list(DEFAULT_NORMAL_CAPABILITIES),
        mutation_intent="mutation",
        network_policy="project",
        executor_binding=None,
        admission_id="adm_x",
        expires_at=None,
    )
    assert scope.covers("/tmp/veya_scope_ws/a/b.py")
    assert not scope.covers("/etc/passwd")
