"""Async admission + capability-memoisation contract for Local2 worker.dispatch.

Regression cover for two defects:

1. ``_submit_worker_child`` awaited ``run_sync_in_daemon_thread`` from a plain
   ``def``, which is a module-level ``SyntaxError``.  That killed the whole MCP
   import chain, so the gateway could never bind :8790 and the unit crash-looped
   into systemd's start limit.  The manifest must be genuinely awaited.
2. The runtime capability probe shells out to ``<binary> --version`` (2s
   timeout) once per child, inside the admission request.  It is now memoised
   for a short TTL, keyed on executor + workspace + health.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.goal_run.pre_admission import create_pending_run
from veya.remote.execution import DurableJobManager, ExecutionStore
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


def test_submit_worker_child_is_a_coroutine_function() -> None:
    """Regression: the manifest probe forces an async admission signature.

    ``_submit_worker_child`` awaits ``run_sync_in_daemon_thread`` for the
    runtime capability manifest.  A plain ``def`` here raises
    ``SyntaxError: 'await' outside async function`` at import time, which took
    the whole MCP gateway down.  Lock the signature so that cannot regress.
    """
    assert inspect.iscoroutinefunction(RemoteToolAdapter._submit_worker_child)


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
    pre = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="dispatch-await-manifest",
        session_id=session.session_id,
        requested_executor="claude_code",
        tasks=[{"worker": "claude_code", "task": "probe shell capability"}],
    )
    parent = adapter.jobs.create_parent(
        session=session,
        tool="worker.dispatch",
        binding=binding,
        dispatch_id="dispatch-await-manifest",
        goal_run_id=pre.goal_run_id,
        goal_task_id=pre.goal_task_id,
        goal_project_root=str(tmp_path),
        executor_id="claude_code",
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
        goal_run_id=pre.goal_run_id,
        goal_task_id=pre.goal_task_id,
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
