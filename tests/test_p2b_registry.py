"""P2-B: Skill/Playbook canonical registry — discoverable, versioned, reusable.

Registries describe; they never execute and never accept. Physical work always
funnels through CanonicalActionRequest → GoalRun → ActionGateway inside the
SAME GoalRun. Discovery is contract-structural only: no keyword hard routing
exists anywhere on this path.
"""

from __future__ import annotations

import pytest

from runtime.execution.durable import (
    DurableExecutionRepository,
    WorkItemSpec,
    build_operation_key,
)
from runtime.execution.models import (
    PLAYBOOK_ACCEPTANCE_AUTHORITY,
    PLAYBOOK_EXECUTION_AUTHORITY,
    SECOND_ACCEPTANCE_AUTHORITY,
    SECOND_EXECUTION_AUTHORITY,
    SKILL_ACCEPTANCE_AUTHORITY,
    SKILL_EXECUTION_AUTHORITY,
    WORKER_SELF_REPORT_AUTHORITY,
    DelegateResult,
    assert_playbook_execution_authority_zero,
    assert_skill_acceptance_authority_zero,
    assert_skill_execution_authority_zero,
    reconcile_delegate_results,
)
from runtime.execution.side_effects import SideEffectLedger
from server.capability_model import (
    PlaybookRegistry,
    PlaybookSpec,
    PlaybookStep,
    SkillRegistry,
    SkillSpec,
    _JsonRegistryStore,
    validate_playbook_steps,
    validate_skill_input,
)
from server.goal_run.action_protocol import CanonicalActionRequest
from server.goal_run.canonical_worker import CanonicalWorkerAdapter, MasterAgentActionAdapter
from server.goal_run.models import GoalRunState, PlaybookState
from server.goal_run.store import load_goal_run, save_goal_run


def _registries(tmp_path):
    store = _JsonRegistryStore(tmp_path / "registry.json")
    skills = SkillRegistry(store)
    playbooks = PlaybookRegistry(store, skills=skills)
    return skills, playbooks


def _seed_skill(
    skills: SkillRegistry,
    skill_id: str = "excel.parse",
    version: int = 2,
    status: str = "verified",
    capabilities: list | None = None,
    required_tools: list | None = None,
    preconditions: list | None = None,
) -> SkillSpec:
    spec = SkillSpec(
        skill_id=skill_id,
        instructions="parse spreadsheet workbooks",
        version=version,
        capabilities=list(capabilities) if capabilities is not None else ["research"],
        required_tools=list(required_tools) if required_tools is not None else ["read_file"],
        input_schema={
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
        output_schema={"type": "object"},
        preconditions=list(preconditions) if preconditions is not None else ["workspaceMounted"],
        evidence_requirements=["parsed-row-count"],
        failure_contract={"retry": "once", "on_unknown": "block"},
        status=status,
    )
    skills.register_candidate(spec)
    if status == "verified":
        stored = skills.get_version(skill_id)
        assert stored is not None
        stored.status = "verified"
        skills._store.put("skill", skill_id, stored.__dict__ | {"status": "verified"})
        # Re-read through the public surface to keep the test honest.
        stored = skills.get_version(skill_id)
        assert stored is not None and stored.status == "verified"
    return skills.get_version(skill_id)


def test_skill_register_get_list(tmp_path):
    skills, _ = _registries(tmp_path)
    _seed_skill(skills, "excel.parse", version=2)
    _seed_skill(skills, "mail.send", version=1)
    assert skills.get("excel.parse", version=2) is not None
    assert skills.get("excel.parse", version=999) is None
    assert skills.get("missing") is None
    assert {s.skill_id for s in skills.list()} == {"excel.parse", "mail.send"}


def test_skill_schema_validation(tmp_path):
    skills, _ = _registries(tmp_path)
    spec = _seed_skill(skills)
    assert validate_skill_input(spec, {"path": "a.xlsx"}) == []
    assert validate_skill_input(spec, {}) == ["missing required field: path"]
    assert validate_skill_input(spec, {"path": 42}) == ["field 'path' must be string"]
    assert validate_skill_input(SkillSpec(skill_id="x", instructions="y"), "nope") == []


def test_playbook_register_get_list(tmp_path):
    skills, playbooks = _registries(tmp_path)
    _seed_skill(skills)
    spec = PlaybookSpec(
        playbook_id="publish.report",
        version=3,
        capabilities=["research"],
        required_tools=["read_file"],
        preconditions=["workspaceMounted"],
        evidence_requirements=["report-sha"],
        steps=[
            PlaybookStep(step_id="parse", skill_id="excel.parse"),
            PlaybookStep(step_id="draft", depends_on=["parse"]),
        ],
    )
    playbooks.register_candidate(spec)
    assert playbooks.get("publish.report", version=3) is not None
    assert playbooks.get("publish.report", version=999) is None
    assert [p.playbook_id for p in playbooks.list()] == ["publish.report"]
    assert validate_playbook_steps([PlaybookStep(step_id="a"), PlaybookStep(step_id="a")]) == [
        "duplicate step_id: a"
    ]
    with pytest.raises(ValueError, match="unknown skill"):
        playbooks.register_candidate(
            PlaybookSpec(playbook_id="bad", steps=[PlaybookStep(step_id="a", skill_id="nope")])
        )


def test_master_agent_skill_discovery(tmp_path):
    """Model-side selection via structured requirements — no keywords."""
    skills, _ = _registries(tmp_path)
    _seed_skill(skills, "excel.parse")
    _seed_skill(
        skills,
        "mail.send",
        capabilities=["comms"],
        required_tools=["smtp"],
        preconditions=["network"],
    )
    # Structured requirement match finds the right skill.
    found = skills.query(
        capabilities=["research"],
        tools=["read_file"],
        preconditions=["workspaceMounted"],
        include_candidates=True,
    )
    assert [s.skill_id for s in found] == ["excel.parse"]
    # Free-text keywords never match: there is no keyword routing to lean on.
    assert skills.query(capabilities=["parse spreadsheet"], include_candidates=True) == []
    assert skills.query(capabilities=["excel"], include_candidates=True) == []
    # Unsatisfied preconditions exclude; deprecated never match.
    assert (
        skills.query(capabilities=["research"], tools=["read_file"], include_candidates=True) == []
    )
    skills.rollback("excel.parse")
    assert (
        skills.query(
            capabilities=["research"],
            tools=["read_file"],
            preconditions=["workspaceMounted"],
            include_candidates=True,
        )
        == []
    )


def test_master_agent_playbook_discovery(tmp_path):
    skills, playbooks = _registries(tmp_path)
    _seed_skill(skills)
    playbooks.register_candidate(
        PlaybookSpec(
            playbook_id="publish.report",
            capabilities=["research"],
            required_tools=["read_file"],
            preconditions=["workspaceMounted"],
            steps=[PlaybookStep(step_id="parse", skill_id="excel.parse")],
        )
    )
    found = playbooks.query(
        capabilities=["research"],
        tools=["read_file"],
        preconditions=["workspaceMounted"],
        include_candidates=True,
    )
    assert [p.playbook_id for p in found] == ["publish.report"]
    assert playbooks.query(capabilities=["publish report"], include_candidates=True) == []


async def _execute_skill_step(state, worker, tool: str, arguments: dict, calls: list):
    async def gateway(request: CanonicalActionRequest) -> dict:
        calls.append((request.tool, request.goal_run_id))
        assert request.goal_run_id == state.goal_id
        return {"status": "completed", "executed": True, "result": f"{tool}-ok"}

    master = MasterAgentActionAdapter(
        goal_run_id=state.goal_id,
        task_id="task-p2b",
        computer_ref=worker.computer_id,
        approval={"side_effect": "pure_read"},
        executor=lambda request: worker.execute_canonical_action(
            state, request, gateway_executor=gateway
        ),
    )
    return await master.execute(tool, arguments)


async def test_skill_canonical_execution(tmp_path):
    skills, _ = _registries(tmp_path)
    spec = _seed_skill(skills)
    # Model selects from discovery, validates args, then executes canonically.
    selected = skills.query(
        capabilities=["research"],
        tools=["read_file"],
        preconditions=["workspaceMounted"],
        include_candidates=True,
    )[0]
    assert selected.skill_id == spec.skill_id
    assert validate_skill_input(selected, {"path": "a.xlsx"}) == []

    state = GoalRunState(goal_id="goal-skill", goal_text="run selected skill")
    state.active_skill_id = selected.skill_id
    state.active_skill_version = selected.version
    worker = CanonicalWorkerAdapter(task_id="task-p2b", objective=state.goal_text)
    await worker.before_execution(state, str(tmp_path))
    calls: list = []
    result = await _execute_skill_step(state, worker, "read_file", {"path": "a.xlsx"}, calls)
    assert result.status == "completed"
    assert calls == [("read_file", "goal-skill")]
    assert_skill_execution_authority_zero()
    assert_skill_acceptance_authority_zero()
    assert SKILL_EXECUTION_AUTHORITY == 0
    assert SKILL_ACCEPTANCE_AUTHORITY == 0


async def test_playbook_canonical_execution(tmp_path):
    skills, playbooks = _registries(tmp_path)
    _seed_skill(skills)
    playbooks.register_candidate(
        PlaybookSpec(
            playbook_id="publish.report",
            version=3,
            steps=[
                PlaybookStep(step_id="parse", skill_id="excel.parse"),
                PlaybookStep(step_id="draft", depends_on=["parse"]),
            ],
        )
    )
    selected = playbooks.query(include_candidates=True)[0]
    assert_playbook_execution_authority_zero()
    assert PLAYBOOK_EXECUTION_AUTHORITY == 0

    state = GoalRunState(goal_id="goal-pb", goal_text="run playbook canonically")
    state.playbook_id = selected.playbook_id
    state.active_playbook_version = selected.version
    state.playbook_states[selected.playbook_id] = PlaybookState(
        playbook_id=selected.playbook_id, version=selected.version
    )
    worker = CanonicalWorkerAdapter(task_id="task-p2b", objective=state.goal_text)
    await worker.before_execution(state, str(tmp_path))
    calls: list = []
    for step in selected.steps:
        state.current_playbook_step = step.step_id
        result = await _execute_skill_step(state, worker, "read_file", {"path": "a.xlsx"}, calls)
        assert result.status == "completed"
        projection = state.playbook_states[selected.playbook_id]
        projection.completed_steps.append(step.step_id)
        projection.active_step = None
    assert [c[1] for c in calls] == ["goal-pb", "goal-pb"]
    assert state.playbook_states["publish.report"].completed_steps == ["parse", "draft"]


async def test_same_goalrun(tmp_path):
    state = GoalRunState(goal_id="goal-same", goal_text="same run")
    worker = CanonicalWorkerAdapter(task_id="task-p2b", objective=state.goal_text)
    await worker.before_execution(state, str(tmp_path))
    calls: list = []
    with pytest.raises(ValueError, match="different GoalRun"):
        await worker.execute_canonical_action(
            state,
            CanonicalActionRequest(
                action_id="a-x",
                goal_run_id="goal-other",
                task_id="task-p2b",
                tool="read_file",
            ),
            gateway_executor=lambda request: calls.append(request),
        )
    assert calls == []


def test_skill_version_preserved(tmp_path):
    skills, _ = _registries(tmp_path)
    _seed_skill(skills, version=2)
    state = GoalRunState(goal_id="goal-ver", goal_text="pin versions")
    state.active_skill_id = "excel.parse"
    state.active_skill_version = 2
    save_goal_run(state, str(tmp_path))
    restored = load_goal_run(str(tmp_path), "goal-ver")
    assert restored is not None
    assert restored.active_skill_id == "excel.parse"
    assert restored.active_skill_version == 2
    assert skills.get("excel.parse", version=2) is not None


def test_playbook_version_preserved_and_restart_resume(tmp_path):
    skills, playbooks = _registries(tmp_path)
    _seed_skill(skills)
    playbooks.register_candidate(
        PlaybookSpec(
            playbook_id="publish.report",
            version=3,
            steps=[PlaybookStep(step_id="s1"), PlaybookStep(step_id="s2")],
        )
    )
    state = GoalRunState(goal_id="goal-resume", goal_text="resume same version+step")
    state.playbook_id = "publish.report"
    state.active_playbook_version = 3
    state.current_playbook_step = "s2"
    state.playbook_states["publish.report"] = PlaybookState(
        playbook_id="publish.report", version=3, active_step="s2", completed_steps=["s1"]
    )
    save_goal_run(state, str(tmp_path))
    resumed = load_goal_run(str(tmp_path), "goal-resume")
    assert resumed is not None
    assert resumed.active_playbook_version == 3
    assert resumed.current_playbook_step == "s2"
    assert resumed.playbook_states["publish.report"].version == 3
    assert resumed.playbook_states["publish.report"].completed_steps == ["s1"]
    spec = playbooks.get("publish.report", version=resumed.active_playbook_version)
    assert spec is not None and spec.version == 3
    resumed.playbook_states["publish.report"].completed_steps.append("s2")
    resumed.playbook_states["publish.report"].active_step = None
    save_goal_run(resumed, str(tmp_path))
    final = load_goal_run(str(tmp_path), "goal-resume")
    assert final is not None
    assert final.playbook_states["publish.report"].completed_steps == ["s1", "s2"]


async def test_no_duplicate_side_effects(tmp_path):
    repo = DurableExecutionRepository(sqlite_path=tmp_path / "ledger.sqlite3")
    await repo.connect()
    try:
        await repo.create_goal_run(goal_run_id="run-dedup", idempotency_key="run-dedup")
        item = await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id="run-dedup", logical_key="publish", kind="tool")
        )
        operation_key = build_operation_key("run-dedup", item["id"], "publish")
        calls = 0

        async def provider():
            nonlocal calls
            calls += 1
            return "published"

        ledger = SideEffectLedger(repo)
        for _ in range(2):
            result = await ledger.execute(
                goal_run_id="run-dedup",
                work_item_id=item["id"],
                operation_key=operation_key,
                operation_type="publish",
                target_ref="provider:item",
                request={"value": 1},
                provider=provider,
            )
            assert result == "published"
        assert calls == 1
    finally:
        await repo.close()


def test_worker_self_report_authority_zero():
    assert WORKER_SELF_REPORT_AUTHORITY == 0
    assert SECOND_EXECUTION_AUTHORITY == 0
    assert SECOND_ACCEPTANCE_AUTHORITY == 0
    assert PLAYBOOK_ACCEPTANCE_AUTHORITY == 0
    real = DelegateResult(
        delegate_id="ok", status="complete", stop_reason="completed", summary="ok"
    )
    claimed = DelegateResult(
        delegate_id="self", status="complete", stop_reason="completed", summary="self"
    )
    claimed.self_report = True  # type: ignore[attr-defined]
    reconciled = reconcile_delegate_results([claimed])
    assert reconciled["overall_status"] == "blocked"
    assert reconcile_delegate_results([claimed, real])["overall_status"] == "success"
