"""Q4 execution correctness: executor completion is not acceptance."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from veya.remote import RemoteAuth, RemotePermissions, RemoteSessionManager, RemoteToolAdapter
from veya.remote.execution import ExecutionStore
from veya.remote.mcp_server import create_gateway
from veya.supervision.evidence import build_execution_report
from veya.supervision.models import (
    ExecutionReport,
    Mission,
    MissionStatus,
    ReviewDecision,
    SupervisorReview,
)
from veya.supervision.retask import plan_retask
from veya.supervision.runner import _KNOWN_EXECUTORS, canonical_runner, executor_hint


async def _rpc(gateway, secret: str, session: str, name: str, arguments: dict):
    return await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        authorization=f"Bearer {secret}",
        session_header=session,
    )


async def test_mcp_mission_create_contract_matches_adapter(tmp_path: Path) -> None:
    auth = RemoteAuth()
    _, secret = auth.issue(
        "q4",
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        workspaces=[str(tmp_path)],
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=2),
        adapter=RemoteToolAdapter(None, execution_store=ExecutionStore(None)),
    )
    init = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "q4"}},
        },
        authorization=f"Bearer {secret}",
    )
    session = init["result"]["sessionId"]
    response = await _rpc(
        gateway,
        secret,
        session,
        "veya.mission.create",
        {
            "project_root": str(tmp_path),
            "workspace": str(tmp_path),
            "workspace_path": str(tmp_path),
            "goal": "read-only qualification",
            "executor": "builtin",
        },
    )
    assert response["result"]["structuredContent"]["ok"] is True
    assert "unexpected keyword" not in str(response)


async def test_builtin_cannot_claim_artifact_without_artifact(tmp_path: Path) -> None:
    mission = Mission(mission_id="m", goal="write probe", workspace=str(tmp_path))
    result = await canonical_runner(mission)
    assert result.status == "blocked"
    assert result.unfinished_work


def test_missing_evidence_prevents_accept() -> None:
    mission = Mission(mission_id="m", goal="g")
    report = ExecutionReport(mission_id="m", iteration=0, objective="g", status="completed")
    review = SupervisorReview(
        mission_id="m", iteration=0, supervisor="test", decision=ReviewDecision.done
    )
    outcome = plan_retask(review, mission=mission, report=report)
    assert outcome.mission_status.value == "BLOCKED"
    assert "ACCEPTED state" in outcome.reason


@pytest.mark.parametrize(
    "status", ["blocked", "failed", "cancelled", "partial_completed", "running"]
)
def test_incomplete_execution_status_cannot_be_accepted(status: str) -> None:
    """Evidence from a non-successful iteration must never satisfy ACCEPT.

    Regression: a blocked iteration that produced an execution delta was being
    accepted, because the gate only checked failures/blockers/evidence and never
    the execution outcome itself.
    """
    mission = Mission(mission_id="m", goal="g")
    report = ExecutionReport(
        mission_id="m",
        iteration=0,
        objective="g",
        status=status,
        evidence_chain=[{"category": "runtime", "kind": "execution_delta"}],
    )
    review = SupervisorReview(
        mission_id="m", iteration=0, supervisor="test", decision=ReviewDecision.accept
    )
    outcome = plan_retask(review, mission=mission, report=report)
    assert outcome.mission_status is MissionStatus.blocked
    assert "not an accepted outcome" in outcome.reason


def test_completed_execution_status_is_accepted() -> None:
    """The positive control: a completed iteration with evidence is accepted."""
    mission = Mission(mission_id="m", goal="g")
    report = ExecutionReport(
        mission_id="m",
        iteration=0,
        objective="g",
        status="completed",
        evidence_chain=[{"category": "runtime", "kind": "execution_delta"}],
    )
    review = SupervisorReview(
        mission_id="m", iteration=0, supervisor="test", decision=ReviewDecision.accept
    )
    assert (
        plan_retask(review, mission=mission, report=report).mission_status is MissionStatus.accepted
    )


def test_goal_status_enum_compares_like_its_value() -> None:
    """A GoalStatus member and its bare value must judge acceptance identically.

    Regression: ``GoalStatus`` is a plain Enum, so comparing ``str(member)``
    against a status vocabulary silently rejected genuinely completed runs.
    """
    from server.goal_run.models import GoalStatus

    mission = Mission(mission_id="m", goal="g")
    review = SupervisorReview(
        mission_id="m", iteration=0, supervisor="test", decision=ReviewDecision.accept
    )
    completed = ExecutionReport(
        mission_id="m",
        iteration=0,
        objective="g",
        status=GoalStatus.completed,
        evidence_chain=[{"category": "runtime", "kind": "execution_delta"}],
    )
    assert (
        plan_retask(review, mission=mission, report=completed).mission_status
        is MissionStatus.accepted
    )

    blocked = ExecutionReport(
        mission_id="m",
        iteration=0,
        objective="g",
        status=GoalStatus.blocked,
        evidence_chain=[{"category": "runtime", "kind": "execution_delta"}],
    )
    assert (
        plan_retask(review, mission=mission, report=blocked).mission_status is MissionStatus.blocked
    )


def test_executed_status_is_accepted() -> None:
    """The dispatch path's own success spelling must also be acceptable."""
    mission = Mission(mission_id="m", goal="g")
    report = ExecutionReport(
        mission_id="m",
        iteration=0,
        objective="g",
        status="executed",
        evidence_chain=[{"category": "runtime", "kind": "execution_delta"}],
    )
    review = SupervisorReview(
        mission_id="m", iteration=0, supervisor="test", decision=ReviewDecision.accept
    )
    assert (
        plan_retask(review, mission=mission, report=report).mission_status is MissionStatus.accepted
    )


def test_done_requires_accepted() -> None:
    mission = Mission(mission_id="m", goal="g")
    report = ExecutionReport(
        mission_id="m",
        iteration=0,
        objective="g",
        status="completed",
        evidence_chain=[{"category": "runtime", "kind": "verified"}],
    )
    review = SupervisorReview(
        mission_id="m", iteration=0, supervisor="test", decision=ReviewDecision.done
    )
    blocked = plan_retask(review, mission=mission, report=report)
    assert blocked.mission_status is MissionStatus.blocked
    mission.status = MissionStatus.accepted
    assert plan_retask(review, mission=mission, report=report).mission_status is MissionStatus.done


def test_missing_artifact_fail_closed() -> None:
    mission = Mission(mission_id="m", goal="g")
    report = ExecutionReport(
        mission_id="m",
        iteration=0,
        objective="g",
        status="completed",
        failures=[{"kind": "artifact_missing", "path": "probe.txt"}],
        evidence_chain=[{"category": "failure"}],
    )
    review = SupervisorReview(
        mission_id="m", iteration=0, supervisor="test", decision=ReviewDecision.accept
    )
    outcome = plan_retask(review, mission=mission, report=report)
    assert outcome.mission_status.value == "BLOCKED"


def test_execution_delta_is_acceptance_evidence(tmp_path: Path) -> None:
    state = types.SimpleNamespace(
        goal_id="gr",
        status="completed",
        tasks={},
        final_summary="verified",
        unfinished_work=[],
        baseline_git_state={"status": []},
        execution_delta={"created": ["probe.txt"], "modified": [], "deleted": []},
    )
    report = build_execution_report(
        Mission(mission_id="m", goal="g", workspace=str(tmp_path)),
        state,
        iteration=0,
    )
    delta = next(item for item in report.runtime_evidence if item.get("kind") == "execution_delta")
    assert delta["execution_delta"]["created"] == ["probe.txt"]
    assert report.evidence_chain


def test_execution_delta_does_not_attribute_preexisting_commit_as_delete() -> None:
    before = {
        "files": {
            "existing.py": {
                "tracked": True,
                "worktree_blob": "blob-before",
            }
        },
        "head_blobs": {"existing.py": "blob-old"},
    }
    after = {"files": {}, "head_blobs": {"existing.py": "blob-before"}}
    from server.goal_run.execution_delta import execution_delta

    delta = execution_delta(before, after)
    assert delta["execution_deleted"] == []
    assert delta["preexisting_committed"] == ["existing.py"]


def test_executor_capability_matches_assignment() -> None:
    assert {"builtin", "hicode", "dsh"} == _KNOWN_EXECUTORS
    assert (
        executor_hint(
            types.SimpleNamespace(
                policies=types.SimpleNamespace(execution_policy={"assignee_hint": "native_tool"})
            )
        )
        is None
    )


def test_unsupported_executor_is_rejected() -> None:
    from server.supervision_tools import veya_mission_create

    with pytest.raises(ValueError, match="unsupported mission executor"):
        veya_mission_create("/tmp", "g", executor="native_tool")
