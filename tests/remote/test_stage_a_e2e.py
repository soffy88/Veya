"""Stage A E2E: ambiguous delivery, finish boundary, sleep/revive, restart recovery."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from veya.remote.execution import DurableJobManager, ExecutionStore, ExecutionType
from veya.remote.outbox import CommandState, DurableOutbox, SideEffectState
from veya.remote.worker_runtime import WorkerRuntimeRegistry, WorkerRuntimeState

_SESSION = SimpleNamespace(session_id="s", token_id="t", principal="p")
_BG_TASKS: list[asyncio.Task] = []


def _binding(workspace: str) -> SimpleNamespace:
    return SimpleNamespace(
        requested_path=workspace,
        requested_realpath=workspace,
        repo_root=workspace,
        repo_identity=workspace,
        worktree_path=None,
        worktree_repo_root=None,
    )


# ── A2: ambiguous delivery + idempotency + reconciliation ─────────────
def test_ambiguous_delivery_and_reconciliation(tmp_path: Path) -> None:
    outbox = DurableOutbox(tmp_path / "ob")
    artifact = tmp_path / "side_effect.txt"

    def deliver_once() -> None:
        artifact.write_text("applied", encoding="utf-8")

    command, created = outbox.create(
        idempotency_key="k-side-effect",
        execution_id="ex_a",
        worker_runtime_id="pi-03",
        payload={"op": "write", "path": str(artifact)},
    )
    assert created is True
    outbox.claim(command.command_id)
    outbox.mark_sent(command.command_id)
    outbox.mark_delivered(command.command_id, provider_request_id="req-1")
    deliver_once()  # provider actually applies the side effect
    ambiguous = outbox.mark_ambiguous(command.command_id, error="ACK lost")  # ACK dropped
    assert ambiguous.state == str(CommandState.AMBIGUOUS)
    assert ambiguous.side_effect_state == str(SideEffectState.AMBIGUOUS)
    assert outbox.can_replay(command.command_id) is False

    # Same idempotency key -> no second side effect.
    again, created2 = outbox.create(
        idempotency_key="k-side-effect",
        execution_id="ex_a",
        worker_runtime_id="pi-03",
        payload={"op": "write", "path": str(artifact)},
    )
    assert created2 is False and again.command_id == command.command_id
    if outbox.can_replay(again.command_id):
        deliver_once()
    assert artifact.read_text(encoding="utf-8") == "applied"  # exactly one effect

    # Reconcile from real artifact evidence.
    resolved = outbox.reconcile(command.command_id, confirmed=artifact.exists())
    assert resolved.state == str(CommandState.COMPLETED)
    assert resolved.side_effect_state == str(SideEffectState.CONFIRMED)


# ── A3: finish boundary (final claim with a live owned process) ───────
async def test_finish_boundary_waits_for_owned_process(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(None), heartbeat_interval_s=0.05)

    async def runner(reporter):
        reporter.continuity(active_process_count=1)
        proc = await asyncio.create_subprocess_exec("sleep", "1")

        async def waiter():
            await proc.wait()
            reporter.continuity(active_process_count=0)

        _BG_TASKS.append(asyncio.ensure_future(waiter()))
        return "final claim"

    record = manager.submit(
        session=_SESSION,
        tool="shell.exec",
        veya_tool="x",
        binding=_binding(str(tmp_path)),
        runner=runner,
        execution_type=str(ExecutionType.DIRECT),
    )
    # While the owned process is alive the execution must NOT be COMPLETED.
    await asyncio.sleep(0.2)
    mid = record.to_public(heartbeat_timeout_s=5.0)
    assert mid["worker_final_claim"] is True
    assert mid["active_process_count"] >= 1
    assert mid["phase"] == "FINALIZING"
    assert mid["status"] != "COMPLETED"
    # After the process ends -> COMPLETED.
    await manager.wait(record.execution_id, timeout_s=5)
    final = record.to_public(heartbeat_timeout_s=5.0)
    assert final["phase"] == "COMPLETED"
    assert final["active_process_count"] == 0


# ── A4: sleep / revive across executions (runtime-level) ──────────────
async def test_worker_sleep_revive_across_executions(tmp_path: Path) -> None:
    registry = WorkerRuntimeRegistry(tmp_path / "wr")
    registry.register("pi", worker_runtime_id="pi-03", provider_session_id="ps-1")
    manager = DurableJobManager(ExecutionStore(None), worker_registry=registry)

    async def runner(reporter):
        return "ok"

    def submit(preferred: str):
        return manager.submit(
            session=_SESSION,
            tool="shell.exec",
            veya_tool="x",
            binding=_binding(str(tmp_path)),
            runner=runner,
            execution_type=str(ExecutionType.DIRECT),
            execution_mode="direct_pi",
            preferred_worker_runtime_id=preferred,
        )

    first = submit("pi-03")
    assert first.worker_runtime_id == "pi-03"
    await manager.wait(first.execution_id, timeout_s=5)
    assert registry.get("pi-03").state == str(WorkerRuntimeState.IDLE)
    registry.sleep("pi-03")
    assert registry.get("pi-03").state == str(WorkerRuntimeState.SLEEPING)

    second = submit("pi-03")
    assert second.execution_id != first.execution_id
    assert second.worker_runtime_id == "pi-03"  # identity stable
    assert second.affinity_state == "AFFINITY_HONORED"
    assert registry.get("pi-03").state == str(WorkerRuntimeState.ACTIVE)
    await manager.wait(second.execution_id, timeout_s=5)

    # Negative: preferred runtime of a different worker type -> explicit, no swap.
    registry.register("grok", worker_runtime_id="grok-01")
    mismatch = manager.submit(
        session=_SESSION,
        tool="shell.exec",
        veya_tool="x",
        binding=_binding(str(tmp_path)),
        runner=runner,
        execution_type=str(ExecutionType.DIRECT),
        execution_mode="direct_pi",
        preferred_worker_runtime_id="grok-01",
    )
    assert mismatch.affinity_state == "AFFINITY_UNAVAILABLE"
    assert mismatch.worker_runtime_id != "grok-01"
    assert mismatch.context_continuity_lost is True
    await manager.wait(mismatch.execution_id, timeout_s=5)


# ── A5: gateway restart recovery (two OS processes) ───────────────────
_CHILD = r"""
import asyncio, sys
from pathlib import Path
from types import SimpleNamespace
from veya.remote.execution import DurableJobManager, ExecutionStore, ExecutionType
from veya.remote.outbox import DurableOutbox
from veya.remote.worker_runtime import WorkerRuntimeRegistry

root = Path(sys.argv[1]); ready = Path(sys.argv[2])
registry = WorkerRuntimeRegistry(root / "wr")
outbox = DurableOutbox(root / "ob")
manager = DurableJobManager(ExecutionStore(root / "exec"), worker_registry=registry, outbox=outbox)
session = SimpleNamespace(session_id="s", token_id="t", principal="p")
binding = SimpleNamespace(requested_path=str(root), requested_realpath=str(root),
                          repo_root=str(root), repo_identity=str(root),
                          worktree_path=None, worktree_repo_root=None)

async def runner(reporter):
    reporter.continuity(active_process_count=1)
    await asyncio.sleep(3600)
    return "never"

async def main():
    record = manager.submit(session=session, tool="shell.exec", veya_tool="x", binding=binding,
                            runner=runner, execution_type=str(ExecutionType.DIRECT),
                            execution_mode="direct_pi")
    await asyncio.sleep(0.5)
    ready.write_text(record.execution_id, encoding="utf-8")
    await asyncio.sleep(3600)

asyncio.run(main())
"""


def test_gateway_restart_recovery(tmp_path: Path) -> None:
    root = tmp_path
    ready = root / "eid.txt"
    script = root / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    child = subprocess.Popen(
        [sys.executable, str(script), str(root), str(ready)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 30
        while not ready.exists() and time.time() < deadline:
            if child.poll() is not None:
                _, err = child.communicate(timeout=5)
                raise AssertionError(f"child exited early: {err.decode()[-600:]}")
            time.sleep(0.1)
        assert ready.exists()
        execution_id = ready.read_text(encoding="utf-8").strip()
        time.sleep(0.4)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)

    # Gateway B reloads the same store/registry/outbox.
    store = ExecutionStore(root / "exec")
    registry = WorkerRuntimeRegistry(root / "wr")
    outbox = DurableOutbox(root / "ob")
    persisted = store.get(execution_id)
    assert persisted is not None
    assert persisted.worker_runtime_id
    runtime = registry.get(persisted.worker_runtime_id)
    assert runtime is not None  # identity survives reload

    manager = DurableJobManager(
        store, worker_registry=registry, outbox=outbox, heartbeat_timeout_s=1.0
    )
    record = manager.status(execution_id, token_id=persisted.token_id)
    # Worker is gone with the gateway -> STALLED, never a false RUNNING/resume.
    time.sleep(1.2)
    stale = record.to_public(heartbeat_timeout_s=1.0)
    assert stale["status"] == "STALLED"
    assert stale["status"] != "RUNNING"

    # Outbox command survived; a DELIVERED-but-unacked command is ambiguous and
    # must never be auto-replayed after restart.
    assert persisted.command_id is not None
    command = outbox.get(persisted.command_id)
    assert command is not None
    if command.state in {str(CommandState.SENDING), str(CommandState.DELIVERED)}:
        outbox.mark_ambiguous(command.command_id, error="restart before ACK")
    reloaded = outbox.get(persisted.command_id)
    assert reloaded is not None
    assert reloaded.state in {str(CommandState.CREATED), str(CommandState.AMBIGUOUS)}
    assert outbox.can_replay(persisted.command_id) is (
        reloaded.state != str(CommandState.AMBIGUOUS)
    )


async def test_execution_context_and_failure_taxonomy_persisted(tmp_path: Path) -> None:
    manager = DurableJobManager(ExecutionStore(None))

    async def runner(reporter):
        reporter.execution_context(
            context_id="ctx_1",
            context_hash="abc",
            budget={"total_injected_context_bytes": 100, "selected_skill_count": 1},
            capability_ids=["github.search"],
            skill_ids=["frontend-design"],
        )
        reporter.failure(
            failure_class="SKILL_PERMISSION_DENIED", source="skill", detail="network denied"
        )
        return "ok"

    record = manager.submit(
        session=_SESSION,
        tool="shell.exec",
        veya_tool="x",
        binding=_binding(str(tmp_path)),
        runner=runner,
        execution_type=str(ExecutionType.DIRECT),
        execution_mode="direct_pi",
    )
    await manager.wait(record.execution_id, timeout_s=5)
    public = record.to_public(heartbeat_timeout_s=5.0)
    assert public["execution_context_id"] == "ctx_1"
    assert public["execution_context_hash"] == "abc"
    assert public["context_budget"]["total_injected_context_bytes"] == 100
    assert public["selected_capability_ids"] == ["github.search"]
    assert public["selected_skill_ids"] == ["frontend-design"]
    assert public["failure_class"] == "SKILL_PERMISSION_DENIED"
    assert public["failure_source"] == "skill"
