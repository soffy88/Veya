"""W12 — supervised-run end-to-end: phrase → routine fire → bot conversation.

Regression for the live failure where a bot said
"每 5 分钟告诉我进度：统计 README.md 有多少行" and the supervisor was started
under the wrong bot, so the routine fire could not find the binding and the
report never reached the user.

Three defects are pinned here:
  1. the phrase path did not bind the bot scope, so ``supervise_task`` filed the
     binding / routine / report under "veya-default" instead of the real bot;
  2. ``_try_supervisor_handoff`` looked the binding up by ``owner_id`` (wrong
     field) instead of the routine it fires on;
  3. the legacy unscoped path wrote to ``project_root="."`` — the read-only
     process cwd in production.

All offline: no real model, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import ROOT  # noqa: F401 — path bootstrap


@pytest.fixture
def supervised_env(monkeypatch, tmp_path: Path):
    """Enable the supervisor and isolate every store it touches."""
    monkeypatch.setenv("VEYA_SUPERVISOR", "1")
    monkeypatch.setenv("VEYA_SUPERVISOR_BINDING_PATH", str(tmp_path / "supervisor_bindings.json"))
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routine_registry.json"))
    monkeypatch.setenv("VEYA_ROUTINE_CLOCK_PATH", str(tmp_path / "routine_clock.json"))
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    monkeypatch.setenv("VEYA_WRITE_ROOT", str(tmp_path / "work"))
    yield tmp_path


def _start_supervisor_phrase(bot_id: str, text: str) -> None:
    """Drive the phrase path exactly as the HTTP route does: no bot scope bound.

    ``run_bot_turn`` reaches ``_supervise_from_phrase`` before
    ``_run_product_task`` binds the running bot, so ``current_bot_id()`` is
    ``None`` here — the fix must bind it internally.
    """
    import asyncio
    from types import SimpleNamespace

    from server.bot_team import turns
    from server.bot_team.conversations import conversation_store

    bot = SimpleNamespace(bot_id=bot_id)
    asyncio.run(
        turns._supervise_from_phrase(
            bot=bot,
            text=text,
            user={},
            workspace_id=None,
            conversations=conversation_store(),
        )
    )


def _complete_all_tasks(project_root: str, goal_run_id: str, evidence: str) -> None:
    """Mark every task of a GoalRun completed, with evidence."""
    from server.goal_run.models import TaskNode, TaskStatus
    from server.goal_run.store import load_goal_run, save_goal_run

    state = load_goal_run(project_root, goal_run_id)
    state.tasks["t1"] = TaskNode(
        id="t1",
        title="统计 README.md 行数",
        instruction="wc -l README.md",
        acceptance=["给出数字"],
        depends_on=[],
        assignee="worker",
        status=TaskStatus("completed"),
        execute_result=evidence,
    )
    save_goal_run(state, project_root)


def _set_partial_progress(project_root: str, goal_run_id: str) -> None:
    """One completed task of two — a reportable but unfinished run."""
    from server.goal_run.models import TaskNode, TaskStatus
    from server.goal_run.store import load_goal_run, save_goal_run

    state = load_goal_run(project_root, goal_run_id)
    for tid, status in (("t1", "completed"), ("t2", "running")):
        state.tasks[tid] = TaskNode(
            id=tid,
            title=tid,
            instruction=f"do {tid}",
            acceptance=["ok"],
            depends_on=[],
            assignee="worker",
            status=TaskStatus(status),
            execute_result="README.md 有 42 行" if status == "completed" else None,
        )
    save_goal_run(state, project_root)


def _fire_clock(routine_id: str) -> list[dict]:
    """Fire the routine clock once, forcing ``routine_id`` to be due."""
    import asyncio

    from server.bot_team.routine_clock import RoutineClock
    from server.goal_run.routine_registry import RoutineRegistry

    clock = RoutineClock(RoutineRegistry())
    clock.last_fired = {routine_id: "2020-01-01T00:00:00+00:00"}
    return asyncio.run(clock.tick())


def _bot_messages(bot_id: str) -> list[dict]:
    from server.bot_team.conversations import conversation_store

    return conversation_store().list(bot_id, include_hidden=True)


# ---------------------------------------------------------------------------
# Defect 1 + 2: the binding is owned by the real bot and the fire finds it
# ---------------------------------------------------------------------------


def test_phrase_binds_real_bot_and_fire_reaches_conversation(supervised_env):
    """phrase → supervise → routine fire posts a progress report into the
    bot's own conversation (the live bug: it went to veya-default)."""
    from server.supervisor import binding as supervisor_binding

    bot_id = "bot_cf4577f3"
    _start_supervisor_phrase(bot_id, "每 5 分钟告诉我进度：统计 README.md 有多少行")

    bindings = supervisor_binding.list_bindings()
    assert len(bindings) == 1
    binding = bindings[0]
    # Defect 1: the binding must be owned by the bot that spoke, not the default.
    assert binding.bot_id == bot_id
    assert binding.owner_id == bot_id

    # The routine is scoped to the same bot, so the clock dispatches it as such.
    from server.goal_run.routine_registry import RoutineRegistry

    routine = RoutineRegistry().get(binding.routine_id)
    assert routine is not None
    assert routine.bot_id == bot_id

    _set_partial_progress(binding.project_root, binding.goal_run_id)

    handoffs = _fire_clock(binding.routine_id)
    assert len(handoffs) == 1
    assert handoffs[0]["bot_id"] == bot_id

    # Defect 2: the report lands in the *bot's* conversation, tagged supervisor.
    messages = _bot_messages(bot_id)
    reports = [m for m in messages if (m.get("meta") or {}).get("source") == "supervisor"]
    assert reports, "no supervisor progress report reached the bot's conversation"
    assert "已完成 1/2" in reports[0]["text"]

    # And nothing leaked into the default bot's conversation.
    assert not _bot_messages("veya-default")


def test_handoff_matches_by_routine_not_owner(supervised_env):
    """A binding whose owner_id differs from the firing bot is still found by
    its routine id (the pre-fix lookup filtered on owner_id and missed it)."""
    import asyncio

    from server.bot_team.routine_clock import _try_supervisor_handoff
    from server.goal_run.models import GoalRunState, GoalStatus
    from server.goal_run.store import save_goal_run
    from server.supervisor import binding as supervisor_binding
    from server.supervisor.binding import SupervisorBinding

    project_root = str(supervised_env / "work" / "supervised" / "bot_x")
    Path(project_root).mkdir(parents=True, exist_ok=True)
    goal_id = "goal_owner_mismatch"
    save_goal_run(
        GoalRunState(
            goal_id=goal_id, goal_text="X", status=GoalStatus.running, bot_id="veya-default"
        ),
        project_root,
    )
    supervisor_binding.save_binding(
        SupervisorBinding(
            binding_id="sup_mismatch",
            goal_run_id=goal_id,
            routine_id="routine_mismatch",
            owner_id="veya-default",  # legacy: filed under the default bot
            bot_id="veya-default",
            objective="X",
            project_root=project_root,
        )
    )

    handled = asyncio.run(_try_supervisor_handoff(routine_id="routine_mismatch", bot_id="bot_x"))
    assert handled is True, "binding must be found by routine_id, not owner_id"


# ---------------------------------------------------------------------------
# Completion: final report + binding closed + routine disabled
# ---------------------------------------------------------------------------


def test_completed_goal_posts_final_report_and_closes(supervised_env):
    """When the verifier passes (all tasks done), the fire posts the final
    report, closes the binding and disables the routine."""
    from server.goal_run.routine_registry import RoutineRegistry
    from server.supervisor import binding as supervisor_binding

    bot_id = "bot_final"
    _start_supervisor_phrase(bot_id, "每 5 分钟告诉我进度：统计 README.md 有多少行")
    binding = supervisor_binding.list_bindings()[0]

    _complete_all_tasks(binding.project_root, binding.goal_run_id, "README.md 有 42 行")

    handoffs = _fire_clock(binding.routine_id)
    assert len(handoffs) == 1

    reports = [
        m for m in _bot_messages(bot_id) if (m.get("meta") or {}).get("source") == "supervisor"
    ]
    assert reports, "no final report reached the bot's conversation"
    assert "任务完成" in reports[0]["text"]
    assert "README.md 有 42 行" in reports[0]["text"]

    # Binding closed and routine disabled so it stops firing.
    assert supervisor_binding.get_binding(binding.binding_id).status == "closed"
    assert RoutineRegistry(bot_id=bot_id).get(binding.routine_id).enabled is False


# ---------------------------------------------------------------------------
# Defect 3: the legacy unscoped path never writes the read-only cwd
# ---------------------------------------------------------------------------


def test_legacy_routine_never_uses_cwd(supervised_env, monkeypatch):
    """A routine with no bot-catalog entry runs the legacy path; its
    project_root must live under VEYA_WRITE_ROOT, never "."."""
    import asyncio

    from server.bot_team.routine_clock import _default_handoff_consumer
    from server.coordinator_master import master_coordinator
    from server.goal_run.models import GoalRunResponse, GoalStatus

    seen: list = []

    async def fake_execute(request):
        seen.append(request)
        return GoalRunResponse(
            goal_id="goal-legacy",
            status=GoalStatus.completed,
            phase="finalized",
            reply="done",
            summary="done",
        )

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)

    handoff = {
        "routine_id": "r-legacy",
        "trigger_topic": "schedule.trigger",
        "trigger_id": "r-legacy:2020",
        "target": {"goal_template": "报一次时间"},
        # Not in the bot catalog and not the default bot → legacy unscoped path.
        "bot_id": "bot_unknown",
        "model": "",
        "workspace_path": "",
    }
    asyncio.run(_default_handoff_consumer([handoff]))

    assert seen, "legacy path did not execute"
    project_root = seen[0].project_root
    assert project_root != "."
    assert str(supervised_env / "work") in project_root
    assert Path(project_root).is_dir()


def test_legacy_project_root_helper_is_under_write_root(supervised_env):
    """The helper resolves to a writable dir under VEYA_WRITE_ROOT."""
    from server.bot_team.routine_clock import _legacy_routine_project_root

    root = _legacy_routine_project_root("bot_unknown")
    assert root != "."
    assert str(supervised_env / "work") in root
    assert Path(root).is_dir()
