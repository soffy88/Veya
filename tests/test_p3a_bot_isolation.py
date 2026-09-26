"""P3-A multi-bot isolation: one user, several persistent bots, strict isolation.

Every durable object carries the owning ``bot_id``; any cross-bot read /
resume / execute / reuse is refused fail-closed. Bots own no authority::

    MasterAgent = semantic, GoalRun = execution, ComputerSupervisor = physical,
    SideEffectLedger = side effect, IndependentVerifier = acceptance.

Covers: MULTI_BOT_CREATE, BOT_ID_PROPAGATION, *_BOUND, CROSS_BOT_*=DENIED,
BOT_A_RESTART_RESUME, BOT_B_UNAFFECTED, authorities stay 0, no duplicate
side effects. No bot-to-bot delegation, no swarm, no marketplace, no
billing, no UI.
"""

from __future__ import annotations

import pytest

from runtime.bot_scope import CrossBotAccessDenied
from runtime.computer.models import CheckpointRef
from runtime.computer.store import PersistentComputerStore
from runtime.context.engine import ContextEngine
from runtime.context.models import ContextLayer
from runtime.execution.delegate_runtime import DelegateRuntime
from runtime.execution.durable import (
    DurableExecutionError,
    DurableExecutionRepository,
    WorkItemSpec,
    build_operation_key,
)
from runtime.execution.fanin import fan_in
from runtime.execution.models import (
    SECOND_ACCEPTANCE_AUTHORITY,
    SECOND_EXECUTION_AUTHORITY,
    DelegateRequest,
    DelegateResult,
    Evidence,
    reconcile_delegate_results,
)
from runtime.execution.side_effects import SideEffectLedger
from runtime.execution.spawn_guard import SpawnBudget, SpawnGuard
from runtime.verification.models import EvidenceBundle, EvidenceItem
from server.capability_model import (
    PlaybookRegistry,
    PlaybookSpec,
    PlaybookStep,
    SkillRegistry,
    SkillSpec,
    _JsonRegistryStore,
)
from server.goal_run.bot_identity import (
    BotRegistry,
    BotSpec,
    assert_bot_may_use_playbook,
    assert_bot_may_use_skill,
    bot_may_use_playbook,
    bot_may_use_skill,
)
from server.goal_run.canonical_worker import (
    CanonicalWorkerAdapter,
    MasterAgentActionAdapter,
)
from server.goal_run.models import (
    DelegateState,
    FanInState,
    GoalRunState,
    PlaybookState,
    RoutineSpec,
)
from server.goal_run.routine_dispatch import dispatch_trigger
from server.goal_run.routine_registry import RoutineRegistry
from server.goal_run.store import load_goal_run, save_goal_run

BOT_A = "bot-a"
BOT_B = "bot-b"
OWNER = "user-1"


def _bots(tmp_path) -> tuple[BotRegistry, BotSpec, BotSpec]:
    registry = BotRegistry(tmp_path / "bots.json")
    bot_a = BotSpec(
        bot_id=BOT_A,
        owner_id=OWNER,
        config_version=2,
        available_skill_ids=["brief.render"],
        available_playbook_ids=["publish.report"],
    )
    bot_b = BotSpec(
        bot_id=BOT_B,
        owner_id=OWNER,
        config_version=2,
        available_skill_ids=["other.skill"],
        available_playbook_ids=["other.playbook"],
    )
    registry.register(bot_a)
    registry.register(bot_b)
    return registry, bot_a, bot_b


def _routine_registries(tmp_path):
    reg_a = RoutineRegistry(tmp_path / "routines-a.json", bot_id=BOT_A)
    reg_a.register(
        RoutineSpec(
            routine_id="morning.brief",
            trigger_topic="schedule.trigger",
            objective="deliver morning brief",
            version=2,
            target_skill_id="brief.render",
            bot_id=BOT_A,
        )
    )
    reg_b = RoutineRegistry(tmp_path / "routines-b.json", bot_id=BOT_B)
    reg_b.register(
        RoutineSpec(
            routine_id="evening.brief",
            trigger_topic="schedule.trigger",
            objective="deliver evening brief",
            target_skill_id="other.skill",
            bot_id=BOT_B,
        )
    )
    return reg_a, reg_b


def test_multi_bot_create(tmp_path):
    registry, bot_a, bot_b = _bots(tmp_path)
    assert registry.get(BOT_A) is not None
    assert registry.get(BOT_B) is not None
    assert {b.bot_id for b in registry.list()} == {BOT_A, BOT_B}
    assert {b.bot_id for b in registry.list(owner_id=OWNER)} == {BOT_A, BOT_B}
    assert registry.list(owner_id="nobody") == []
    # Identity: owner, version, namespaces, refs are all distinct per bot.
    assert bot_a.user_id == OWNER == bot_b.user_id
    assert bot_a.config_version == 2
    assert bot_a.knowledge_namespace != bot_b.knowledge_namespace
    assert bot_a.context_namespace != bot_b.context_namespace
    assert bot_a.routine_registry_ref != bot_b.routine_registry_ref
    assert bot_a.knowledge_namespace == f"knowledge:{BOT_A}"
    assert bot_a.context_namespace == f"context:{BOT_A}"
    # Reloaded from disk keeps every field.
    reloaded = BotRegistry(tmp_path / "bots.json")
    assert reloaded.get(BOT_A) is not None
    assert reloaded.get(BOT_A).available_skill_ids == ["brief.render"]
    assert reloaded.get(BOT_A).available_playbook_ids == ["publish.report"]
    with pytest.raises(ValueError, match="bot_id"):
        registry.register(BotSpec(bot_id="", owner_id=OWNER))


def test_skill_playbook_availability(tmp_path):
    _, bot_a, bot_b = _bots(tmp_path)
    assert bot_may_use_skill(bot_a, "brief.render") is True
    assert bot_may_use_skill(bot_a, "other.skill") is False
    assert bot_may_use_playbook(bot_a, "publish.report") is True
    assert bot_may_use_playbook(bot_b, "publish.report") is False
    assert_bot_may_use_skill(bot_a, "brief.render")  # positive case runs clean
    with pytest.raises(CrossBotAccessDenied):
        assert_bot_may_use_skill(bot_a, "other.skill")
    with pytest.raises(CrossBotAccessDenied):
        assert_bot_may_use_playbook(bot_b, "publish.report")


def test_goalrun_bot_bound(tmp_path):
    state = GoalRunState(goal_id="goal-a", goal_text="owned", bot_id=BOT_A)
    assert state.bot_id == BOT_A
    save_goal_run(state, str(tmp_path))
    resumed = load_goal_run(str(tmp_path), "goal-a")
    assert resumed is not None and resumed.bot_id == BOT_A
    # Positive: the owning bot resumes clean.
    assert load_goal_run(str(tmp_path), "goal-a", bot_id=BOT_A) is not None


def test_cross_bot_goalrun_resume_denied(tmp_path):
    save_goal_run(GoalRunState(goal_id="goal-a", goal_text="owned", bot_id=BOT_A), str(tmp_path))
    with pytest.raises(CrossBotAccessDenied):
        load_goal_run(str(tmp_path), "goal-a", bot_id=BOT_B)


def test_computer_bot_bound(tmp_path):
    store = PersistentComputerStore(tmp_path / "computers.sqlite3")
    computer = store.create_computer(
        owner_id="task-a", workspace_ref=str(tmp_path), computer_id="comp-a", bot_id=BOT_A
    )
    assert computer.bot_id == BOT_A
    assert store.get_computer("comp-a") is not None
    assert store.get_computer("comp-a").bot_id == BOT_A
    # Positive: the owning bot reads clean.
    assert store.get_computer("comp-a", bot_id=BOT_A) is not None
    assert store.list_computers(bot_id=BOT_A)[0].computer_id == "comp-a"
    assert store.list_computers(bot_id=BOT_B) == []
    checkpoint = CheckpointRef(
        checkpoint_id="ckpt-1",
        computer_id="comp-a",
        goal_run_id="goal-a",
        path=str(tmp_path / "ckpt"),
        sha256="sha:ckpt",
        size_bytes=8,
        bot_id=BOT_A,
    )
    assert store.set_checkpoint("comp-a", checkpoint) is not None
    assert store.get_checkpoint("comp-a", bot_id=BOT_A) is not None


def test_cross_bot_computer_resume_denied(tmp_path):
    store = PersistentComputerStore(tmp_path / "computers.sqlite3")
    store.create_computer(
        owner_id="task-a", workspace_ref=str(tmp_path), computer_id="comp-a", bot_id=BOT_A
    )
    store.link_goal_run("goal-a", "comp-a", "task-a", bot_id=BOT_A)
    with pytest.raises(CrossBotAccessDenied):
        store.get_computer("comp-a", bot_id=BOT_B)
    with pytest.raises(CrossBotAccessDenied):
        store.get_computer_for_goal_run("goal-a", bot_id=BOT_B)
    with pytest.raises(CrossBotAccessDenied):
        store.get_checkpoint("comp-a", bot_id=BOT_B)
    # Reusing the same computer id under another bot is refused, not aliased.
    with pytest.raises(CrossBotAccessDenied):
        store.create_computer(
            owner_id="task-a", workspace_ref=str(tmp_path), computer_id="comp-a", bot_id=BOT_B
        )
    # Linking across bots is refused without a partial link.
    assert store.link_goal_run("goal-a", "comp-a", "task-a", bot_id=BOT_B) is False
    # A foreign checkpoint is never attached.
    foreign = CheckpointRef(
        checkpoint_id="ckpt-x",
        computer_id="comp-a",
        goal_run_id="goal-x",
        path=str(tmp_path / "ckpt-x"),
        sha256="sha:x",
        size_bytes=1,
        bot_id=BOT_B,
    )
    with pytest.raises(CrossBotAccessDenied):
        store.set_checkpoint("comp-a", foreign)


def test_context_bot_bound(tmp_path):
    engine = ContextEngine(goal_run_id="goal-a", computer_id="comp-a", bot_id=BOT_A)
    assert engine.bot_id == BOT_A
    assert engine.state.bot_id == BOT_A
    assert engine.state.preserved.bot_id == BOT_A
    engine.append_to_layer(ContextLayer.L2_OBSERVATIONS, [{"note": "a"}], token_estimate=4)
    checkpoint = tmp_path / "context.json"
    engine.save_checkpoint(checkpoint)
    # Positive: the owning bot reloads clean.
    resumed = ContextEngine.load_checkpoint(checkpoint, bot_id=BOT_A)
    assert resumed.state.bot_id == BOT_A
    assert resumed.state.preserved.bot_id == BOT_A
    assert resumed.bot_id == BOT_A


def test_cross_bot_context_and_checkpoint_access_denied(tmp_path):
    engine = ContextEngine(goal_run_id="goal-a", computer_id="comp-a", bot_id=BOT_A)
    checkpoint = tmp_path / "context.json"
    engine.save_checkpoint(checkpoint)
    with pytest.raises(CrossBotAccessDenied):
        ContextEngine.load_checkpoint(checkpoint, bot_id=BOT_B)


def test_routine_bot_bound(tmp_path):
    reg_a, _ = _routine_registries(tmp_path)
    state = GoalRunState(goal_id="goal-a", goal_text="owned", bot_id=BOT_A)
    (handoff,) = dispatch_trigger(
        state, reg_a.lookup("schedule.trigger"), topic="schedule.trigger", trigger_id="t1"
    )
    assert handoff["goal_run_id"] == "goal-a"
    assert state.routine_states["morning.brief"].bot_id == BOT_A
    assert state.routine_states["morning.brief"].started_count == 1
    # A foreign routine never enters this bot's catalog.
    with pytest.raises(CrossBotAccessDenied):
        reg_a.register(
            RoutineSpec(
                routine_id="intruder",
                trigger_topic="schedule.trigger",
                objective="intrude",
                target_skill_id="other.skill",
                bot_id=BOT_B,
            )
        )
    # Positive: the owning bot's lookup serves its own routine.
    assert [r.routine_id for r in reg_a.lookup("schedule.trigger", bot_id=BOT_A)] == [
        "morning.brief"
    ]


def test_cross_bot_routine_dispatch_denied(tmp_path):
    reg_a, reg_b = _routine_registries(tmp_path)
    state_b = GoalRunState(goal_id="goal-b", goal_text="owned", bot_id=BOT_B)
    with pytest.raises(CrossBotAccessDenied):
        dispatch_trigger(
            state_b,
            reg_a.lookup("schedule.trigger"),
            topic="schedule.trigger",
            trigger_id="t9",
        )
    with pytest.raises(CrossBotAccessDenied):
        reg_a.lookup("schedule.trigger", bot_id=BOT_B)
    # Positive: bot B dispatches its own routine clean.
    (handoff,) = dispatch_trigger(
        state_b, reg_b.lookup("schedule.trigger"), topic="schedule.trigger", trigger_id="t1"
    )
    assert handoff["routine_id"] == "evening.brief"


async def test_delegate_bot_bound(tmp_path):
    runtime = DelegateRuntime(SpawnGuard(SpawnBudget()), goal_run_id="goal-a", bot_id=BOT_A)

    async def operation(_cancel):
        return DelegateResult(
            delegate_id="d1",
            status="complete",
            stop_reason="completed",
            summary="ok",
            evidence=[
                Evidence(
                    id="e-d1",
                    kind="note",
                    source="worker",
                    content="evidence-d1",
                    producer="worker",
                    sha256="sha:d1",
                )
            ],
        )

    result = await runtime.run(
        DelegateRequest(
            delegate_id="d1",
            parent_task_id="goal-a",
            parent_trace_id="goal-a",
            objective="parse",
            bot_id=BOT_A,
        ),
        operation,
    )
    assert result.status == "complete"
    batch = fan_in([result])
    assert batch.complete_count == 1
    assert reconcile_delegate_results([result])["overall_status"] == "success"
    projection = DelegateState.from_dict(
        {
            "delegate_id": "d1",
            "parent_goal_run_id": "goal-a",
            "status": "complete",
            "evidence_refs": ["sha:d1"],
            "bot_id": BOT_A,
        }
    )
    assert projection.bot_id == BOT_A
    fanin = FanInState.from_dict(
        {
            "fanin_id": "f1",
            "expected_delegate_ids": ["d1"],
            "completed_delegate_ids": ["d1"],
            "reconciled_result_ref": "sha:r1",
            "bot_id": BOT_A,
        }
    )
    assert fanin.bot_id == BOT_A


async def test_cross_bot_delegate_denied():
    runtime = DelegateRuntime(SpawnGuard(SpawnBudget()), goal_run_id="goal-a", bot_id=BOT_A)

    async def operation(_cancel):
        return DelegateResult(
            delegate_id="dx", status="complete", stop_reason="completed", summary="x"
        )

    with pytest.raises(CrossBotAccessDenied):
        await runtime.run(
            DelegateRequest(
                delegate_id="dx",
                parent_task_id="goal-a",
                parent_trace_id="goal-a",
                objective="cross",
                bot_id=BOT_B,
            ),
            operation,
        )


def test_evidence_bot_bound():
    bundle = EvidenceBundle(task_id="task-a", goal_run_id="goal-a", head_sha="sha:a", bot_id=BOT_A)
    assert bundle.bot_id == BOT_A
    assert bundle.is_bound_to_bot(BOT_A) is True
    assert bundle.is_bound_to_bot(BOT_B) is False
    assert bundle.verify_integrity() is True
    # Claiming unattributed evidence re-hashes; integrity still holds.
    plain = EvidenceBundle(task_id="task-a", goal_run_id="goal-a", head_sha="sha:a")
    claimed = plain.scoped_to(BOT_A)
    assert claimed.bot_id == BOT_A
    assert claimed.verify_integrity() is True
    grown = claimed.add_evidence(
        EvidenceItem(
            id="ev-1",
            kind="artifact",
            source="seam",
            content="content",
            producer="p3a",
        )
    )
    assert grown.bot_id == BOT_A
    assert grown.verify_integrity() is True


def test_cross_bot_evidence_denied():
    bundle = EvidenceBundle(task_id="task-b", goal_run_id="goal-b", head_sha="sha:b", bot_id=BOT_B)
    with pytest.raises(CrossBotAccessDenied):
        bundle.scoped_to(BOT_A)


async def test_cross_bot_side_effect_reuse_denied(tmp_path):
    repo = DurableExecutionRepository(sqlite_path=tmp_path / "ledger.sqlite3")
    await repo.connect()
    try:
        await repo.create_goal_run(goal_run_id="run-a", idempotency_key="run-a")
        item = await repo.enqueue_work_item(
            WorkItemSpec(goal_run_id="run-a", logical_key="publish", kind="tool")
        )
        operation_key = build_operation_key("run-a", item["id"], "publish")
        provider_calls = 0

        async def provider():
            nonlocal provider_calls
            provider_calls += 1
            return "published"

        ledger = SideEffectLedger(repo)
        # Bot A commits under its scope.
        assert (
            await ledger.execute(
                goal_run_id="run-a",
                work_item_id=item["id"],
                operation_key=operation_key,
                operation_type="publish",
                target_ref="provider:item",
                request={"value": 1},
                provider=provider,
                bot_id=BOT_A,
            )
            == "published"
        )
        # Same bot + same key replays without a second execution.
        assert (
            await ledger.execute(
                goal_run_id="run-a",
                work_item_id=item["id"],
                operation_key=operation_key,
                operation_type="publish",
                target_ref="provider:item",
                request={"value": 1},
                provider=provider,
                bot_id=BOT_A,
            )
            == "published"
        )
        assert provider_calls == 1
        # Bot B reusing the same key is refused, never replayed.
        with pytest.raises(DurableExecutionError) as exc_info:
            await ledger.execute(
                goal_run_id="run-a",
                work_item_id=item["id"],
                operation_key=operation_key,
                operation_type="publish",
                target_ref="provider:item",
                request={"value": 1},
                provider=provider,
                bot_id=BOT_B,
            )
        assert exc_info.value.code == "CROSS_BOT_DENIED"
        assert provider_calls == 1
    finally:
        await repo.close()


def test_bot_id_propagation(tmp_path):
    """One bot's id lands on every durable object derived from its GoalRun."""
    _, bot_a, _ = _bots(tmp_path)
    reg_a, _ = _routine_registries(tmp_path)
    assert_bot_may_use_skill(bot_a, "brief.render")
    state = GoalRunState(goal_id="goal-a", goal_text="propagate", bot_id=BOT_A)
    dispatch_trigger(
        state, reg_a.lookup("schedule.trigger"), topic="schedule.trigger", trigger_id="t1"
    )
    state.delegate_states["d1"] = DelegateState(
        delegate_id="d1",
        parent_goal_run_id="goal-a",
        status="complete",
        evidence_refs=["sha:d1"],
        bot_id=BOT_A,
    )
    state.fanin_states["f1"] = FanInState(
        fanin_id="f1",
        expected_delegate_ids=["d1"],
        completed_delegate_ids=["d1"],
        reconciled_result_ref="sha:r1",
        bot_id=BOT_A,
    )
    state.playbook_states["publish.report"] = PlaybookState(
        playbook_id="publish.report", version=3, completed_steps=["parse"], bot_id=BOT_A
    )
    save_goal_run(state, str(tmp_path))
    resumed = load_goal_run(str(tmp_path), "goal-a", bot_id=BOT_A)
    assert resumed is not None
    assert resumed.bot_id == BOT_A
    assert resumed.delegate_states["d1"].bot_id == BOT_A
    assert resumed.fanin_states["f1"].bot_id == BOT_A
    assert resumed.playbook_states["publish.report"].bot_id == BOT_A
    assert resumed.routine_states["morning.brief"].bot_id == BOT_A
    claimed = (
        EvidenceBundle(task_id="task-a", goal_run_id="goal-a", head_sha="sha:a")
        .scoped_to(BOT_A)
        .bot_id
    )
    assert claimed == BOT_A


async def test_bot_a_restart_resume_and_b_unaffected(tmp_path):
    """Bot A restarts mid-chain on the SAME GoalRun/computer; bot B is untouched."""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    _, bot_a, bot_b = _bots(tmp_path)
    assert_bot_may_use_skill(bot_a, "brief.render")
    assert_bot_may_use_skill(bot_b, "other.skill")

    async def gateway(request):
        assert request.goal_run_id == state_a.goal_id
        assert request.bot_id == BOT_A
        return {"status": "completed", "executed": True, "result": "ok"}

    state_a = GoalRunState(goal_id="goal-a", goal_text="restart mid chain", bot_id=BOT_A)
    state_b = GoalRunState(goal_id="goal-b", goal_text="idle neighbor", bot_id=BOT_B)
    save_goal_run(state_b, str(project_root))
    snapshot_b = dict(state_b.to_taskgraph_json())
    worker_a = CanonicalWorkerAdapter(task_id="task-a", objective=state_a.goal_text)
    await worker_a.before_execution(state_a, str(project_root))
    assert worker_a.computer_id is not None
    assert worker_a.context_engine.bot_id == BOT_A
    computer_a = worker_a.computer_store.get_computer(worker_a.computer_id, bot_id=BOT_A)
    assert computer_a is not None and computer_a.bot_id == BOT_A
    master = MasterAgentActionAdapter(
        goal_run_id=state_a.goal_id,
        task_id="task-a",
        computer_ref=worker_a.computer_id,
        approval={"side_effect": "pure_read"},
        executor=lambda request: worker_a.execute_canonical_action(
            state_a, request, gateway_executor=gateway
        ),
        bot_id=BOT_A,
    )
    result = await master.execute("read_file", {"path": "a"})
    assert result.status == "completed"
    worker_a.checkpoint(state_a, str(project_root), reason="pre_restart")
    save_goal_run(state_a, str(project_root))
    if worker_a.execution_repository is not None:
        await worker_a.execution_repository.close()

    # Restart: same GoalRun, same computer, state preserved.
    resumed = load_goal_run(str(project_root), "goal-a", bot_id=BOT_A)
    assert resumed is not None and resumed.bot_id == BOT_A
    worker_a2 = CanonicalWorkerAdapter(task_id="task-a", objective=resumed.goal_text)
    await worker_a2.before_execution(resumed, str(project_root))
    assert worker_a2.computer_id == worker_a.computer_id
    assert worker_a2.context_engine.bot_id == BOT_A
    master2 = MasterAgentActionAdapter(
        goal_run_id=resumed.goal_id,
        task_id="task-a",
        computer_ref=worker_a2.computer_id,
        approval={"side_effect": "pure_read"},
        executor=lambda request: worker_a2.execute_canonical_action(
            resumed, request, gateway_executor=gateway
        ),
        bot_id=BOT_A,
    )
    post = await master2.execute("read_file", {"path": "b"})
    assert post.status == "completed"
    if worker_a2.execution_repository is not None:
        await worker_a2.execution_repository.close()

    # Bot B never moved: identical bytes, and bot B still resumes its own run.
    reloaded_b = load_goal_run(str(project_root), "goal-b", bot_id=BOT_B)
    assert reloaded_b is not None
    assert reloaded_b.to_taskgraph_json() == snapshot_b
    with pytest.raises(CrossBotAccessDenied):
        load_goal_run(str(project_root), "goal-b", bot_id=BOT_A)


def test_authorities_stay_zero():
    assert SECOND_EXECUTION_AUTHORITY == 0
    assert SECOND_ACCEPTANCE_AUTHORITY == 0
    claimed = DelegateResult(
        delegate_id="self", status="complete", stop_reason="completed", summary="self"
    )
    claimed.self_report = True  # type: ignore[attr-defined]
    assert reconcile_delegate_results([claimed])["overall_status"] == "blocked"


def test_playbook_and_skill_bot_refs(tmp_path):
    """Availability refs live on the bot; registries stay authority-free."""
    store = _JsonRegistryStore(tmp_path / "catalog.json")
    skills = SkillRegistry(store)
    skills.register_candidate(
        SkillSpec(skill_id="brief.render", instructions="render morning brief", version=2)
    )
    playbooks = PlaybookRegistry(store, skills=skills)
    playbooks.register_candidate(
        PlaybookSpec(
            playbook_id="publish.report",
            version=3,
            steps=[
                PlaybookStep(step_id="parse", skill_id="brief.render"),
                PlaybookStep(step_id="draft", depends_on=["parse"]),
            ],
        )
    )
    _, bot_a, bot_b = _bots(tmp_path)
    assert_bot_may_use_skill(bot_a, "brief.render")
    assert_bot_may_use_playbook(bot_a, "publish.report")
    with pytest.raises(CrossBotAccessDenied):
        assert_bot_may_use_skill(bot_b, "brief.render")
    with pytest.raises(CrossBotAccessDenied):
        assert_bot_may_use_playbook(bot_b, "publish.report")
