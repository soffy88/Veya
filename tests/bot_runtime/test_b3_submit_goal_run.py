"""B3 / C1 — ``BotRuntime.submit()`` enters the real durable execution plane.

Acceptance is deliberately hard: nothing below the semantic seam is mocked and
no assertion may be satisfied by "success or failed".

    BotRuntime.submit()
      -> create_pending_run(bot_id=...)   canonical, bot-scoped GoalRun
      -> bind_execution()                ExecutionAdmission + ExecutorBinding
      -> ExecutionControl.run_task()     Local2 substrate
      -> ActivityRecorder                unified evidence stream
      -> ExecutionReceipt                durable receipt
      -> MemoryController                bot-scoped long-term memory

Also asserted: the fake execution authority is gone (no ``_goal_run_store``, no
hand-built ``GoalRunState``), and two bots stay isolated on GoalRun state,
computer and workspace.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.bot_scope import CrossBotAccessDenied
from server.agent_definition import AgentDefinitionStore
from server.bot_runtime.manager import BotManager
from server.bot_runtime.product import BotProduct
from server.goal_run.bot_identity import BotRegistry
from server.goal_run.execution_activity import ActivityRecorder
from server.goal_run.execution_admission import reset_admissions, reset_scopes
from server.goal_run.models import GoalStatus
from server.goal_run.store import load_goal_run
from veya.remote.executor_adapters import register_default_adapters


@pytest.fixture(autouse=True)
def _reset():
    register_default_adapters()
    reset_admissions()
    reset_scopes()
    yield
    reset_admissions()
    reset_scopes()


@pytest.fixture
def mock_runtime_available():
    def mock_available(runtime_id):
        return {
            "available": True,
            "reason": "",
            "kind": "provider",
            "runtime_id": runtime_id,
            "protocol_family": "llm",
            "capabilities": [],
            "lifecycle": {},
        }

    with (
        patch("server.bot_runtime.manager.check_runtime_available") as m1,
        patch("server.agent_definition.check_runtime_available") as m2,
    ):
        m1.side_effect = mock_available
        m2.side_effect = mock_available
        yield m1, m2


def _manager(tmp_path: Path, mock_runtime_available) -> BotManager:
    product = BotProduct(tmp_path / "bots")
    return BotManager(
        BotRegistry(storage_path=tmp_path / "bot_registry.json"),
        AgentDefinitionStore(tmp_path / "definitions"),
        product=product,
    )


def _bot(manager: BotManager, bot_id: str, tmp_path: Path) -> Path:
    """Create a bot whose computer workspace lives inside tmp_path."""
    workspace = tmp_path / "workspaces" / bot_id
    workspace.mkdir(parents=True, exist_ok=True)
    manager.create_bot(
        bot_id=bot_id,
        owner_id="u1",
        runtime_id="agent_loop",
        workspace_root=str(workspace),
    )
    return workspace


# ── static: the fake execution authority is removed ─────────────────────


def test_fake_goal_run_authority_is_gone():
    """No Bot-local GoalRun store and no hand-built GoalRunState remain."""
    import inspect

    from server.bot_runtime import runtime as runtime_mod

    source = inspect.getsource(runtime_mod)
    assert "_goal_run_store" not in source, "BotRuntime must not own a GoalRun store"
    assert "GoalRunState(" not in source, "BotRuntime must not construct GoalRunState"


def test_manager_exposes_no_goal_run_store():
    import inspect

    from server.bot_runtime import manager as manager_mod

    source = inspect.getsource(manager_mod)
    assert "_goal_run_store" not in source


# ── the real chain ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_persists_bot_scoped_goalrun(tmp_path: Path, mock_runtime_available):
    manager = _manager(tmp_path, mock_runtime_available)
    workspace = _bot(manager, "bot-a", tmp_path)

    result = await manager.submit_goal("bot-a", "echo b3-submit-ok")

    # A real GoalRun id came back (not empty / not synthesised).
    assert result.goal_id, "submit must return the canonical GoalRun id"
    assert result.execution_id

    # The GoalRun really exists on disk, inside the bot's own workspace.
    state = load_goal_run(str(workspace), result.goal_id)
    assert state is not None, "GoalRun must be persisted durably"
    assert state.bot_id == "bot-a", "GoalRun must be bot-scoped, never veya-default"
    assert state.status == GoalStatus.completed


@pytest.mark.asyncio
async def test_submit_enters_admission_and_binding(tmp_path: Path, mock_runtime_available):
    manager = _manager(tmp_path, mock_runtime_available)
    workspace = _bot(manager, "bot-adm", tmp_path)

    result = await manager.submit_goal("bot-adm", "echo admission")

    state = load_goal_run(str(workspace), result.goal_id)
    assert state is not None
    checkpoint = state.runtime_checkpoint or {}
    assert checkpoint.get("admission_id"), "run must be bound to an admission"
    assert checkpoint.get("binding_id"), "run must be bound to an executor binding"

    assert list(workspace.rglob("admission_*.json")), "admission record must be durable"
    assert list(workspace.rglob("binding_*.json")), "binding record must be durable"


@pytest.mark.asyncio
async def test_submit_runs_local2_for_real(tmp_path: Path, mock_runtime_available):
    """Local2 really executes: stdout comes back from the activity stream."""
    manager = _manager(tmp_path, mock_runtime_available)
    workspace = _bot(manager, "bot-local2", tmp_path)

    result = await manager.submit_goal("bot-local2", "echo real-local2-proof")

    recorder = ActivityRecorder(str(workspace))
    activities, _cursor = recorder.stream(result.goal_id, cursor=0)
    types = [a.type for a in activities]
    assert "PROCESS_STARTED" in types
    assert "OUTPUT" in types, "Local2 must produce real OUTPUT evidence"
    assert "GOALRUN_COMPLETED" in types

    stdout = "".join(
        a.payload.get("text") or a.payload.get("tail") or ""
        for a in activities
        if a.type == "OUTPUT"
    ).strip()
    assert stdout.endswith("real-local2-proof"), f"unexpected stdout: {stdout!r}"


@pytest.mark.asyncio
async def test_submit_writes_activity_receipt_and_memory(tmp_path: Path, mock_runtime_available):
    manager = _manager(tmp_path, mock_runtime_available)
    workspace = _bot(manager, "bot-evidence", tmp_path)

    result = await manager.submit_goal("bot-evidence", "echo evidence")

    # Activity sequence is monotonic and non-zero.
    recorder = ActivityRecorder(str(workspace))
    activities, _ = recorder.stream(result.goal_id, cursor=0)
    sequences = [a.sequence for a in activities]
    assert sequences == sorted(sequences)
    assert result.activity_sequence == max(sequences)

    # Durable receipt carries the real identifiers.
    assert result.receipt
    assert result.receipt["goal_run_id"] == result.goal_id
    assert result.receipt["admission_id"]
    assert result.receipt["binding_id"]
    # C2.1: the receipt names both roles. ``executor_id`` is the ADMITTED
    # executor the grant was issued for; ``substrate_id`` is the physical
    # substrate the work actually ran on (local2 / native_substrate).
    assert result.receipt["executor_id"] == "codex"
    assert result.receipt["substrate_id"] == "local2"
    assert result.receipt["substrate_kind"] == "native_substrate"
    receipts = list(workspace.rglob("execution_receipts.jsonl"))
    assert receipts, "receipt must be appended durably"

    # Bot-scoped long-term memory write really happened.
    assert result.memory_id, "submit must write a long-term memory record"


# ── isolation between bots ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_bots_get_separate_bot_scoped_goalruns(tmp_path: Path, mock_runtime_available):
    """Bot A submit -> bot_id A; Bot B submit -> bot_id B. Never veya-default."""
    manager = _manager(tmp_path, mock_runtime_available)
    ws_a = _bot(manager, "bot-iso-a", tmp_path)
    ws_b = _bot(manager, "bot-iso-b", tmp_path)

    result_a = await manager.submit_goal("bot-iso-a", "echo alpha")
    result_b = await manager.submit_goal("bot-iso-b", "echo bravo")

    assert result_a.goal_id != result_b.goal_id

    state_a = load_goal_run(str(ws_a), result_a.goal_id)
    state_b = load_goal_run(str(ws_b), result_b.goal_id)
    assert state_a is not None and state_b is not None
    assert state_a.bot_id == "bot-iso-a"
    assert state_b.bot_id == "bot-iso-b"
    assert "veya-default" not in (state_a.bot_id, state_b.bot_id)

    # Each bot's GoalRun lives only in its own workspace.
    assert list(ws_b.rglob(f"*{result_a.goal_id}*")) == []
    assert list(ws_a.rglob(f"*{result_b.goal_id}*")) == []


@pytest.mark.asyncio
async def test_bot_cannot_access_other_bot_computer(tmp_path: Path, mock_runtime_available):
    """Cross-bot computer access is refused fail-closed in both directions."""
    manager = _manager(tmp_path, mock_runtime_available)
    _bot(manager, "bot-ca", tmp_path)
    _bot(manager, "bot-cb", tmp_path)

    computer_a = manager.get_computer("bot-ca")
    computer_b = manager.get_computer("bot-cb")
    assert computer_a is not None and computer_b is not None
    assert computer_a.computer_id != computer_b.computer_id
    assert computer_a.workspace_root != computer_b.workspace_root

    computers = manager._computers
    with pytest.raises(CrossBotAccessDenied):
        computers.get_persistent(computer_b.computer_id, bot_id="bot-ca")
    with pytest.raises(CrossBotAccessDenied):
        computers.get_persistent(computer_a.computer_id, bot_id="bot-cb")
    with pytest.raises(CrossBotAccessDenied):
        computers.require("bot-ca", computer_b.computer_id)


@pytest.mark.asyncio
async def test_submit_refuses_without_bound_computer(tmp_path: Path, mock_runtime_available):
    """Fail-closed: no computer means no execution at all."""
    manager = _manager(tmp_path, mock_runtime_available)
    _bot(manager, "bot-nocomp", tmp_path)
    computer = manager.get_computer("bot-nocomp")
    assert computer is not None

    manager._computers.store.delete_computer(computer.computer_id)

    from server.bot_runtime.runtime import BotRuntimeError

    with pytest.raises(BotRuntimeError, match="no persistent computer"):
        await manager.submit_goal("bot-nocomp", "echo should-not-run")

    # No GoalRun was created anywhere.
    workspace = tmp_path / "workspaces" / "bot-nocomp"
    assert list(workspace.rglob("taskgraph.json")) == []
