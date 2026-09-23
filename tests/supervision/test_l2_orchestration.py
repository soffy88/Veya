"""L2 orchestration scheduler tests (deterministic dispatch; real runtime reuse)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from veya.supervision.orchestrated import (
    OrchestrationError,
    Subtask,
    SubtaskResult,
    orchestrated_runner,
    parse_subtasks,
    report_to_state,
    run_plan,
    validate_plan,
)


class Recorder:
    def __init__(self, *, fail: set[str] | None = None, delay: float = 0.2) -> None:
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.started: dict[str, float] = {}
        self.finished: dict[str, float] = {}
        self.fail = fail or set()
        self.delay = delay
        self.counter = 0

    async def __call__(self, subtask: Subtask) -> SubtaskResult:
        loop = asyncio.get_event_loop()
        self.calls.append(subtask.task_id)
        self.started[subtask.task_id] = loop.time()
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        self.finished[subtask.task_id] = loop.time()
        self.counter += 1
        failed = subtask.task_id in self.fail
        return SubtaskResult(
            task_id=subtask.task_id,
            worker=subtask.worker,
            status="FAILED" if failed else "COMPLETED",
            execution_id=f"ex_{subtask.task_id}_{id(self)}_{self.counter}",
            parent_execution_id="parent_1",
            summary=f"{subtask.task_id} done",
            evidence=[
                {"kind": "file_change", "path": f"{subtask.task_id}.txt"},
                {
                    "kind": "artifact_manifest",
                    "file_count": 1,
                    "manifest_hash": f"manifest-{subtask.task_id}",
                },
                {
                    "kind": "artifact",
                    "relative_path": f"{subtask.task_id}.txt",
                    "materialized_path": f"/workspace/.veya/artifacts/{subtask.task_id}.txt",
                    "source_worktree": "/worker",
                    "hash": "abc",
                    "size": 3,
                },
            ],
            error="boom" if failed else None,
            failure_class="TEST_PROVIDER_FAILURE" if failed else None,
            failure_source="deterministic_test" if failed else None,
            failure_message="known child failure" if failed else None,
            failure_detail="known child failure detail" if failed else None,
            provider_error_code="TEST_E001" if failed else None,
            exit_code=17 if failed else None,
            phase="WORKER" if failed else "COMPLETED",
            last_event={"kind": "terminal", "phase": "FAILED"} if failed else None,
        )


def _sub(task_id: str, worker: str = "hicode", deps: tuple[str, ...] = ()) -> Subtask:
    return Subtask(task_id=task_id, objective=f"do {task_id}", worker=worker, depends_on=deps)


async def test_independent_subtasks_run_concurrently() -> None:
    recorder = Recorder()
    plan = [_sub("a", "hicode"), _sub("b", "pi"), _sub("c", "grok")]
    report = await run_plan(
        plan, recorder, mission_id="m1", iteration=1, objective="g", parent_execution_id="parent_1"
    )
    assert report.status == "completed"
    assert recorder.max_active >= 2
    routing = [e for e in report.runtime_evidence if e.get("kind") == "routing_decision"]
    assert len(routing) == 3
    assert {r["selected_worker"] for r in routing} == {"hicode", "pi", "grok"}
    assert all(r["parent_execution_id"] == "parent_1" for r in routing)


async def test_dependent_task_never_early_starts() -> None:
    recorder = Recorder(delay=0.15)
    plan = [
        _sub("a"),
        _sub("b"),
        _sub("c"),
        _sub("d", deps=("a", "b", "c")),
    ]
    report = await run_plan(plan, recorder, mission_id="m1", iteration=1, objective="g")
    assert report.status == "completed"
    assert set(recorder.calls[:3]) == {"a", "b", "c"}
    assert recorder.calls[-1] == "d"
    assert recorder.started["d"] >= max(recorder.finished[t] for t in ("a", "b", "c"))


async def test_dependent_task_receives_materialized_dependency_artifacts() -> None:
    seen: dict[str, Subtask] = {}

    async def dispatch(subtask: Subtask) -> SubtaskResult:
        seen[subtask.task_id] = subtask
        evidence = []
        if subtask.task_id == "a":
            evidence.append(
                {
                    "kind": "artifact",
                    "relative_path": "case_a/input.txt",
                    "materialized_path": "/workspace/.veya/artifacts/ex_a/case_a/input.txt",
                    "hash": "abc",
                    "size": 3,
                }
            )
        return SubtaskResult(
            task_id=subtask.task_id,
            worker=subtask.worker,
            status="COMPLETED",
            execution_id=f"ex_{subtask.task_id}",
            evidence=evidence,
        )

    await run_plan(
        [_sub("a"), _sub("d", deps=("a",))],
        dispatch,
        mission_id="m1",
        iteration=1,
        objective="dependency inputs",
    )
    assert seen["d"].inputs["dependency_artifacts"] == [
        {
            "dependency_subtask_id": "a",
            "execution_id": "ex_a",
            "source_worker": "hicode",
            "source_path": "case_a/input.txt",
            "relative_path": "case_a/input.txt",
            "materialized_path": "/workspace/.veya/artifacts/ex_a/case_a/input.txt",
            "hash": "abc",
            "size": 3,
        }
    ]


async def test_required_artifact_missing_is_completed_but_not_dependency_ready() -> None:
    calls: list[str] = []

    async def dispatch(subtask: Subtask) -> SubtaskResult:
        calls.append(subtask.task_id)
        return SubtaskResult(
            task_id=subtask.task_id,
            worker=subtask.worker,
            status="COMPLETED",
            execution_id=f"ex_{subtask.task_id}",
            evidence=[{"kind": "artifact_manifest", "file_count": 0}],
            artifact_requirement="UNSATISFIED",
            dependency_ready=False,
            required_artifacts=list(subtask.required_artifacts),
            missing_required_artifacts=list(subtask.required_artifacts),
        )

    report = await run_plan(
        [
            Subtask(
                task_id="pi",
                objective="produce the dependency file",
                worker="pi",
                required_artifacts=("case_a/pi.txt",),
            ),
            _sub("d", worker="dsh", deps=("pi",)),
        ],
        dispatch,
        mission_id="m1",
        iteration=1,
        objective="required artifact gate",
    )
    assert calls == ["pi"]
    execution = next(
        item
        for item in report.runtime_evidence
        if item.get("kind") == "l1_execution" and item.get("subtask_id") == "pi"
    )
    assert execution["status"] == "COMPLETED"
    assert execution["artifact_requirement"] == "UNSATISFIED"
    assert execution["dependency_ready"] is False
    blocked = next(item for item in report.blocked_items if item.get("task_id") == "d")
    assert blocked["failure_class"] == "DEPENDENCY_ARTIFACT_STAGING_FAILED"


async def test_required_artifact_is_dependency_ready_when_materialized() -> None:
    seen: list[Subtask] = []

    async def dispatch(subtask: Subtask) -> SubtaskResult:
        seen.append(subtask)
        evidence: list[dict] = []
        if subtask.task_id == "pi":
            evidence = [
                {"kind": "artifact_manifest", "file_count": 1},
                {
                    "kind": "artifact",
                    "relative_path": "case_a/pi.txt",
                    "materialized_path": "/workspace/.veya/artifacts/ex_pi/case_a/pi.txt",
                    "source_worktree": "/worker-pi",
                    "hash": "abc",
                    "size": 3,
                },
            ]
        return SubtaskResult(
            task_id=subtask.task_id,
            worker=subtask.worker,
            status="COMPLETED",
            execution_id=f"ex_{subtask.task_id}",
            evidence=evidence,
            artifact_requirement="SATISFIED" if subtask.task_id == "pi" else "OPTIONAL",
            dependency_ready=True,
            required_artifacts=list(subtask.required_artifacts),
        )

    report = await run_plan(
        [
            Subtask(
                task_id="pi",
                objective="produce the dependency file",
                worker="pi",
                required_artifacts=("case_a/pi.txt",),
            ),
            _sub("d", worker="dsh", deps=("pi",)),
        ],
        dispatch,
        mission_id="m1",
        iteration=1,
        objective="required artifact gate",
    )
    assert [subtask.task_id for subtask in seen] == ["pi", "d"]
    assert report.status == "completed"
    assert seen[-1].inputs["dependency_artifacts"][0]["relative_path"] == "case_a/pi.txt"


async def test_dsh_missing_dependency_artifact_fails_before_dispatch() -> None:
    calls: list[str] = []

    async def dispatch(subtask: Subtask) -> SubtaskResult:
        calls.append(subtask.task_id)
        return SubtaskResult(
            task_id=subtask.task_id,
            worker=subtask.worker,
            status="COMPLETED",
            execution_id=f"ex_{subtask.task_id}",
            evidence=[
                {
                    "kind": "artifact_manifest",
                    "file_count": 0,
                    "manifest_hash": "empty",
                }
            ],
        )

    report = await run_plan(
        [_sub("a"), _sub("b", worker="pi"), _sub("d", worker="dsh", deps=("a", "b"))],
        dispatch,
        mission_id="m1",
        iteration=1,
        objective="fail closed",
    )
    assert calls == ["a", "b"]
    blocked = next(item for item in report.blocked_items if item.get("task_id") == "d")
    assert blocked["failure_class"] == "DEPENDENCY_ARTIFACT_STAGING_FAILED"
    assert "DSH_DISPATCHED=NO" in blocked["failure_detail"]


async def test_dependency_failure_blocks_dependent_and_keeps_siblings() -> None:
    recorder = Recorder(fail={"a"})
    plan = [_sub("a"), _sub("b"), _sub("c"), _sub("d", deps=("a",))]
    report = await run_plan(plan, recorder, mission_id="m1", iteration=1, objective="g")
    assert report.status == "partial"
    assert "d" not in recorder.calls  # dependent never dispatched
    assert {"a", "b", "c"} <= set(recorder.calls)
    assert report.proposed_next_action == "retask"
    # original failed result preserved, siblings preserved
    assert any(f["task_id"] == "a" for f in report.failures)
    blocked = next(b for b in report.blocked_items if b["task_id"] == "d")
    assert blocked["status"] == "BLOCKED_BY_DEPENDENCY"
    assert blocked["blocked_by_subtask"] == "a"
    assert blocked["blocked_by_execution_id"]
    failed = next(f for f in report.failures if f["task_id"] == "a")
    assert failed["failure_class"] == "TEST_PROVIDER_FAILURE"
    assert failed["failure_source"] == "deterministic_test"
    assert failed["failure_detail"] == "known child failure detail"
    assert sum(1 for e in report.runtime_evidence if e.get("kind") == "l1_execution") == 4


async def test_failure_truth_survives_report_jsonl_and_bounded_review(tmp_path) -> None:
    from veya.supervision.evidence import build_execution_report
    from veya.supervision.reviewer import InternalSupervisor
    from veya.supervision.store import MissionStore

    report = await run_plan(
        [_sub("a"), _sub("d", deps=("a",))],
        Recorder(fail={"a"}),
        mission_id="m1",
        iteration=1,
        objective="failure truth",
    )
    mission = _mission("veya_orchestrated", workspace=str(tmp_path))
    state = report_to_state(report)
    canonical = build_execution_report(mission, state, iteration=1)
    store = MissionStore(tmp_path)
    store.save(mission)
    store.append_report(canonical)

    loaded = store.latest_report("m1")
    assert loaded is not None
    encoded = json.dumps(loaded.to_dict(), ensure_ascii=False)
    assert "TEST_PROVIDER_FAILURE" in encoded
    assert "known child failure detail" in encoded
    assert "BLOCKED_BY_DEPENDENCY" in encoded
    assert "blocked_by_execution_id" in encoded

    context = InternalSupervisor().build_review_context(mission, loaded).payload()
    bounded = json.dumps(context, ensure_ascii=False)
    assert "TEST_PROVIDER_FAILURE" in bounded
    assert "known child failure detail" in bounded
    assert "BLOCKED_BY_DEPENDENCY" in bounded
    assert "blocked_by_execution_id" in bounded
    assert "stdout" not in bounded
    assert "stderr" not in bounded


async def test_all_failed_is_not_success() -> None:
    recorder = Recorder(fail={"a", "b"})
    report = await run_plan(
        [_sub("a"), _sub("b")], recorder, mission_id="m1", iteration=1, objective="g"
    )
    assert report.status == "failed"
    assert report.status != "completed"


async def test_cycle_is_rejected() -> None:
    with pytest.raises(OrchestrationError):
        validate_plan([_sub("a", deps=("b",)), _sub("b", deps=("a",))])


async def test_unknown_worker_rejected() -> None:
    with pytest.raises(OrchestrationError):
        validate_plan([_sub("a", worker="unknown_worker")])


async def test_parse_subtasks_rejects_bad_input() -> None:
    parsed = parse_subtasks(
        [{"task_id": "a", "objective": "x", "worker": "hicode", "depends_on": []}]
    )
    assert parsed[0].worker == "hicode"
    declared = parse_subtasks(
        [
            {
                "task_id": "artifact",
                "objective": "produce it",
                "worker": "pi",
                "required_artifacts": ["case_a/result.txt"],
            }
        ]
    )
    assert declared[0].required_artifacts == ("case_a/result.txt",)
    with pytest.raises(OrchestrationError):
        parse_subtasks("not a list")
    with pytest.raises(OrchestrationError):
        parse_subtasks(
            [
                {
                    "task_id": "escape",
                    "objective": "bad",
                    "worker": "pi",
                    "required_artifacts": ["../outside.txt"],
                }
            ]
        )


async def test_retask_gets_new_execution_id_and_original_preserved() -> None:
    first = Recorder(fail={"a"})
    plan = [_sub("a")]
    report_one = await run_plan(plan, first, mission_id="m1", iteration=1, objective="g")
    original_exec = next(
        e["execution_id"] for e in report_one.runtime_evidence if e.get("kind") == "l1_execution"
    )
    second = Recorder()
    report_two = await run_plan(plan, second, mission_id="m1", iteration=2, objective="g")
    retask_exec = next(
        e["execution_id"] for e in report_two.runtime_evidence if e.get("kind") == "l1_execution"
    )
    assert original_exec != retask_exec
    # original report is immutable evidence of the first attempt
    assert report_one.status == "failed"
    assert report_two.status == "completed"


# ── wiring + structured plan validation ────────────────────────────────
def _mission(mode: str, subtasks: list[dict] | None = None, workspace: str = "/tmp/ws"):
    from veya.supervision.models import Mission, MissionPolicies

    policy: dict = {"mode": mode}
    if subtasks is not None:
        policy["subtasks"] = subtasks
    return Mission(
        mission_id="m1",
        goal="ship the feature",
        workspace=workspace,
        policies=MissionPolicies(execution_policy=policy),
        authority={"execution_id": "m1:1", "iteration": 1},
    )


async def test_select_runner_routes_orchestrated_mode() -> None:
    from veya.supervision.runner import select_runner

    recorder = Recorder()
    mission = _mission(
        "veya_orchestrated",
        [
            {"task_id": "a", "objective": "A", "worker": "hicode"},
            {"task_id": "b", "objective": "B", "worker": "pi"},
            {"task_id": "d", "objective": "D", "worker": "dsh", "depends_on": ["a", "b"]},
        ],
    )
    runner = select_runner(orchestrated_dispatch=recorder)
    state = await runner(mission)
    assert recorder.calls == ["a", "b", "d"]
    assert state.status == "executed"
    assert set(state.tasks) == {"a", "b", "d"}


async def test_orchestrated_state_projects_to_canonical_report() -> None:
    from veya.supervision.evidence import build_execution_report

    recorder = Recorder()
    mission = _mission(
        "veya_orchestrated",
        [{"task_id": "a", "objective": "A", "worker": "hicode"}],
    )
    state = await orchestrated_runner(mission, dispatch=recorder)
    report = build_execution_report(mission, state, iteration=1)
    kinds = {e.get("kind") for e in report.runtime_evidence}
    assert "routing_decision" in kinds
    assert report.status in {"executed", "completed"}


async def test_structured_plan_validation_blocks_bad_plans() -> None:
    with pytest.raises(OrchestrationError):
        validate_plan(parse_subtasks([{"task_id": "a", "objective": "A", "worker": "nope"}]))
    with pytest.raises(OrchestrationError):
        validate_plan(
            parse_subtasks(
                [{"task_id": "a", "objective": "A", "worker": "hicode", "depends_on": ["ghost"]}]
            )
        )
    with pytest.raises(OrchestrationError):
        validate_plan(
            [
                _sub("a", deps=("b",)),
                _sub("b", deps=("a",)),
            ]
        )
    with pytest.raises(OrchestrationError):
        validate_plan([_sub("a"), _sub("a")])


# ── L1 bridge (reuses worker.dispatch; no second engine) ───────────────
class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, object]] = []
        self.polls = 0

    async def call(self, session, name, args):
        self.calls.append((name, args, session))
        if name == "worker.dispatch":
            return SimpleNamespace(ok=True, execution_id="parent_1", result={}, message="")
        if name == "process.status":
            self.polls += 1
            if self.polls < 2:
                return SimpleNamespace(ok=True, result={"children": []}, message="")
            return SimpleNamespace(
                ok=True,
                result={
                    "children": [
                        {
                            "execution_id": "ex_1",
                            "worker_type": "PI",
                            "status": "COMPLETED",
                            "result_summary": "ok",
                            "worker_workspace": "/w",
                            "error": None,
                        }
                    ]
                },
                message="",
            )
        return SimpleNamespace(ok=True, result={}, message="")


async def test_l1_bridge_reuses_worker_dispatch_and_records_lineage() -> None:
    from veya.supervision.l1_bridge import L1Bridge

    adapter = FakeAdapter()
    bridge = L1Bridge("/tmp/ws", adapter=adapter, poll_interval_s=0.0, timeout_s=5)
    result = await bridge(Subtask(task_id="t1", objective="do", worker="pi"))
    assert result.status == "COMPLETED"
    assert result.execution_id == "ex_1"
    assert result.parent_execution_id == "parent_1"
    assert result.worker == "PI"
    assert result.evidence[0]["kind"] == "l1_child"
    # it drove the canonical L1 tool, not a worker implementation
    assert adapter.calls[0][0] == "worker.dispatch"
    assert adapter.calls[0][1]["tasks"][0]["worker"] == "pi"
    # canonical internal mission context, not an external MCP session
    session = adapter.calls[0][2]
    assert session.client_info.get("kind") == "internal_mission"
    assert session.token_id == "internal-mission"


# ── planner adapter (canonical planner + availability) ─────────────────
async def test_planner_adapter_reuses_canonical_planner_and_blocks_unavailable() -> None:
    from veya.supervision.planner_adapter import decompose

    mission = _mission("veya_orchestrated")

    async def good_llm(_messages):
        return (
            '{"subtasks":['
            '{"id":"a","goal":"A","worker":"hicode","dependencies":[]},'
            '{"id":"b","goal":"B","worker":"pi","dependencies":[]},'
            '{"id":"c","goal":"C","worker":"grok","dependencies":[]},'
            '{"id":"d","goal":"D","worker":"dsh","dependencies":["a","b","c"]}]}'
        )

    subs = await decompose(
        mission,
        workspace="/tmp/ws",
        available_workers=["hicode", "dsh", "pi", "grok"],
        temporarily_unavailable_workers={"codex": "UPSTREAM_QUOTA"},
        llm=good_llm,
    )
    assert {s.worker for s in subs} == {"hicode", "dsh", "pi", "grok"}
    assert any(s.depends_on for s in subs)

    async def codex_llm(_messages):
        return '{"subtasks":[{"id":"a","goal":"A","worker":"codex","dependencies":[]}]}'

    with pytest.raises(OrchestrationError):
        await decompose(
            mission,
            workspace="/tmp/ws",
            available_workers=["hicode", "dsh", "pi", "grok"],
            temporarily_unavailable_workers={"codex": "UPSTREAM_QUOTA"},
            llm=codex_llm,
        )
