"""P6: resource scheduler / queue.

Proves:
- same workspace x 2 mutation -> one ADMITTED, one WAITING_FOR_RESOURCE (QUEUED)
- different workspace x 2 -> both ADMITTED (parallel)
- FIFO queue drain after release
- ordered (deadlock-free) resource acquisition
- ResourceMode SHARED vs EXCLUSIVE
- fixed-exclusive kinds (gpu) conflict even for read-only
"""

from __future__ import annotations

import pytest

from server.goal_run.execution_admission import (
    AdmissionStatus,
    ExecutionAdmission,
    ExecutionAdmissionRequest,
    MutationIntent,
    ResourceMode,
    reset_admissions,
    reset_scopes,
    resource_mode_for,
)
from tests._executor_health import _healthy_admission_executor  # noqa: F401 (autouse)


@pytest.fixture(autouse=True)
def _reset_registries():
    reset_admissions()
    reset_scopes()
    yield
    reset_admissions()
    reset_scopes()


def _req(
    tmp_path,
    *,
    goal: str,
    intent: MutationIntent = MutationIntent.MUTATION,
    queue: bool = False,
    workspace=None,
    resources: list[str] | None = None,
    installation: str = "default",
) -> ExecutionAdmissionRequest:
    ws = workspace or tmp_path
    return ExecutionAdmissionRequest(
        goal_run_id=goal,
        executor_id="codex",
        executor_installation_id=installation,
        project_id="proj",
        workspace_id=str(ws),
        project_root=str(tmp_path),
        workspace_root=str(ws),
        mutation_intent=intent,
        queue_if_busy=queue,
        required_resources=list(resources or []),
    )


def test_same_workspace_two_mutations_one_queued(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    a = adm.request(_req(tmp_path, goal="g1", queue=False))
    b = adm.request(_req(tmp_path, goal="g2", queue=True))
    assert a.status == AdmissionStatus.ADMITTED
    assert b.status == AdmissionStatus.QUEUED
    assert b.status == AdmissionStatus.WAITING_FOR_RESOURCE


def test_same_workspace_second_denied_without_queue(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    adm.request(_req(tmp_path, goal="g1", queue=False))
    b = adm.request(_req(tmp_path, goal="g2", queue=False))
    assert b.status == AdmissionStatus.DENIED


def test_different_workspace_two_mutations_parallel(tmp_path):
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    adm = ExecutionAdmission(str(tmp_path))
    a = adm.request(_req(tmp_path, goal="g1", workspace=ws_a, installation="install-1"))
    b = adm.request(_req(tmp_path, goal="g2", workspace=ws_b, installation="install-2"))
    assert a.status == AdmissionStatus.ADMITTED
    assert b.status == AdmissionStatus.ADMITTED


def test_queue_drain_fifo_after_release(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    a = adm.request(_req(tmp_path, goal="g1"))
    b = adm.request(_req(tmp_path, goal="g2", queue=True))
    assert b.status == AdmissionStatus.QUEUED

    adm.release(a.admission_id, project_root=str(tmp_path))
    admitted = adm.admit_queued(str(tmp_path))
    assert len(admitted) == 1
    assert admitted[0].goal_run_id == "g2"
    assert admitted[0].status == AdmissionStatus.ADMITTED


def test_queue_fifo_order_preserved(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    a = adm.request(_req(tmp_path, goal="holder"))
    adm.request(_req(tmp_path, goal="first", queue=True))
    adm.request(_req(tmp_path, goal="second", queue=True))

    queued = adm.queued_admissions(str(tmp_path))
    assert [r["goal_run_id"] for r in queued] == ["first", "second"]

    adm.release(a.admission_id, project_root=str(tmp_path))
    admitted = adm.admit_queued(str(tmp_path))
    # Only one can hold the workspace; the FIFO head wins.
    assert [r.goal_run_id for r in admitted] == ["first"]
    still_queued = adm.queued_admissions(str(tmp_path))
    assert [r["goal_run_id"] for r in still_queued] == ["second"]


def test_gpu_exclusive_even_for_read(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    a = adm.request(
        _req(tmp_path, goal="g1", intent=MutationIntent.READ_ONLY, resources=["gpu:0"], queue=True)
    )
    b = adm.request(
        _req(tmp_path, goal="g2", intent=MutationIntent.READ_ONLY, resources=["gpu:0"], queue=True)
    )
    assert a.status == AdmissionStatus.ADMITTED
    assert b.status == AdmissionStatus.QUEUED


def test_resource_mode_shared_vs_exclusive():
    assert resource_mode_for("workspace", MutationIntent.READ_ONLY) == ResourceMode.SHARED
    assert resource_mode_for("workspace", MutationIntent.MUTATION) == ResourceMode.EXCLUSIVE
    assert resource_mode_for("gpu", MutationIntent.READ_ONLY) == ResourceMode.EXCLUSIVE
    assert resource_mode_for("browser_session", MutationIntent.READ_ONLY) == ResourceMode.EXCLUSIVE


def test_ordered_resource_acquisition_is_canonical(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    req = _req(
        tmp_path,
        goal="g1",
        resources=["browser_session:default", "gpu:0"],
    )
    keys = adm._resource_keys(req)
    kinds = [k.kind for k in keys]
    # Canonical order: project < workspace < executor-installation < gpu < browser-session
    assert kinds == [
        "project",
        "workspace",
        "executor-installation",
        "gpu",
        "browser-session",
    ]


def test_queued_admission_has_no_active_lease(tmp_path):
    adm = ExecutionAdmission(str(tmp_path))
    adm.request(_req(tmp_path, goal="g1"))
    b = adm.request(_req(tmp_path, goal="g2", queue=True))
    # A queued admission holds nothing.
    assert all(not lease.active() for lease in adm._leases.values() if lease.goal_run_id == "g2")
    assert b.resource_leases == []


def test_execution_control_queue_view_and_drain(tmp_path):
    from server.goal_run.execution_admission import get_admission
    from server.goal_run.execution_control import ExecutionControl

    # Production goes through the singleton admission authority.
    adm = get_admission(str(tmp_path))
    a = adm.request(_req(tmp_path, goal="g1"))
    adm.request(_req(tmp_path, goal="g2", queue=True))

    control = ExecutionControl(str(tmp_path))
    assert [r["goal_run_id"] for r in control.queued()] == ["g2"]

    adm.release(a.admission_id, project_root=str(tmp_path))
    drained = control.drain_queue()
    assert [r["goal_run_id"] for r in drained] == ["g2"]
    assert control.queued() == []
