"""P3: external supervisor surface + high-level MCP bindings (same MCP server)."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from veya.supervision import ExternalSupervisor, MissionStore, SupervisionRouter
from veya.supervision.external import MissionNotFound


async def _runner(mission):
    node = types.SimpleNamespace(
        title="do it",
        assignee="hicode",
        status="completed",
        acceptance=["ok"],
        verify_summary="passed",
        artifacts=["out/a.txt"],
        evidence=[{"kind": "command", "exit_code": 0}],
        retries=0,
        block_reason=None,
        unfinished_work=[],
    )
    return types.SimpleNamespace(
        goal_id="gr-1",
        status="completed",
        tasks={"t1": node},
        final_summary="done",
        unfinished_work=[],
    )


def _facade(tmp_path: Path, runner=None) -> ExternalSupervisor:
    store = MissionStore(tmp_path)
    return ExternalSupervisor(store=store, router=SupervisionRouter(store), runner=runner)


async def test_external_run_parks_waiting_for_supervisor(tmp_path: Path) -> None:
    facade = _facade(tmp_path, runner=_runner)
    mission = facade.create(goal="ship", supervision_mode="external", workspace=str(tmp_path))
    out = await facade.run(mission.mission_id)
    assert out["status"] == "WAITING_EXTERNAL_SUPERVISOR"
    assert out["supervisor"] == "external"
    assert out["report"]["artifacts"] == [{"task_id": "t1", "path": "out/a.txt"}]

    inspected = facade.inspect(mission.mission_id)
    assert inspected["latest_report"] is not None
    assert "MISSION_CREATED" in [e["topic"] for e in facade.store.events(mission.mission_id)]


async def test_external_review_apply_retasks_same_mission(tmp_path: Path) -> None:
    facade = _facade(tmp_path, runner=_runner)
    mission = facade.create(goal="ship", supervision_mode="external", workspace=str(tmp_path))
    await facade.run(mission.mission_id)
    out = facade.apply_review(
        mission.mission_id,
        {"decision": "REVISE", "reason": "missing test", "next_task": "add test"},
    )
    assert out["status"] == "RETASKING"
    assert out["next_task"]["objective"] == "add test"
    reloaded = facade.store.load(mission.mission_id)
    assert reloaded is not None and reloaded.mission_id == mission.mission_id


async def test_done_requires_no_unresolved_failures(tmp_path: Path) -> None:
    async def failing_runner(mission):
        bad = types.SimpleNamespace(
            title="x",
            assignee="dsh",
            status="blocked",
            acceptance=[],
            verify_summary=None,
            artifacts=[],
            evidence=[],
            retries=1,
            block_reason="missing binary",
            unfinished_work=[],
        )
        return types.SimpleNamespace(
            goal_id="gr-2",
            status="blocked",
            tasks={"t1": bad},
            final_summary="",
            unfinished_work=[],
        )

    facade = _facade(tmp_path, runner=failing_runner)
    mission = facade.create(goal="ship", supervision_mode="external", workspace=str(tmp_path))
    await facade.run(mission.mission_id)
    out = facade.apply_review(mission.mission_id, {"decision": "DONE"})
    assert out["status"] == "BLOCKED"


async def test_cancel_and_reports_and_escalations(tmp_path: Path) -> None:
    facade = _facade(tmp_path, runner=_runner)
    mission = facade.create(goal="ship", supervision_mode="external", workspace=str(tmp_path))
    await facade.run(mission.mission_id)
    assert facade.latest_report(mission.mission_id) is not None
    assert facade.get_report(mission.mission_id, 0) is not None
    assert facade.list_escalations(mission.mission_id) == []
    assert facade.cancel(mission.mission_id)["status"] == "CANCELLED"


def test_mission_not_found(tmp_path: Path) -> None:
    with pytest.raises(MissionNotFound):
        _facade(tmp_path).inspect("nope")


# ── MCP surface ─────────────────────────────────────────────────────────
def test_high_level_mcp_bindings_share_the_same_server() -> None:
    from veya.remote.tool_adapter import BINDING_INDEX, BINDINGS

    expected = {
        "veya.mission.create": "veya_mission_create",
        "veya.mission.inspect": "veya_mission_inspect",
        "veya.mission.run": "veya_mission_run",
        "veya.mission.continue": "veya_mission_continue",
        "veya.mission.cancel": "veya_mission_cancel",
        "veya.report.latest": "veya_report_latest",
        "veya.report.get": "veya_report_get",
        "veya.review.apply": "veya_review_apply",
        "veya.escalation.list": "veya_escalation_list",
    }
    for mcp_name, canonical in expected.items():
        assert BINDING_INDEX[mcp_name].veya_tool == canonical
    # 17 low-level + 9 supervision, one server
    assert len(BINDINGS) == 26
    assert not any(b.veya_tool is None for b in BINDINGS if not b.name.startswith("process."))


def test_canonical_supervision_tools_register() -> None:
    from server.supervision_tools import _TOOLS

    names = {spec[0] for spec in _TOOLS}
    assert names == {
        "veya_mission_create",
        "veya_mission_inspect",
        "veya_mission_run",
        "veya_mission_continue",
        "veya_mission_cancel",
        "veya_report_latest",
        "veya_report_get",
        "veya_review_apply",
        "veya_escalation_list",
        # P8A additions: thin mode switch + read-only review/event views (HTTP adapter parity)
        "veya_mission_set_mode",
        "veya_reviews",
        "veya_events",
    }
