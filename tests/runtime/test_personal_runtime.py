"""Personal Agent Runtime correctness contract on an isolated SQLite authority."""

from __future__ import annotations

import pytest

from runtime.personal.runtime import PersonalRuntimeError, PersonalRuntimeStore


@pytest.fixture
async def store(tmp_path):
    value = PersonalRuntimeStore(sqlite_path=tmp_path / "personal.db", production=False)
    await value.connect()
    yield value
    await value.close()


async def _active_memory(store: PersonalRuntimeStore, text: str, *, scope: str = "ws") -> dict:
    event = await store.record_event("test.source", {"content": text}, workspace_id=scope)
    candidate = await store.create_memory_candidate(
        text,
        scope_type="workspace",
        scope_id=scope,
        memory_type="preference",
        source_event_ids=[event["id"]],
        reason="test evidence",
    )
    return (await store.commit_memory_candidate(candidate["id"]))["record"]


@pytest.mark.asyncio
async def test_memory_exact_dedup_and_structured_provenance(store):
    first = await _active_memory(store, "project tests use pytest")
    event = await store.record_event("test.source", {"duplicate": True}, workspace_id="ws")
    candidate = await store.create_memory_candidate(
        "project tests use pytest",
        scope_type="workspace",
        scope_id="ws",
        memory_type="preference",
        source_event_ids=[event["id"]],
        reason="repeat evidence",
    )
    result = await store.commit_memory_candidate(candidate["id"])
    assert result["status"] == "deduplicated"
    assert result["record"]["id"] == first["id"]
    assert result["record"]["source_event_ids"]
    assert (await store.memory_doctor())["provenance_coverage"] == 1.0


@pytest.mark.asyncio
async def test_memory_conflict_correction_supersede_and_forget(store):
    old = await _active_memory(store, "project tests use unittest")
    event = await store.record_event("test.correction", {"content": "pytest"}, workspace_id="ws")
    conflicting = await store.create_memory_candidate(
        "project tests use pytest",
        scope_type="workspace",
        scope_id="ws",
        memory_type="preference",
        source_event_ids=[event["id"]],
        reason="new evidence",
    )
    assert old["id"] in conflicting["conflicts_with"]
    with pytest.raises(PersonalRuntimeError, match="CONFLICT_REVIEW_REQUIRED"):
        await store.commit_memory_candidate(conflicting["id"])
    corrected = await store.correct_memory(old["id"], "project tests use uv run pytest", source_event_ids=[event["id"]])
    assert (await store.get_memory(old["id"]))["status"] == "superseded"
    assert (await store.get_memory(corrected["new_id"]))["status"] == "active"
    assert (await store.search_memory("unittest", scope_id="ws")) == []
    forgotten = await store.forget_memory(corrected["new_id"])
    assert forgotten["status"] == "forgotten"
    assert await store.search_memory("pytest", scope_id="ws") == []


@pytest.mark.asyncio
async def test_memory_correction_without_explicit_event_gets_durable_provenance(store):
    old = await _active_memory(store, "the project prefers unittest")
    corrected = await store.correct_memory(old["id"], "the project prefers pytest")
    source = await store.show_memory_source(corrected["new_id"])
    assert source["events"]
    assert source["events"][0]["event_type"] == "memory.correction_source"
    assert source["missing_event_ids"] == []


@pytest.mark.asyncio
async def test_memory_scope_isolation_and_candidate_boundary(store):
    await _active_memory(store, "private preference", scope="a")
    await _active_memory(store, "private preference", scope="b")
    assert len(await store.search_memory("private", scope_id="a")) == 1
    candidate = await store.create_memory_candidate(
        "unproven preference",
        scope_type="user",
        scope_id="u",
        source_session_ids=["s"],
        reason="candidate only",
    )
    assert await store.search_memory("unproven", scope_type="user", scope_id="u") == []
    assert candidate["status"] == "candidate"


@pytest.mark.asyncio
async def test_memory_missing_source_and_low_confidence_are_visible_to_doctor(store):
    candidate = await store.create_memory_candidate(
        "stale project fact",
        scope_type="workspace",
        scope_id="ws",
        memory_type="decision",
        source_event_ids=["missing-event"],
        confidence=0.2,
        reason="imported evidence",
    )
    record = (await store.commit_memory_candidate(candidate["id"]))["record"]
    doctor = await store.memory_doctor()
    assert record["id"] in doctor["missing_source_events"]
    assert record["id"] in doctor["low_confidence_active"]


@pytest.mark.asyncio
async def test_user_memory_scope_isolated_from_other_user_and_workspace(store):
    event = await store.record_event("test.user", {"user": "u1"})
    candidate = await store.create_memory_candidate(
        "private user rule",
        scope_type="user",
        scope_id="u1",
        memory_type="preference",
        source_event_ids=[event["id"]],
        reason="explicit user rule",
    )
    await store.commit_memory_candidate(candidate["id"])
    assert len(await store.search_memory("private", scope_type="user", scope_id="u1")) == 1
    assert await store.search_memory("private", scope_type="user", scope_id="u2") == []
    assert await store.search_memory("private", scope_type="workspace", scope_id="ws") == []


@pytest.mark.asyncio
async def test_skill_teach_version_run_and_rollback(store):
    v1 = await store.create_skill_candidate("pr-review", "Use the PR checklist", scope_type="workspace", scope_id="ws", created_by="u")
    assert v1["source_event_ids"]
    await store.confirm_skill(v1["id"])
    assert (await store.run_skill(v1["skill_id"], {"pr": 1}))['result_status'] == "complete"
    v2 = await store.create_skill_candidate("pr-review", "Use the stricter PR checklist", scope_type="workspace", scope_id="ws", parent_version=1, created_by="u")
    await store.confirm_skill(v2["id"])
    assert (await store.get_skill(v1["skill_id"]))["versions"][0]["version"] == 2
    await store.rollback_skill(v1["skill_id"], 1)
    current = await store.get_skill(v1["skill_id"])
    assert current["versions"][0]["version"] == 1
    assert (await store.run_skill(v1["skill_id"], {}))["version"] == 1
    assert len((await store.get_skill(v1["skill_id"], versions=True))["versions"]) == 2


@pytest.mark.asyncio
async def test_skill_safety_gate_blocks_unsafe_candidate(store):
    candidate = await store.create_skill_candidate("unsafe", "run subprocess and rm -rf", scope_type="user", scope_id="u")
    with pytest.raises(PersonalRuntimeError, match="SAFETY_HOLD"):
        await store.confirm_skill(candidate["id"])


@pytest.mark.asyncio
async def test_skill_failed_run_is_recorded_without_activating_a_new_version(store):
    candidate = await store.create_skill_candidate(
        "missing-skill",
        "Call a registered skill",
        scope_type="user",
        scope_id="u",
        execution_type="tool_chain",
        execution_ref="not-installed",
    )
    await store.confirm_skill(candidate["id"])
    with pytest.raises(PersonalRuntimeError, match="NOT_FOUND"):
        await store.run_skill(candidate["skill_id"], {})
    current = await store.get_skill(candidate["skill_id"])
    assert current["current_version"] == 1
    assert current["versions"][0]["version"] == 1
    assert current["versions"][0]["failure_count"] == 1
    assert len(await store.search_skills("missing-skill", scope_type="user", scope_id="u")) == 1


@pytest.mark.asyncio
async def test_continuity_and_learning_gates(store):
    snapshot = await store.save_continuity({"active_tasks": [{"id": "t1"}], "memory_refs": [], "skill_refs": []}, user_id="u", workspace_id="ws", source_event_cursor="42")
    assert (await store.latest_continuity(user_id="u", workspace_id="ws"))["id"] == snapshot["id"]
    with pytest.raises(PersonalRuntimeError, match="LEARNING_THRESHOLD"):
        await store.create_learning_candidate(pattern_id="single", scope="ws", evidence_task_ids=["t1"], evidence_trajectory_ids=["tr1"], observation="one failure", hypothesis="change", confidence=.9, candidate_type="skill", proposed_change={})
    candidate = await store.create_learning_candidate(pattern_id="repeat", scope="ws", evidence_task_ids=["t1", "t2", "t3"], evidence_trajectory_ids=["tr1", "tr2", "tr3"], observation="repeated success", hypothesis="candidate only", confidence=.9, candidate_type="skill", proposed_change={})
    assert candidate["status"] == "candidate"
    with pytest.raises(PersonalRuntimeError, match="LEARNING_GATE"):
        await store.apply_learning(candidate["id"])
    with pytest.raises(PersonalRuntimeError, match="improvement_delta"):
        await store.record_learning_eval(candidate["id"], baseline_ref="v1", candidate_ref="v2", result={"delta": 0.2}, passed=True)
    await store.record_learning_eval(candidate["id"], baseline_ref="v1", candidate_ref="v2", result={"improvement_delta": 0.2}, passed=True)
    assert (await store.apply_learning(candidate["id"]))["status"] == "applied"
    await store.record_learning_eval(candidate["id"], baseline_ref="v2", candidate_ref="v3", result={"improvement_delta": -0.1}, passed=False)
    assert (await store.get_learning(candidate["id"]))["status"] == "degraded"
    assert (await store.rollback_learning(candidate["id"], reason="regression gate"))["status"] == "rolled_back"
    assert (await store.get_learning(candidate["id"]))["status"] == "rejected"


@pytest.mark.asyncio
async def test_repeated_accepted_trajectories_create_candidate_only(store, tmp_path):
    from server import events as events_module

    original = events_module.event_store
    events_module.event_store = type(original)(tmp_path / "personal-events.jsonl")
    try:
        for index in range(3):
            task_id = f"trajectory-task-{index}"
            event_id = f"trajectory-event-{index}"
            events_module.event_store.append(
                {
                    "event_id": event_id,
                    "topic": "trajectory.recorded",
                    "task_id": task_id,
                    "payload": {"task_id": task_id, "objective": "review a pull request", "outcome": "completed"},
                }
            )
            events_module.event_store.append(
                {
                    "event_id": f"eval-event-{index}",
                    "topic": "eval.recorded",
                    "task_id": task_id,
                    "payload": {"passed": True},
                }
            )
        candidates = await store.scan_trajectory_candidates(scope="workspace:ws")
        assert len(candidates) == 1
        assert len(candidates[0]["evidence_task_ids"]) == 3
        assert candidates[0]["status"] == "candidate"
    finally:
        events_module.event_store = original


@pytest.mark.asyncio
async def test_personal_outbox_is_replayable(store):
    await store.record_event("test.outbox", {"same": True}, idempotency_key="test-event-1")
    assert (await store.outbox_status())["pending"] == 1
    # No EventStore assertions here: publishing is integration-tested by the
    # production adapter; the durable row is still present before publication.
    assert (await store.publish_outbox(limit=10))["published"] == 1
    assert (await store.outbox_status())["pending"] == 0
