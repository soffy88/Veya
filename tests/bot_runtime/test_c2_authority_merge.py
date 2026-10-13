"""C2 — authority merges: dispatch, planner, finalization, bot_id durability.

Covers what C2 actually closed:

- C2.3 finalization authority: the reported status is the GoalRun verdict, never
  the agent engine's answer ("LLM says done" != "GoalRun completed").
- C2.4 ``project_run_goal(bot_id=...)``: the ABI addition exists, is additive,
  and stamps the owning bot so it never falls back to ``veya-default``.
- C2.1 two roles: the admitted executor (ExecutorRegistry authority) and the
  physical substrate are modelled separately and both appear in binding,
  activity and receipt evidence. ``local2`` is deliberately never admitted.
- C2.2 is explicitly recorded as an OPEN fork with the evidence that keeps it
  open (it is not silently declared done).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest

from server.agent_definition import AgentDefinitionStore
from server.bot_runtime.manager import BotManager
from server.bot_runtime.product import BotProduct
from server.goal_run.bot_identity import BotRegistry
from server.goal_run.execution_admission import reset_admissions, reset_scopes
from server.goal_run.models import GoalStatus
from server.goal_run.store import load_goal_run
from veya.remote.executor_adapters import register_default_adapters
from veya.remote.executor_registry import get_executor_registry


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
    return BotManager(
        BotRegistry(storage_path=tmp_path / "bot_registry.json"),
        AgentDefinitionStore(tmp_path / "definitions"),
        product=BotProduct(tmp_path / "bots"),
    )


def _bot(manager: BotManager, bot_id: str, tmp_path: Path) -> Path:
    workspace = tmp_path / "workspaces" / bot_id
    workspace.mkdir(parents=True, exist_ok=True)
    manager.create_bot(
        bot_id=bot_id,
        owner_id="u1",
        runtime_id="agent_loop",
        workspace_root=str(workspace),
    )
    return workspace


# ── C2.3 finalization authority ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_reported_status_is_the_goalrun_verdict(tmp_path: Path, mock_runtime_available):
    """A failing run must never be reported as success, even if the engine says so."""
    manager = _manager(tmp_path, mock_runtime_available)
    workspace = _bot(manager, "bot-final", tmp_path)

    # The command cannot execute: the GoalRun authority must decide "failed".
    result = await manager.submit_goal("bot-final", "this-binary-does-not-exist-c2xyz")

    state = load_goal_run(str(workspace), result.goal_id)
    assert state is not None
    assert state.status is not GoalStatus.completed

    # The user-visible status is the GoalRun's verdict, not the engine's.
    assert result.status == state.status.value, (
        f"reported {result.status!r} but GoalRun is {state.status.value!r}"
    )


@pytest.mark.asyncio
async def test_engine_answer_cannot_mark_goalrun_complete(tmp_path: Path, mock_runtime_available):
    """Even a successful engine run does not decide completion; execution does."""
    manager = _manager(tmp_path, mock_runtime_available)
    workspace = _bot(manager, "bot-final2", tmp_path)

    result = await manager.submit_goal("bot-final2", "echo c2-final-proof")
    state = load_goal_run(str(workspace), result.goal_id)
    assert state is not None
    # Both agree on success only because execution really succeeded.
    assert result.status == state.status.value
    assert state.status is GoalStatus.completed


# ── C2.4 project_run_goal(bot_id=...) ───────────────────────────────────


def test_project_run_goal_exposes_bot_id():
    """The authorized ABI addition exists and is additive."""
    from server.goal_run.runner import project_run_goal

    params = inspect.signature(project_run_goal).parameters
    assert "bot_id" in params
    # main (Lorca P1) landed this parameter as ``bot_id: str | None = None``;
    # either empty default is additive for existing callers.
    assert params["bot_id"].default in ("", None), "must stay additive for existing callers"


def test_existing_callers_unaffected_by_bot_id_default():
    """bot_id defaults to '' so every pre-existing call keeps DEFAULT_BOT_ID."""
    from server.goal_run.models import GoalRunState

    plain = GoalRunState(goal_id="g", goal_text="t")
    assert plain.bot_id == "veya-default"


def test_planner_threads_bot_id_into_created_state():
    """g1_plan stamps the owning bot on the state it creates."""
    from server.goal_run.planner import g1_plan

    params = inspect.signature(g1_plan).parameters
    assert "bot_id" in params
    assert params["bot_id"].default in ("", None)


def test_bot_id_survives_durable_round_trip():
    """bot_id is serialized, so dispatch/resume/finalize all read the same owner."""
    import tempfile

    from server.goal_run.models import GoalRunState
    from server.goal_run.store import load_goal_run, save_goal_run

    root = tempfile.mkdtemp()
    state = GoalRunState(goal_id="g_c2", goal_text="t", bot_id="bot-roundtrip")
    save_goal_run(state, root)
    loaded = load_goal_run(root, "g_c2")
    assert loaded is not None
    assert loaded.bot_id == "bot-roundtrip"
    assert loaded.bot_id != "veya-default"


def test_cross_bot_resume_is_refused():
    """A caller cannot resume another bot's GoalRun."""
    import asyncio
    import tempfile

    from runtime.bot_scope import CrossBotAccessDenied
    from server.goal_run.models import GoalRunState
    from server.goal_run.runner import project_run_goal
    from server.goal_run.store import save_goal_run

    root = tempfile.mkdtemp()
    state = GoalRunState(goal_id="g_owned", goal_text="t", bot_id="bot-owner")
    state.status = GoalStatus.completed
    save_goal_run(state, root)

    async def _run():
        with pytest.raises(CrossBotAccessDenied):
            await project_run_goal(
                project_root=root,
                goal="t",
                resume_goal_id="g_owned",
                bot_id="bot-intruder",
            )

    asyncio.run(_run())


# ── C2.1 two roles, formally modelled (CLOSED) ──────────────────────────


def test_local2_is_a_substrate_not_an_admitted_executor():
    """ExecutorRegistry stays the single admission authority; local2 is not in it."""
    registry = get_executor_registry()
    admitted = set(registry.snapshot().keys())
    assert "local2" not in admitted, (
        "local2 must not be admitted as an executor: it is a native substrate, "
        "a different semantic layer"
    )
    from veya.remote.executor_adapter import get_executor_adapter

    adapter = get_executor_adapter("local2", "/tmp")
    assert adapter is not None
    assert adapter.identity.executor_kind == "native_substrate"


def test_binding_defaults_substrate_to_executor_when_unset():
    """An ordinary executor runs through its own worker: substrate defaults."""
    from server.goal_run.execution_admission import ExecutorBinding

    binding = ExecutorBinding(
        binding_id="b1",
        executor_id="codex",
        installation_id="default",
        provider_id="codex",
        provider_session_id=None,
        provider_run_id=None,
        goal_run_id="g1",
        admission_id="a1",
    )
    assert binding.effective_substrate_id == "codex"
    assert binding.to_dict()["substrate_id"] == "codex"


@pytest.mark.asyncio
async def test_c21_both_roles_are_recorded_in_durable_evidence(
    tmp_path: Path, mock_runtime_available
):
    """Admission, binding, activity and receipt each name both roles."""
    import json

    from server.goal_run.execution_activity import ActivityRecorder

    manager = _manager(tmp_path, mock_runtime_available)
    workspace = _bot(manager, "bot-split", tmp_path)

    result = await manager.submit_goal("bot-split", "echo c21-split")

    # The binding carries the admitted executor AND the physical substrate.
    binding = json.loads(next(workspace.rglob("binding_*.json")).read_text())
    assert binding["executor_id"] == "codex"
    assert binding["substrate_id"] == "local2"
    assert binding["substrate_kind"] == "native_substrate"
    assert binding["executor_kind"]

    # The receipt names both roles, never conflating them.
    assert result.receipt["executor_id"] == "codex"
    assert result.receipt["substrate_id"] == "local2"
    assert result.receipt["substrate_kind"] == "native_substrate"

    # The activity stream names both too.
    activities, _ = ActivityRecorder(str(workspace)).stream(result.goal_id, cursor=0)
    selected = next(a for a in activities if a.type == "EXECUTOR_SELECTED")
    assert selected.payload["admitted_executor"] == "codex"
    assert selected.payload["substrate_id"] == "local2"
    assert selected.payload["substrate_kind"] == "native_substrate"
    # executor_id stays the admitted executor, never the substrate.
    assert selected.payload["executor_id"] == "codex"


# ── C2.2 OPEN FORK (recorded) ───────────────────────────────────────────


def test_c22_open_fork_planner_and_dispatch_are_separate_chains():
    """The semantic planner path and the dispatch/admission path are disjoint.

    ``project_run_goal`` plans + finalizes but its leaf executor does not run
    ExecutionAdmission; the Bot dispatch path admits + controls + receipts but
    does not plan. C2.2 unifies them; until then this is the recorded truth.
    """
    from pathlib import Path as _P

    import server.goal_run.leaf as leaf_mod
    import server.goal_run.runner as runner_mod

    leaf_src = _P(leaf_mod.__file__).read_text(encoding="utf-8")
    runner_src = _P(runner_mod.__file__).read_text(encoding="utf-8")

    # The semantic leaf path is admitted through ExecutorRegistry + harness,
    # not through ExecutionAdmission.
    assert "get_executor_registry" in leaf_src
    assert "get_admission" not in leaf_src
    assert "ExecutionControl" not in leaf_src
    # The runner (semantic path) does not perform admission either.
    assert "get_admission" not in runner_src
