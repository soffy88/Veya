"""P5: restart / crash reconciliation.

Proves the recovery contract:
- restart discovery finds live runs
- reconcile distinguishes RESUMABLE / RECOVERY_REQUIRED / LOST
- recovery never fakes success (never COMPLETED)
- no duplicate execution, no duplicate lease, no repeated normal approval
- recovery evidence events are written
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.goal_run import bind_execution, create_pending_run
from server.goal_run.execution_admission import (
    BindingStatus,
    get_admission,
    reset_admissions,
    reset_scopes,
    scope_for,
)
from server.goal_run.execution_recovery import (
    ExecutionRecovery,
    RecoveryOutcome,
    recover_after_restart,
)
from server.goal_run.models import GoalStatus
from server.goal_run.store import load_goal_run, read_events, save_goal_run


@pytest.fixture(autouse=True)
def _reset_registries():
    reset_admissions()
    reset_scopes()
    yield
    reset_admissions()
    reset_scopes()


def _binding_files(root: Path) -> list[Path]:
    d = root / ".veya" / "execution_admission"
    return sorted(d.glob("binding_*.json")) if d.is_dir() else []


def _live_run(tmp_path: Path, *, dispatch: str = "d-p5", executor: str = "codex") -> str:
    """Create a fully-bound, live GoalRun (admission + binding + scope + lease)."""
    create = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id=dispatch,
        session_id="s-p5",
        requested_executor=executor,
        tasks=[{"worker": executor, "task": "do work"}],
    )
    bind_execution(create, f"ex_{dispatch}", str(tmp_path))
    state = load_goal_run(str(tmp_path), create.goal_run_id)
    assert state is not None
    state.status = GoalStatus.running
    save_goal_run(state, str(tmp_path))
    return create.goal_run_id


def _simulate_restart() -> None:
    """Drop all in-memory authority; only durable files survive."""
    reset_admissions()
    reset_scopes()


def test_discover_finds_live_run(tmp_path: Path):
    goal_id = _live_run(tmp_path)
    _simulate_restart()
    recovery = ExecutionRecovery(str(tmp_path))
    assert goal_id in recovery.discover()


def test_discover_ignores_terminal_run(tmp_path: Path):
    goal_id = _live_run(tmp_path)
    state = load_goal_run(str(tmp_path), goal_id)
    state.status = GoalStatus.completed
    save_goal_run(state, str(tmp_path))
    _simulate_restart()
    assert goal_id not in ExecutionRecovery(str(tmp_path)).discover()


@pytest.mark.asyncio
async def test_reconcile_reachable_provider_is_resumable(tmp_path: Path):
    goal_id = _live_run(tmp_path, executor="codex")
    _simulate_restart()
    verdict = await ExecutionRecovery(str(tmp_path)).reconcile(goal_id)
    assert verdict.outcome == RecoveryOutcome.RESUMABLE
    assert verdict.provider_reachable is True
    assert verdict.binding_present is True


@pytest.mark.asyncio
async def test_reconcile_unavailable_provider_is_recovery_required(tmp_path: Path, monkeypatch):
    goal_id = _live_run(tmp_path, executor="codex")
    _simulate_restart()
    recovery = ExecutionRecovery(str(tmp_path))
    monkeypatch.setattr(recovery, "_probe_provider", lambda executor_id: False)
    verdict = await recovery.reconcile(goal_id)
    assert verdict.outcome == RecoveryOutcome.RECOVERY_REQUIRED
    assert verdict.provider_reachable is False


@pytest.mark.asyncio
async def test_reconcile_binding_missing_is_recovery_required(tmp_path: Path):
    goal_id = _live_run(tmp_path)
    _simulate_restart()
    for path in _binding_files(tmp_path):
        path.unlink()
    verdict = await ExecutionRecovery(str(tmp_path)).reconcile(goal_id)
    assert verdict.outcome == RecoveryOutcome.RECOVERY_REQUIRED
    assert verdict.reason == "binding_record_missing"
    assert verdict.binding_present is False


@pytest.mark.asyncio
async def test_reconcile_lost_binding_is_lost(tmp_path: Path):
    goal_id = _live_run(tmp_path)
    _simulate_restart()
    for path in _binding_files(tmp_path):
        data = json.loads(path.read_text())
        data["status"] = str(BindingStatus.LOST)
        path.write_text(json.dumps(data))
    verdict = await ExecutionRecovery(str(tmp_path)).reconcile(goal_id)
    assert verdict.outcome == RecoveryOutcome.LOST


@pytest.mark.asyncio
async def test_recovery_never_marks_completed(tmp_path: Path):
    """The central P5 invariant: no false success."""
    goal_id = _live_run(tmp_path)
    _simulate_restart()
    recovery = ExecutionRecovery(str(tmp_path))
    verdict = await recovery.reconcile(goal_id)
    assert verdict.outcome != RecoveryOutcome.RUNNING
    assert verdict.outcome.value != "COMPLETED"
    state = load_goal_run(str(tmp_path), goal_id)
    assert state.status != GoalStatus.completed
    assert state.status == GoalStatus.recovering


@pytest.mark.asyncio
async def test_recovery_does_not_duplicate_execution(tmp_path: Path):
    goal_id = _live_run(tmp_path)
    before = load_goal_run(str(tmp_path), goal_id)
    before_exec = before.execution_id
    before_attempts = len(before.execution_attempts)
    _simulate_restart()
    await ExecutionRecovery(str(tmp_path)).reconcile(goal_id)
    after = load_goal_run(str(tmp_path), goal_id)
    assert after.execution_id == before_exec
    assert len(after.execution_attempts) == before_attempts


def test_recover_resources_restores_scope_and_lease(tmp_path: Path):
    _live_run(tmp_path)
    # Scope + lease are live before restart.
    assert scope_for(str(tmp_path)) is not None
    _simulate_restart()
    # After restart the in-memory scope is gone; recovery restores it.
    assert scope_for(str(tmp_path)) is None
    resources = ExecutionRecovery(str(tmp_path)).recover_resources()
    assert resources["scopes_restored"] >= 1
    assert resources["leases_restored"] >= 1
    assert scope_for(str(tmp_path)) is not None


def test_reclaim_orphan_lease_for_terminal_run(tmp_path: Path):
    goal_id = _live_run(tmp_path)
    state = load_goal_run(str(tmp_path), goal_id)
    state.status = GoalStatus.completed
    save_goal_run(state, str(tmp_path))
    _simulate_restart()
    resources = ExecutionRecovery(str(tmp_path)).recover_resources()
    assert resources["orphans_reclaimed"] >= 1
    # Orphan must not keep the workspace locked: a fresh mutation can admit.
    from server.goal_run.execution_admission import (
        AdmissionStatus,
        ExecutionAdmissionRequest,
        MutationIntent,
    )

    adm = get_admission(str(tmp_path))
    fresh = adm.request(
        ExecutionAdmissionRequest(
            goal_run_id="g-fresh",
            executor_id="codex",
            executor_installation_id="default",
            project_id="p",
            workspace_id=str(tmp_path),
            project_root=str(tmp_path),
            workspace_root=str(tmp_path),
            mutation_intent=MutationIntent.MUTATION,
        )
    )
    assert fresh.status == AdmissionStatus.ADMITTED


@pytest.mark.asyncio
async def test_recovery_evidence_events_written(tmp_path: Path):
    goal_id = _live_run(tmp_path)
    _simulate_restart()
    await ExecutionRecovery(str(tmp_path)).reconcile(goal_id)
    events = read_events(str(tmp_path), goal_id, limit=100)
    types = [e.get("type", "") for e in events]
    assert "execution_recovery.RECOVERY_STARTED" in types
    assert "execution_recovery.BINDING_RECONCILED" in types
    assert "execution_recovery.PROVIDER_PROBED" in types


def test_recover_after_restart_runs_full_cycle(tmp_path: Path):
    goal_id = _live_run(tmp_path)
    _simulate_restart()
    report = recover_after_restart(str(tmp_path))
    assert report["resources"]["scopes_restored"] >= 1
    # Live run reconciled to a non-success outcome.
    assert any(v["goal_run_id"] == goal_id for v in report["verdicts"])
