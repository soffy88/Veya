"""Stage C contracts: canonical correction scope, bounded review, and artifacts."""

from __future__ import annotations

import asyncio
import json
import subprocess
import types
from pathlib import Path

from veya.supervision import (
    ExecutionReport,
    InternalSupervisor,
    Mission,
    MissionStatus,
    MissionStore,
    ReviewDecision,
    SupervisionRouter,
    SupervisorReview,
    plan_retask,
)
from veya.supervision.l1_bridge import _artifact_manifest, _git_baseline, _sanitize_objective
from veya.supervision.loop import MissionLoop
from veya.supervision.orchestrated import Subtask


def _state(summary: str = "completed") -> types.SimpleNamespace:
    node = types.SimpleNamespace(
        title="child",
        assignee="hicode",
        status="completed",
        acceptance=["criterion"],
        verify_summary="passed",
        artifacts=[],
        evidence=[{"kind": "key_evidence", "summary": summary}],
        retries=0,
        block_reason=None,
        unfinished_work=[],
    )
    return types.SimpleNamespace(
        goal_id="goal-stage-c",
        status="completed",
        tasks={"child": node},
        final_summary=summary,
        unfinished_work=[],
    )


def test_canonical_review_vocabulary_has_no_replan_decision() -> None:
    assert {str(decision) for decision in ReviewDecision} == {
        "ACCEPT",
        "CONTINUE",
        "REVISE",
        "RETRY",
        "ROLLBACK",
        "ESCALATE",
        "DONE",
    }
    assert "REPLAN" not in {str(decision) for decision in ReviewDecision}


async def test_review_normalizes_lowercase_and_rejects_unknown_decision() -> None:
    report = ExecutionReport(
        mission_id="m-c",
        iteration=0,
        objective="goal",
        status="completed",
    )
    mission = Mission(mission_id="m-c", goal="goal")

    async def lower(_: str) -> str:
        return '{"decision":"revise","correction_scope":"plan","reason":"plan is insufficient"}'

    review = await InternalSupervisor(llm=lower).review(mission, report)
    assert review.decision is ReviewDecision.revise
    assert review.correction_scope == "PLAN"

    async def unknown(_: str) -> str:
        return '{"decision":"REPLAN","reason":"not canonical"}'

    rejected = await InternalSupervisor(llm=unknown).review(mission, report)
    assert rejected.decision is ReviewDecision.escalate


async def test_review_projection_excludes_raw_logs_and_prompts() -> None:
    mission = Mission(mission_id="m-c", goal="goal", acceptance_criteria=["pass"])
    report = ExecutionReport(
        mission_id="m-c",
        iteration=1,
        objective="goal",
        status="completed",
        runtime_evidence=[
            {
                "kind": "key_evidence",
                "summary": "verified",
                "stdout": "RAW_STDOUT_SHOULD_NOT_CROSS",
                "stderr": "RAW_STDERR_SHOULD_NOT_CROSS",
                "worker_prompt": "RAW_PROMPT_SHOULD_NOT_CROSS",
            }
        ],
        artifacts=[{"path": "out/result.txt", "hash": "abc", "size": 3}],
        jev_decisions=[{"answers": {"evidence_sufficient": {"score": 1}}}],
    )
    context = InternalSupervisor().build_review_context(mission, report)
    encoded = json.dumps(context.payload(), ensure_ascii=False)
    assert "RAW_STDOUT_SHOULD_NOT_CROSS" not in encoded
    assert "RAW_STDERR_SHOULD_NOT_CROSS" not in encoded
    assert "RAW_PROMPT_SHOULD_NOT_CROSS" not in encoded
    assert "out/result.txt" in encoded
    assert "jev_decisions" in encoded


async def test_plan_revision_reinvokes_planner_and_preserves_old_plan(tmp_path: Path) -> None:
    planner_calls: list[int] = []
    runner_versions: list[int] = []
    reviewer_prompts: list[str] = []

    async def planner(mission: Mission) -> list[Subtask]:
        planner_calls.append(len(planner_calls) + 1)
        objective = "initial plan" if len(planner_calls) == 1 else "corrected plan"
        return [Subtask(task_id="child", objective=objective, worker="hicode")]

    async def runner(mission: Mission) -> types.SimpleNamespace:
        runner_versions.append(int(mission.authority["plan_version"]))
        return _state()

    responses = [
        '{"decision":"revise","correction_scope":"plan","reason":"missing acceptance path"}',
        '{"decision":"accept","reason":"corrected evidence is sufficient"}',
    ]

    async def reviewer(prompt: str) -> str:
        reviewer_prompts.append(prompt)
        return responses.pop(0)

    class JEV:
        async def decide(self, _state, _questions):
            from veya.decision import JevAnswer, JevDecision, QuestionKind

            return JevDecision(
                answers={
                    "evidence_sufficient": JevAnswer(
                        "evidence_sufficient", QuestionKind.score, score=0.2, confidence=0.9
                    )
                }
            )

    store = MissionStore(tmp_path)
    loop = MissionLoop(
        store=store,
        router=SupervisionRouter(store),
        runner=runner,
        internal=InternalSupervisor(llm=reviewer),
        planner=planner,
        jev=JEV(),
    )
    mission = loop.facade().create(
        goal="correct the plan",
        supervision_mode="internal",
        workspace=str(tmp_path),
    )
    saved = store.load(mission.mission_id)
    assert saved is not None
    saved.policies.execution_policy["mode"] = "veya_orchestrated"
    store.save(saved)

    result = await loop.run_to_completion(mission.mission_id)
    final = store.load(mission.mission_id)
    assert result["status"] == "ACCEPTED"
    assert planner_calls == [1, 2]
    assert runner_versions == [1, 2]
    assert final is not None
    assert final.authority["plan_version"] == 2
    assert final.authority["plan"][0]["objective"] == "corrected plan"
    assert final.authority["plan_history"][0]["version"] == 1
    assert final.authority["plan_history"][0]["plan"][0]["objective"] == "initial plan"
    assert "jev_decisions" in reviewer_prompts[0]
    assert '"plan_version": 1' in reviewer_prompts[0]
    assert all(
        review.decision is not ReviewDecision.revise
        for review in store.reviews(mission.mission_id)[1:]
    )


async def test_accept_persists_consistent_mission_state_for_clean_orchestration_summary(
    tmp_path: Path,
) -> None:
    node = types.SimpleNamespace(
        title="child",
        assignee="dsh",
        status="completed",
        acceptance=[],
        verify_summary="passed",
        artifacts=[],
        evidence=[],
        retries=0,
        block_reason=None,
        unfinished_work=[],
    )

    async def runner(_mission: Mission) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            status="executed",
            tasks={"child": node},
            final_summary="L2 orchestration: 1/1 subtasks completed (0 failed, 0 blocked)",
            unfinished_work=[],
        )

    async def reviewer(_: str) -> str:
        return '{"decision":"ACCEPT","reason":"evidence is sufficient"}'

    store = MissionStore(tmp_path)
    loop = MissionLoop(
        store=store,
        router=SupervisionRouter(store),
        runner=runner,
        internal=InternalSupervisor(llm=reviewer),
    )
    mission = loop.facade().create(
        goal="accept a clean completed execution",
        supervision_mode="internal",
        workspace=str(tmp_path),
    )

    snapshot = await loop.step(mission.mission_id)
    persisted = store.load(mission.mission_id)
    inspected = loop.facade().inspect(mission.mission_id)

    assert snapshot["status"] == "ACCEPTED"
    assert persisted is not None and persisted.status is MissionStatus.accepted
    assert inspected["mission"]["status"] == "ACCEPTED"
    assert inspected["latest_review"]["decision"] == "ACCEPT"
    report = store.latest_report(mission.mission_id)
    assert report is not None and report.failures == [] and report.blocked_items == []


async def test_mission_cancel_stops_future_dispatch_and_cancels_active_runner(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class CancelableRunner:
        def __init__(self) -> None:
            self.calls = 0
            self.cancel_calls = 0

        async def __call__(self, _mission: Mission) -> types.SimpleNamespace:
            self.calls += 1
            started.set()
            await release.wait()
            return _state()

        async def cancel(self, _mission_id: str) -> None:
            self.cancel_calls += 1
            release.set()

    runner = CancelableRunner()
    store = MissionStore(tmp_path)
    loop = MissionLoop(store=store, router=SupervisionRouter(store), runner=runner)
    mission = loop.facade().create(
        goal="cancel active children",
        supervision_mode="internal",
        workspace=str(tmp_path),
    )
    running = asyncio.create_task(loop.step(mission.mission_id))
    await started.wait()
    cancelled = await loop.cancel(mission.mission_id)
    finished = await running
    await loop.cancel(mission.mission_id)

    assert cancelled["status"] == "CANCELLED"
    assert finished["status"] == "CANCELLED"
    assert runner.calls == 1
    assert runner.cancel_calls == 1
    assert store.latest_report(mission.mission_id) is not None
    assert (await loop.step(mission.mission_id))["reason"] == "terminal"


def test_task_correction_keeps_existing_retask_contract() -> None:
    mission = Mission(mission_id="m-c", goal="goal")
    review = SupervisorReview(
        mission_id="m-c",
        iteration=0,
        supervisor="internal",
        decision=ReviewDecision.revise,
        correction_scope="TASK",
        next_task={"worker": "hicode", "objective": "repair child"},  # type: ignore[arg-type]
    )
    outcome = plan_retask(review, mission=mission)
    assert outcome.mission_status is MissionStatus.retasking
    assert outcome.correction_scope == "TASK"
    assert outcome.next_task is not None


async def test_retask_plan_projection_preserves_worker_and_artifacts(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)

    async def runner(_mission: Mission) -> types.SimpleNamespace:
        return _state()

    loop = MissionLoop(store=store, router=SupervisionRouter(store), runner=runner)
    mission = loop.facade().create(
        goal="retask dsh",
        supervision_mode="internal",
        workspace=str(tmp_path),
    )
    mission.policies.execution_policy["mode"] = "veya_orchestrated"
    mission.authority["pending_task_retask"] = {
        "task_id": "dsh-correction",
        "objective": "create the corrected DSH artifact",
        "worker_type": "dsh",
        "required_artifacts": ["case_a/dsh_corrected.txt"],
        "dependency_context": {"source": "canonical-staged-artifacts"},
        "source_review_id": "review-1",
        "original_child_execution_id": "old-dsh",
        "correction_index": 1,
    }
    store.save(mission)

    prepared = await loop._prepare_plan(mission)
    current = store.load(mission.mission_id)

    assert prepared is True
    assert current is not None
    projected = current.policies.execution_policy["subtasks"][0]
    assert projected["worker"] == "dsh"
    assert projected["required_artifacts"] == ["case_a/dsh_corrected.txt"]
    assert projected["inputs"]["dependency_context"] == {"source": "canonical-staged-artifacts"}


async def test_retask_plan_projection_missing_worker_fails_closed(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)

    async def runner(_mission: Mission) -> types.SimpleNamespace:
        return _state()

    loop = MissionLoop(store=store, router=SupervisionRouter(store), runner=runner)
    mission = loop.facade().create(
        goal="retask without worker",
        supervision_mode="internal",
        workspace=str(tmp_path),
    )
    mission.policies.execution_policy["mode"] = "veya_orchestrated"
    mission.authority["pending_task_retask"] = {
        "task_id": "invalid-retask",
        "objective": "must not dispatch",
    }
    store.save(mission)

    prepared = await loop._prepare_plan(mission)
    current = store.load(mission.mission_id)

    assert prepared is False
    assert current is not None and current.status is MissionStatus.blocked
    assert current.authority["retask_block_reason"] == "RETASK_BLOCKED_WORKER_UNRESOLVED"


def test_artifact_manifest_uses_worktree_delta_and_materializes(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "stage-c@test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Stage C"], check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "baseline"], check=True)
    baseline = _git_baseline(str(tmp_path))
    tracked.write_text("after\n", encoding="utf-8")
    untracked = tmp_path / "new.txt"
    untracked.write_text("new\n", encoding="utf-8")

    entries = _artifact_manifest(
        "ex-stage-c",
        "child",
        "HICODE",
        str(tmp_path),
        str(tmp_path / ".veya" / "artifacts"),
        baseline=baseline,
    )
    manifest = entries[0]
    files = {entry["relative_path"]: entry for entry in entries[1:]}
    assert manifest["kind"] == "artifact_manifest"
    assert manifest["baseline_clean"] is True
    assert set(files) == {"new.txt", "tracked.txt"}
    assert files["tracked.txt"]["hash"]
    assert Path(files["new.txt"]["materialized_path"]).read_text() == "new\n"
    assert manifest["manifest_hash"]


def test_objective_path_sanitization_handles_quoted_absolute_path(tmp_path: Path) -> None:
    objective = f'write "{tmp_path}/result.txt" and ../escape'
    sanitized = _sanitize_objective(objective, str(tmp_path))
    assert str(tmp_path) not in sanitized
    assert '"result.txt"' in sanitized
