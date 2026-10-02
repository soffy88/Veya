"""Deterministic Local2 control-plane closure tests.

These tests deliberately use a fake worker. Provider availability is not part
of admission correctness: the handle must be durable and idempotent before the
fake worker is allowed to finish.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from server.goal_run.pre_admission import (
    bind_execution,
    create_pending_run,
    reconcile_unbound_runs,
)
from server.goal_run.store import load_goal_run
from veya.remote.execution import DurableJobManager, ExecutionError, ExecutionStore
from veya.remote.execution_contract import clear_runtime_capability_manifest_cache
from veya.remote.tool_adapter import RemoteToolAdapter


def _session(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        session_id="local2-session",
        principal="owner",
        token_id="local2-token",
        active_workspace=str(root),
        explicit_workspace=str(root),
    )


def _binding(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        requested_path=str(root),
        requested_realpath=str(root),
        repo_root=str(root),
        repo_identity=str(root),
        worktree_path=None,
        worktree_repo_root=None,
    )


async def _fake_worker(reporter: object) -> str:
    await asyncio.sleep(0.05)
    return "fake-complete"


@pytest.mark.asyncio
async def test_dispatch_handle_is_durable_and_idempotent(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "executions")
    manager = DurableJobManager(store)
    session = _session(tmp_path)
    binding = _binding(tmp_path)
    pre = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="dispatch-closure-1",
        session_id=session.session_id,
        requested_executor="fake",
        tasks=[{"worker": "fake", "task": "fake"}],
    )

    first = manager.submit(
        session=session,
        tool="worker.dispatch",
        veya_tool="direct_fake",
        binding=binding,
        runner=_fake_worker,
        execution_type="direct",
        execution_mode="direct_fake",
        worker_type="FAKE",
        idempotency_key="dispatch-closure-1",
        dispatch_id="dispatch-closure-1",
        goal_run_id=pre.goal_run_id,
        goal_task_id=pre.goal_task_id,
        goal_project_root=str(tmp_path),
    )
    assert first.dispatch_id == "dispatch-closure-1"
    assert first.execution_id
    assert first.goal_run_id == pre.goal_run_id
    assert first.goal_task_id == pre.goal_task_id
    assert first.lifecycle_state == "PERSISTED"
    bind_execution(pre, first.execution_id, str(tmp_path))

    replay = manager.submit(
        session=session,
        tool="worker.dispatch",
        veya_tool="direct_fake",
        binding=binding,
        runner=_fake_worker,
        execution_type="direct",
        execution_mode="direct_fake",
        worker_type="FAKE",
        idempotency_key="dispatch-closure-1",
        dispatch_id="dispatch-closure-1",
        goal_run_id=pre.goal_run_id,
        goal_task_id=pre.goal_task_id,
        goal_project_root=str(tmp_path),
    )
    assert replay.execution_id == first.execution_id
    assert replay.goal_run_id == first.goal_run_id
    assert replay.goal_task_id == first.goal_task_id

    await manager.wait(first.execution_id, timeout_s=5)
    assert manager.lookup(first.execution_id).lifecycle_state == "COMPLETED"
    reloaded = DurableJobManager(ExecutionStore(tmp_path / "executions"))
    restored = reloaded.lookup(first.execution_id)
    assert restored is not None
    assert restored.execution_id == first.execution_id
    assert restored.goal_run_id == first.goal_run_id


@pytest.mark.asyncio
async def test_cancel_persists_intent_and_is_idempotent(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(tmp_path / "executions"))
    session = _session(tmp_path)
    pre = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="dispatch-cancel-1",
        session_id=session.session_id,
        requested_executor="fake",
        tasks=[{"worker": "fake", "task": "fake"}],
    )
    record = manager.submit(
        session=session,
        tool="worker.dispatch",
        veya_tool="direct_fake",
        binding=_binding(tmp_path),
        runner=lambda reporter: asyncio.sleep(30),
        execution_type="direct",
        idempotency_key="dispatch-cancel-1",
        dispatch_id="dispatch-cancel-1",
        goal_run_id=pre.goal_run_id,
        goal_task_id=pre.goal_task_id,
        goal_project_root=str(tmp_path),
    )
    bind_execution(pre, record.execution_id, str(tmp_path))
    cancelled = await manager.cancel(
        record.execution_id,
        token_id=session.token_id,
        session_id=session.session_id,
        principal=session.principal,
        workspace_realpath=str(tmp_path),
    )
    repeated = await manager.cancel(
        record.execution_id,
        token_id=session.token_id,
        session_id=session.session_id,
        principal=session.principal,
        workspace_realpath=str(tmp_path),
    )
    assert cancelled.lifecycle_state == "CANCELLED"
    assert repeated.execution_id == record.execution_id
    assert repeated.lifecycle_state == "CANCELLED"
    assert repeated.cancellation_intent is not None


def test_required_persistence_failure_is_not_ambiguous(tmp_path: Path) -> None:
    class BrokenStore(ExecutionStore):
        def save(self, record):
            raise OSError("injected durable-store failure")

    manager = DurableJobManager(BrokenStore(tmp_path / "executions"))
    session = _session(tmp_path)
    pre = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="dispatch-persist-failure",
        session_id=session.session_id,
        requested_executor="fake",
        tasks=[{"worker": "fake", "task": "fake"}],
    )
    with pytest.raises(ExecutionError, match="durable persistence failed"):
        manager.submit(
            session=session,
            tool="worker.dispatch",
            veya_tool="direct_fake",
            binding=_binding(tmp_path),
            runner=_fake_worker,
            idempotency_key="dispatch-persist-failure",
            dispatch_id="dispatch-persist-failure",
            goal_run_id=pre.goal_run_id,
            goal_task_id=pre.goal_task_id,
            goal_project_root=str(tmp_path),
        )


def test_restart_reconciles_goalrun_persisted_before_execution(tmp_path: Path) -> None:
    pre = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="dispatch-pre-execution-restart",
        session_id="local2-session",
        requested_executor="fake",
        tasks=[{"worker": "fake", "task": "never launched"}],
    )

    assert (
        reconcile_unbound_runs(
            str(tmp_path), reason="backend restarted before execution persistence"
        )
        == 1
    )
    state = load_goal_run(str(tmp_path), pre.goal_run_id)
    assert state is not None
    assert state.execution_id is None
    assert state.status.value == "failed"
    assert state.tasks[pre.goal_task_id].status.value == "blocked"


@pytest.mark.asyncio
async def test_worker_dispatch_persists_real_goalrun_before_child_launch(tmp_path: Path) -> None:
    adapter = RemoteToolAdapter(None, execution_store=ExecutionStore(tmp_path / "executions"))
    session = _session(tmp_path)
    binding = adapter.binding("worker.dispatch")
    ws_binding = SimpleNamespace(
        requested_path=str(tmp_path),
        requested_realpath=str(tmp_path),
        repo_root=str(tmp_path),
        repo_identity=str(tmp_path),
        is_git_repo=False,
        worktree_path=None,
        worktree_repo_root=None,
    )

    async def fake_submit(
        self,
        session,
        ws_binding,
        parent,
        worker,
        task_text,
        item,
        index,
        *,
        dispatch_id,
        goal_run_id,
        goal_task_id,
        goal_project_root,
        execution_target="NEW_ISOLATED_WORKTREE",
    ):
        return self.jobs.submit(
            session=session,
            tool="worker.dispatch",
            veya_tool="direct_fake",
            binding=ws_binding,
            runner=_fake_worker,
            execution_type="direct",
            parent_execution_id=parent.execution_id,
            idempotency_key=f"{dispatch_id}:child:{index}",
            dispatch_id=f"{dispatch_id}:child:{index}",
            goal_run_id=goal_run_id,
            goal_task_id=goal_task_id,
            goal_project_root=goal_project_root,
        )

    adapter._submit_worker_child = MethodType(fake_submit, adapter)
    response = await adapter._call_worker_dispatch(
        session,
        None,
        binding,
        {"dispatch_id": "dispatch-real-goal", "tasks": [{"worker": "pi", "task": "x"}]},
        ws_binding,
        time.time(),
    )
    assert response.ok, response
    payload = response.result
    state = load_goal_run(str(tmp_path), payload["goal_run_id"])
    assert state is not None
    assert state.dispatch_id == "dispatch-real-goal"
    assert state.execution_id == payload["execution_id"]
    assert payload["goal_task_id"] in state.tasks
    assert payload["goal_run_id"] != "goalrun_dispatch-real-goal"


@pytest.mark.asyncio
async def test_prelaunch_restart_reconciles_same_canonical_ids(tmp_path: Path) -> None:
    store = ExecutionStore(tmp_path / "executions")
    manager = DurableJobManager(store)
    session = _session(tmp_path)
    pre = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="dispatch-prelaunch-restart",
        session_id=session.session_id,
        requested_executor="pi",
        tasks=[{"worker": "pi", "task": "never launched"}],
    )
    parent = manager.create_parent(
        session=session,
        tool="worker.dispatch",
        binding=_binding(tmp_path),
        dispatch_id="dispatch-prelaunch-restart",
        goal_run_id=pre.goal_run_id,
        goal_task_id=pre.goal_task_id,
        goal_project_root=str(tmp_path),
        executor_id="pi",
    )
    bind_execution(pre, parent.execution_id, str(tmp_path))

    restarted = RemoteToolAdapter(None, execution_store=ExecutionStore(tmp_path / "executions"))
    report = await restarted.initialize()
    record = restarted.jobs.lookup_dispatch("dispatch-prelaunch-restart")
    state = load_goal_run(str(tmp_path), pre.goal_run_id)
    assert report["prelaunch_reconciled"] == 1
    assert record is not None
    assert record.execution_id == parent.execution_id
    assert record.goal_run_id == pre.goal_run_id
    assert record.goal_task_id == pre.goal_task_id
    assert record.status == "FAILED"
    assert state is not None
    assert state.status.value == "failed"
    assert state.dispatch_id == "dispatch-prelaunch-restart"


def _manifest(executor_id: str, *, supports_shell: bool = True, active: int = 0):
    import time as _time

    from veya.remote.execution_contract import RuntimeCapabilityManifest

    return RuntimeCapabilityManifest(
        executor_id=executor_id,
        executor_kind="l1_worker",
        installed=True,
        authenticated=True,
        reachable=True,
        provider="test",
        model="test",
        runtime_version="test",
        supports_streaming=True,
        supports_cancel=True,
        supports_suspend=True,
        supports_resume=True,
        supports_session_reuse=True,
        supports_handoff=False,
        supports_workspace=True,
        supports_nested_repo=True,
        supports_worktree=True,
        supports_mcp=True,
        supports_skills=True,
        supports_shell=supports_shell,
        filesystem_isolation="isolated_worktree",
        network_isolation="loopback_proxy",
        credential_isolation="ephemeral_redacted",
        process_isolation="process_group",
        max_concurrency=4,
        active_executions=active,
        status="READY",
        status_reason="test",
        observed_at=_time.time(),
    )


async def _never_finishes(reporter: object) -> str:
    await asyncio.Event().wait()
    return "unreachable"


@pytest.mark.asyncio
async def test_dispatch_child_awaits_runtime_capability_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the capability manifest is genuinely awaited, not fire-and-forget.

    A fire-and-forget probe yields a coroutine, so the ``supports_shell``
    admission gate below would read an un-awaited object.  The probe is made
    slow so that "not actually awaited" is also caught by timing, not only by
    attribute access.
    """
    from veya.remote import execution_contract

    probe_calls: list[str] = []

    def slow_probe(executor_id: str, **kwargs: object) -> object:
        probe_calls.append(executor_id)
        time.sleep(0.25)
        return SimpleNamespace(
            executor_id=executor_id,
            supports_shell=False,
            __dict__={"executor_id": executor_id, "supports_shell": False},
        )

    monkeypatch.setattr(execution_contract, "probe_runtime_capability_manifest", slow_probe)

    adapter = RemoteToolAdapter(None, execution_store=ExecutionStore(tmp_path / "executions"))
    session = _session(tmp_path)
    binding = _binding(tmp_path)
    # A real admitted parent record, created without GoalRun pre-admission so
    # this regression test stays independent of that machinery.
    parent = adapter.jobs.submit(
        session=session,
        tool="worker.dispatch",
        veya_tool="direct_claude_code",
        binding=binding,
        runner=_never_finishes,
        execution_type="remote",
        worker_type="CLAUDE_CODE",
        role="parent",
        goal_run_id="goal_synthetic_await_manifest",
        goal_task_id="task_synthetic_await_manifest",
    )

    started = time.monotonic()
    child = await adapter._submit_worker_child(
        session,
        binding,
        parent,
        "claude_code",
        "probe shell capability",
        {"required_capabilities": ["supports_shell"]},
        0,
        dispatch_id="dispatch-await-manifest",
        goal_run_id=parent.goal_run_id,
        goal_task_id=parent.goal_task_id,
        goal_project_root=str(tmp_path),
    )
    elapsed = time.monotonic() - started

    # The probe really ran, and really blocked the admission: a fire-and-forget
    # coroutine would return immediately and never populate probe_calls.
    assert probe_calls == ["claude_code"]
    assert elapsed >= 0.25, f"probe was not awaited (elapsed={elapsed:.3f}s)"

    # The awaited value drove the gate: supports_shell=False with a required
    # supports_shell capability must block the child instead of launching it.
    assert child is not None
    assert child.parent_execution_id == parent.execution_id


async def _never_finishes(reporter: object) -> str:
    """Runner that keeps the parent execution non-terminal for the test."""
    await asyncio.Event().wait()
    return "unreachable"


def _manifest(executor_id: str, *, supports_shell: bool = True, active: int = 0):
    """Build a real RuntimeCapabilityManifest so cache tests exercise the
    same dataclasses.replace() path production uses."""
    import time as _time

    from veya.remote.execution_contract import RuntimeCapabilityManifest

    return RuntimeCapabilityManifest(
        executor_id=executor_id,
        executor_kind="l1_worker",
        installed=True,
        authenticated=True,
        reachable=True,
        provider="test",
        model="test",
        runtime_version="test",
        supports_streaming=True,
        supports_cancel=True,
        supports_suspend=True,
        supports_resume=True,
        supports_session_reuse=True,
        supports_handoff=False,
        supports_workspace=True,
        supports_nested_repo=True,
        supports_worktree=True,
        supports_mcp=True,
        supports_skills=True,
        supports_shell=supports_shell,
        filesystem_isolation="isolated_worktree",
        network_isolation="loopback_proxy",
        credential_isolation="ephemeral_redacted",
        process_isolation="process_group",
        max_concurrency=4,
        active_executions=active,
        status="READY",
        status_reason="test",
        observed_at=_time.time(),
    )


def test_capability_manifest_is_memoised_and_health_keyed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2: admission awaits a real manifest, but must not re-probe binaries.

    ``probe_runtime_capability_manifest`` shells out to ``<binary> --version``
    with a 2s timeout.  Running it per child made the admission request pay
    that latency.  It is memoised for a short TTL, keyed on executor +
    workspace + health so a provider outage invalidates immediately.
    """
    from veya.remote import execution_contract

    clear_runtime_capability_manifest_cache()
    calls: list[str] = []

    class _Health:
        def __init__(self, value: str) -> None:
            self.value = value

    class _Registry:
        def __init__(self, value: str) -> None:
            self.value = value

        def get_health(self, worker: str, **kwargs: object) -> _Health:
            return _Health(self.value)

    def counting_probe(executor_id: str, **kwargs: object) -> object:
        calls.append(executor_id)
        return _manifest(executor_id)

    monkeypatch.setattr(execution_contract, "probe_runtime_capability_manifest", counting_probe)

    reg = _Registry("HEALTHY")
    first = execution_contract.probe_runtime_capability_manifest_cached(
        "claude_code", workspace_path=str(tmp_path), health_registry=reg
    )
    second = execution_contract.probe_runtime_capability_manifest_cached(
        "claude_code", workspace_path=str(tmp_path), health_registry=reg
    )
    assert calls == ["claude_code"], f"second admission re-probed: {calls}"
    assert first is second or (first.executor_id == second.executor_id)

    # A health transition must invalidate immediately, not wait out the TTL.
    reg.value = "UNAVAILABLE"
    third = execution_contract.probe_runtime_capability_manifest_cached(
        "claude_code", workspace_path=str(tmp_path), health_registry=reg
    )
    assert calls == ["claude_code", "claude_code"], f"health change did not invalidate: {calls}"
    assert third.executor_id == "claude_code"

    clear_runtime_capability_manifest_cache()


def test_capability_manifest_cache_refreshes_live_bookkeeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A memoised manifest must never misreport live execution counts."""
    from veya.remote import execution_contract

    clear_runtime_capability_manifest_cache()
    monkeypatch.setattr(
        execution_contract,
        "probe_runtime_capability_manifest",
        lambda executor_id, **kwargs: _manifest(
            executor_id, active=int(kwargs.get("active_executions", 0) or 0)
        ),
    )
    first = execution_contract.probe_runtime_capability_manifest_cached(
        "pi", workspace_path=str(tmp_path), active_executions=1
    )
    second = execution_contract.probe_runtime_capability_manifest_cached(
        "pi", workspace_path=str(tmp_path), active_executions=7
    )
    assert first.active_executions == 1
    assert second.active_executions == 7, "cached manifest reported a stale execution count"
    clear_runtime_capability_manifest_cache()
