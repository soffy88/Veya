"""Worker continuity substrate: identity split, registry, outbox, finish boundary."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from veya.remote.outbox import CommandState, DurableOutbox, SideEffectState
from veya.remote.worker_runtime import (
    FinishBoundary,
    WorkerRuntimeError,
    WorkerRuntimeRegistry,
    WorkerRuntimeState,
)


def test_pi_grok_timeout_budget_separates_inactivity_from_hard_runtime() -> None:
    from veya.remote.tool_adapter import _cli_worker_timeout_budgets

    inactivity, hard_max = _cli_worker_timeout_budgets("pi", 600.0)
    assert inactivity < hard_max
    assert inactivity == 900.0
    assert hard_max == 1800.0

    dsh_inactivity, dsh_hard_max = _cli_worker_timeout_budgets("dsh", 600.0)
    assert (dsh_inactivity, dsh_hard_max) == (120.0, 600.0)


def test_dependency_artifact_staging_records_provenance_and_hash(tmp_path: Path) -> None:
    from veya.remote.tool_adapter import _stage_dependency_artifacts

    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    source = repo / ".veya" / "artifacts" / "ex-a" / "case_a" / "input.txt"
    source.parent.mkdir(parents=True)
    worktree.mkdir()
    source.write_text("INPUT\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    staged = _stage_dependency_artifacts(
        str(worktree),
        str(repo),
        [
            {
                "execution_id": "ex-a",
                "dependency_subtask_id": "a-hicode",
                "source_worker": "HICODE",
                "source_path": "/isolated/a-hicode/case_a/input.txt",
                "relative_path": "case_a/input.txt",
                "materialized_path": str(source),
                "hash": digest,
                "size": len(source.read_bytes()),
            }
        ],
    )
    assert staged[0]["source_execution_id"] == "ex-a"
    assert staged[0]["source_subtask_id"] == "a-hicode"
    assert staged[0]["sha256"] == digest
    staged_path = Path(staged[0]["staged_path"])
    assert staged_path == (
        worktree / ".veya" / "dependencies" / "a-hicode" / "case_a" / "input.txt"
    )
    assert staged_path.read_text(encoding="utf-8") == "INPUT\n"


def test_dependency_artifact_staging_fails_closed_for_hash_and_escape(tmp_path: Path) -> None:
    from veya.remote.execution import ExecutionBlocked
    from veya.remote.tool_adapter import _stage_dependency_artifacts

    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    source = repo / ".veya" / "artifacts" / "ex-a" / "input.txt"
    source.parent.mkdir(parents=True)
    worktree.mkdir()
    source.write_text("INPUT\n", encoding="utf-8")
    with pytest.raises(ExecutionBlocked):
        _stage_dependency_artifacts(
            str(worktree),
            str(repo),
            [
                {
                    "execution_id": "ex-a",
                    "dependency_subtask_id": "a",
                    "relative_path": "../escape.txt",
                    "materialized_path": str(source),
                    "hash": "bad",
                }
            ],
        )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(ExecutionBlocked):
        _stage_dependency_artifacts(
            str(worktree),
            str(repo),
            [
                {
                    "execution_id": "ex-a",
                    "dependency_subtask_id": "a",
                    "relative_path": "input.txt",
                    "materialized_path": str(outside),
                    "hash": hashlib.sha256(outside.read_bytes()).hexdigest(),
                }
            ],
        )


async def test_cli_worker_activity_can_exceed_inactivity_budget() -> None:
    from veya.remote.tool_adapter import _wait_for_cli_process

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; [print('heartbeat', flush=True) or time.sleep(.08) for _ in range(5)]",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    activity_event = asyncio.Event()
    last_activity = [time.monotonic()]

    async def pump() -> None:
        assert process.stdout is not None
        while await process.stdout.readline():
            last_activity[0] = time.monotonic()
            activity_event.set()

    tasks = {asyncio.create_task(pump()), asyncio.create_task(process.wait())}
    await _wait_for_cli_process(
        process,
        tasks,
        activity_event=activity_event,
        last_activity=last_activity,
        inactivity_timeout_s=0.1,
        hard_max_runtime_s=1.0,
    )
    assert process.returncode == 0


async def test_cli_worker_silent_process_hits_inactivity_budget() -> None:
    from veya.remote.tool_adapter import _CLIWorkerTimeout, _wait_for_cli_process

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(1)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    activity_event = asyncio.Event()
    last_activity = [time.monotonic()]

    async def pump() -> None:
        assert process.stdout is not None
        while await process.stdout.readline():
            last_activity[0] = time.monotonic()
            activity_event.set()

    tasks = {asyncio.create_task(pump()), asyncio.create_task(process.wait())}
    with pytest.raises(_CLIWorkerTimeout) as raised:
        await _wait_for_cli_process(
            process,
            tasks,
            activity_event=activity_event,
            last_activity=last_activity,
            inactivity_timeout_s=0.05,
            hard_max_runtime_s=1.0,
        )
    assert raised.value.kind == "INACTIVITY_TIMEOUT"
    process.terminate()
    await process.wait()


async def test_cli_worker_active_process_hits_hard_max_runtime() -> None:
    from veya.remote.tool_adapter import _CLIWorkerTimeout, _wait_for_cli_process

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; [print('heartbeat', flush=True) or time.sleep(.02) for _ in range(50)]",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    activity_event = asyncio.Event()
    last_activity = [time.monotonic()]

    async def pump() -> None:
        assert process.stdout is not None
        while await process.stdout.readline():
            last_activity[0] = time.monotonic()
            activity_event.set()

    tasks = {asyncio.create_task(pump()), asyncio.create_task(process.wait())}
    try:
        with pytest.raises(_CLIWorkerTimeout) as raised:
            await _wait_for_cli_process(
                process,
                tasks,
                activity_event=activity_event,
                last_activity=last_activity,
                inactivity_timeout_s=0.2,
                hard_max_runtime_s=0.12,
            )
        assert raised.value.kind == "HARD_MAX_RUNTIME"
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()


async def test_cli_worker_completion_does_not_leave_cancelled_activity_waiter() -> None:
    from veya.remote.tool_adapter import _wait_for_cli_process

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "print('done', flush=True)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    activity_event = asyncio.Event()
    last_activity = [time.monotonic()]

    async def pump() -> None:
        assert process.stdout is not None
        while await process.stdout.readline():
            last_activity[0] = time.monotonic()
            activity_event.set()

    tasks = {asyncio.create_task(pump()), asyncio.create_task(process.wait())}
    await _wait_for_cli_process(
        process,
        tasks,
        activity_event=activity_event,
        last_activity=last_activity,
        inactivity_timeout_s=0.2,
        hard_max_runtime_s=1.0,
    )
    assert process.returncode == 0


# ── identity split + persistent registry ──────────────────────────────
def test_worker_identity_stable_across_executions_and_sleep_revive(tmp_path) -> None:
    registry = WorkerRuntimeRegistry(tmp_path)
    runtime = registry.register("pi", worker_runtime_id="pi-03", provider_session_id="ps-1")
    assert runtime.worker_runtime_id == "pi-03"

    registry.bind_execution("pi-03", "ex_a")
    registry.release("pi-03")
    assert registry.get("pi-03").state == str(WorkerRuntimeState.IDLE)

    registry.sleep("pi-03")
    assert registry.get("pi-03").state == str(WorkerRuntimeState.SLEEPING)

    registry.revive("pi-03", provider_session_id="ps-2")
    revived = registry.get("pi-03")
    assert revived.worker_runtime_id == "pi-03"  # identity preserved
    assert revived.state == str(WorkerRuntimeState.ACTIVE)

    registry.bind_execution("pi-03", "ex_b")
    # distinct executions, same runtime identity
    assert registry.get("pi-03").worker_runtime_id == "pi-03"
    assert registry.get("pi-03").current_execution_id == "ex_b"


def test_context_rollover_increments_generation_not_identity(tmp_path) -> None:
    registry = WorkerRuntimeRegistry(tmp_path)
    registry.register("pi", worker_runtime_id="pi-03", provider_session_id="ps-1")
    result = registry.rollover_context("pi-03", new_provider_session_id="ps-2", reason="compact")
    assert result["worker_runtime_id"] == "pi-03"
    assert result["worker_identity_changed"] is False
    assert result["old_context_generation"] == 1
    assert result["new_context_generation"] == 2
    assert registry.get("pi-03").provider_session_id == "ps-2"


def test_sleep_not_supported_fails_closed(tmp_path) -> None:
    registry = WorkerRuntimeRegistry(tmp_path)
    registry.register("hicode", worker_runtime_id="hicode-01")
    with pytest.raises(WorkerRuntimeError):
        registry.sleep("hicode-01")


def test_registry_persists_across_reload(tmp_path) -> None:
    registry = WorkerRuntimeRegistry(tmp_path)
    registry.register("grok", worker_runtime_id="grok-01", provider_session_id="ps-9")
    reloaded = WorkerRuntimeRegistry(tmp_path)
    runtime = reloaded.get("grok-01")
    assert runtime is not None and runtime.provider_session_id == "ps-9"


def test_worker_pool_allocate_distinct_runtimes(tmp_path) -> None:
    registry = WorkerRuntimeRegistry(tmp_path)
    a = registry.allocate("pi", workspace_identity="/w")
    registry.bind_execution(a.worker_runtime_id, "ex_a")  # busy
    b = registry.allocate("pi", workspace_identity="/w")
    assert a.worker_runtime_id != b.worker_runtime_id


# ── durable outbox: idempotency + delivery truth + ambiguity ──────────
def test_outbox_idempotency_dedupes_by_key(tmp_path) -> None:
    outbox = DurableOutbox(tmp_path)
    first, created = outbox.create(
        idempotency_key="k1", execution_id="ex_a", worker_runtime_id="pi-03", payload={"x": 1}
    )
    assert created is True
    second, created2 = outbox.create(
        idempotency_key="k1", execution_id="ex_a", worker_runtime_id="pi-03", payload={"x": 1}
    )
    assert created2 is False
    assert second.command_id == first.command_id


def test_outbox_delivery_stages_and_side_effect(tmp_path) -> None:
    outbox = DurableOutbox(tmp_path)
    command, _ = outbox.create(
        idempotency_key="k2", execution_id="ex_a", worker_runtime_id="pi-03", payload={"x": 2}
    )
    assert command.state == str(CommandState.CREATED)
    assert command.side_effect_state == str(SideEffectState.NOT_STARTED)
    outbox.claim(command.command_id)
    outbox.mark_sent(command.command_id)
    outbox.mark_delivered(command.command_id, provider_request_id="req-1")
    outbox.mark_acknowledged(command.command_id)
    final = outbox.mark_completed(command.command_id, result_ref="artifact://r")
    assert final.state == str(CommandState.COMPLETED)
    assert final.side_effect_state == str(SideEffectState.CONFIRMED)
    assert final.provider_request_id == "req-1"


def test_ambiguous_side_effect_never_auto_replays(tmp_path) -> None:
    outbox = DurableOutbox(tmp_path)
    command, _ = outbox.create(
        idempotency_key="k3", execution_id="ex_a", worker_runtime_id="pi-03", payload={"x": 3}
    )
    outbox.claim(command.command_id)
    outbox.mark_sent(command.command_id)
    outbox.mark_delivered(command.command_id)
    ambiguous = outbox.mark_ambiguous(command.command_id, error="ack lost")
    assert ambiguous.state == str(CommandState.AMBIGUOUS)
    assert ambiguous.side_effect_state == str(SideEffectState.AMBIGUOUS)
    assert outbox.can_replay(command.command_id) is False


def test_ambiguous_reconciliation_from_evidence(tmp_path) -> None:
    outbox = DurableOutbox(tmp_path)
    command, _ = outbox.create(
        idempotency_key="k4", execution_id="ex_a", worker_runtime_id="pi-03", payload={"x": 4}
    )
    outbox.mark_delivered(command.command_id)
    outbox.mark_ambiguous(command.command_id, error="conn lost")
    resolved = outbox.reconcile(command.command_id, confirmed=True)
    assert resolved.state == str(CommandState.COMPLETED)
    assert resolved.side_effect_state == str(SideEffectState.CONFIRMED)


def test_outbox_persists_pending_count_across_reload(tmp_path) -> None:
    outbox = DurableOutbox(tmp_path)
    outbox.create(
        idempotency_key="k5", execution_id="ex_a", worker_runtime_id="pi-03", payload={"x": 5}
    )
    reloaded = DurableOutbox(tmp_path)
    assert reloaded.pending_count("pi-03") == 1


# ── finish boundary: worker claim != COMPLETED ────────────────────────
def test_finish_boundary_blocks_until_quiescent() -> None:
    boundary = FinishBoundary(worker_final_claim=True, active_process_count=1, output_settled=False)
    assert boundary.can_complete() is False
    assert "ACTIVE_PROCESS_COUNT>0" in boundary.blocked_reasons()
    settled = FinishBoundary(
        worker_final_claim=True,
        active_tool_count=0,
        active_process_count=0,
        pending_command_count=0,
        output_settled=True,
        artifact_flushed=True,
        execution_store_flushed=True,
    )
    assert settled.can_complete() is True
    assert settled.blocked_reasons() == []


# ── A: live integration into the durable job manager ──────────────────
async def test_durable_job_manager_allocates_runtime_and_outbox(tmp_path) -> None:
    from veya.remote.execution import DurableJobManager, ExecutionStore, ExecutionType
    from veya.remote.outbox import CommandState, DurableOutbox, SideEffectState
    from veya.remote.worker_runtime import WorkerRuntimeRegistry, WorkerRuntimeState

    registry = WorkerRuntimeRegistry(tmp_path / "wr")
    outbox = DurableOutbox(tmp_path / "ob")
    manager = DurableJobManager(ExecutionStore(None), worker_registry=registry, outbox=outbox)
    session = SimpleNamespace(session_id="s", token_id="t", principal="p")
    binding = SimpleNamespace(
        requested_path="/w",
        requested_realpath="/w",
        repo_root="/w",
        repo_identity="x",
        worktree_path=None,
        worktree_repo_root=None,
    )

    async def runner(reporter):
        reporter.phase("RUNNING", message="go")
        return "ok"

    record = manager.submit(
        session=session,
        tool="shell.exec",
        veya_tool="x",
        binding=binding,
        runner=runner,
        execution_type=str(ExecutionType.DIRECT),
        execution_mode="direct_pi",
    )
    assert record.worker_runtime_id and record.worker_runtime_id.startswith("pi-")
    assert record.command_id
    assert record.command_state == str(CommandState.CREATED)
    await manager.wait(record.execution_id, timeout_s=5)
    assert record.status == "COMPLETED"
    assert record.command_state == str(CommandState.COMPLETED)
    assert record.side_effect_state == str(SideEffectState.CONFIRMED)
    assert registry.get(record.worker_runtime_id).state == str(WorkerRuntimeState.IDLE)
    public = record.to_public(heartbeat_timeout_s=5.0)
    assert public["worker_runtime_id"] == record.worker_runtime_id
    assert public["command_state"] == str(CommandState.COMPLETED)


async def test_worker_affinity_honored_and_explicit_when_unavailable(tmp_path) -> None:
    from veya.remote.execution import DurableJobManager, ExecutionStore, ExecutionType
    from veya.remote.worker_runtime import WorkerRuntimeRegistry

    registry = WorkerRuntimeRegistry(tmp_path / "wr")
    registry.register("pi", worker_runtime_id="pi-03")
    manager = DurableJobManager(ExecutionStore(None), worker_registry=registry)
    session = SimpleNamespace(session_id="s", token_id="t", principal="p")
    binding = SimpleNamespace(
        requested_path="/w",
        requested_realpath="/w",
        repo_root="/w",
        repo_identity="x",
        worktree_path=None,
        worktree_repo_root=None,
    )

    async def runner(reporter):
        return "ok"

    honored = manager.submit(
        session=session,
        tool="shell.exec",
        veya_tool="x",
        binding=binding,
        runner=runner,
        execution_type=str(ExecutionType.DIRECT),
        execution_mode="direct_pi",
        preferred_worker_runtime_id="pi-03",
    )
    assert honored.worker_runtime_id == "pi-03"
    assert honored.affinity_state == "AFFINITY_HONORED"
    assert honored.context_continuity_lost is False
    await manager.wait(honored.execution_id, timeout_s=5)

    missing = manager.submit(
        session=session,
        tool="shell.exec",
        veya_tool="x",
        binding=binding,
        runner=runner,
        execution_type=str(ExecutionType.DIRECT),
        execution_mode="direct_pi",
        preferred_worker_runtime_id="pi-999",
    )
    assert missing.worker_runtime_id != "pi-999"
    assert missing.affinity_state == "AFFINITY_UNAVAILABLE"
    assert missing.context_continuity_lost is True  # explicit fallback, never silent
    await manager.wait(missing.execution_id, timeout_s=5)
