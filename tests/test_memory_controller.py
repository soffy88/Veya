"""server.memory_controller 测试(VAOM MemoryRecord/MemoryController P3 落地,
见 docs/dev/rfc-01-vaom.md, docs/dev/rfc-04-data-plane-decision.md)。
全部用 tmp_path 隔离存储, 不碰真实 ~/.veya 文件。"""

from __future__ import annotations

from server.goal_run.trust_plane import (
    append_trust_plane_records,
    build_and_write_task_episode,
    record_task_verification,
)
from server.memory_controller import MemoryController, _MemoryStore


def _controller(tmp_path):
    return MemoryController(_MemoryStore(storage_path=tmp_path / "memory.json"))


# ── observe / search ──────────────────────────────────────────────────


def test_observe_creates_candidate_record(tmp_path):
    ctl = _controller(tmp_path)
    record = ctl.observe("auth refactor should scan migrations first", entities=["auth"])
    assert record.status == "candidate"
    assert record.type == "semantic"

    found = ctl.search("migrations")
    assert [r.memory_id for r in found] == [record.memory_id]


def test_search_filters_by_scope(tmp_path):
    ctl = _controller(tmp_path)
    ctl.observe("project rule", scope="project")
    ctl.observe("global rule", scope="global")

    assert len(ctl.search(scope="global")) == 1
    assert len(ctl.search()) == 2


def test_search_matches_entities_and_keywords(tmp_path):
    ctl = _controller(tmp_path)
    ctl.observe("some note", entities=["auth-service"], keywords=["migration"])
    assert len(ctl.search("auth-service")) == 1
    assert len(ctl.search("migration")) == 1
    assert len(ctl.search("nonexistent")) == 0


# ── extract_candidates: 真实桥接 trust_plane.py 的 TaskEpisode ──────────


def test_extract_candidates_missing_episode_returns_empty(tmp_path):
    ctl = _controller(tmp_path)
    assert ctl.extract_candidates(str(tmp_path), "nonexistent-goal") == []


def test_extract_candidates_from_real_episode(tmp_path):
    project_root = str(tmp_path / "proj")
    goal_id = "g-extract"

    # 造一条真实的 P1 Trust Plane 记录(verify_passed=True 才会产生 VerifiedState)
    claim, evidences, evaluations, verified_state = record_task_verification(
        task_id="t1",
        goal_id=goal_id,
        actor="hicode",
        statement="迁移脚本先跑通再动代码, auth 重构零回归",
        target_refs=[],
        verify_passed=True,
        verify_summary="ok",
        diff_text="diff",
        review_findings=None,
    )
    append_trust_plane_records(
        project_root,
        goal_id,
        claim=claim,
        evidences=evidences,
        evaluations=evaluations,
        verified_state=verified_state,
    )
    build_and_write_task_episode(
        project_root,
        goal_id,
        "重构认证系统",
        task_ids=["t1"],
        outcome="completed",
        started_at=None,
        completed_at=None,
    )

    ctl = _controller(tmp_path)
    candidates = ctl.extract_candidates(project_root, goal_id)

    assert len(candidates) == 1
    assert candidates[0].content == claim.statement
    assert candidates[0].type == "episodic"
    assert candidates[0].trust_level == "L2_verified"
    assert candidates[0].provenance == f"goal_run:{goal_id}"
    # 真的落盘了, 不只是内存返回值
    assert ctl.search("auth")[0].memory_id == candidates[0].memory_id


def test_extract_candidates_skips_tasks_without_verified_state(tmp_path):
    project_root = str(tmp_path / "proj")
    goal_id = "g-failed"

    claim, evidences, evaluations, verified_state = record_task_verification(
        task_id="t1",
        goal_id=goal_id,
        actor="hicode",
        statement="failed attempt",
        target_refs=[],
        verify_passed=False,
        verify_summary="fail",
        diff_text="",
        review_findings=None,
    )
    append_trust_plane_records(
        project_root,
        goal_id,
        claim=claim,
        evidences=evidences,
        evaluations=evaluations,
        verified_state=verified_state,
    )
    build_and_write_task_episode(
        project_root,
        goal_id,
        "goal",
        task_ids=["t1"],
        outcome="blocked",
        started_at=None,
        completed_at=None,
    )

    ctl = _controller(tmp_path)
    assert ctl.extract_candidates(project_root, goal_id) == []


# ── consolidate ──────────────────────────────────────────────────────


def test_consolidate_marks_exact_duplicates_superseded(tmp_path):
    ctl = _controller(tmp_path)
    first = ctl.observe("same content", scope="project")
    second = ctl.observe("same content", scope="project")

    count = ctl.consolidate()

    assert count == 1
    stored_first = ctl._store.get(first.memory_id)
    stored_second = ctl._store.get(second.memory_id)
    assert stored_first.status == "deprecated"
    assert second.memory_id in stored_first.supersedes
    assert stored_second.status == "candidate"


def test_consolidate_ignores_distinct_content(tmp_path):
    ctl = _controller(tmp_path)
    ctl.observe("content A")
    ctl.observe("content B")
    assert ctl.consolidate() == 0


# ── resolve_conflict ─────────────────────────────────────────────────


def test_resolve_conflict_marks_overlapping_entities_different_content(tmp_path):
    ctl = _controller(tmp_path)
    a = ctl.observe("auth uses JWT", scope="project", entities=["auth"])
    b = ctl.observe("auth uses session cookies", scope="project", entities=["auth"])

    marked = ctl.resolve_conflict()

    assert marked == 1
    stored_a = ctl._store.get(a.memory_id)
    stored_b = ctl._store.get(b.memory_id)
    assert b.memory_id in stored_a.contradicts
    assert a.memory_id in stored_b.contradicts


def test_resolve_conflict_ignores_different_scope_or_no_shared_entity(tmp_path):
    ctl = _controller(tmp_path)
    ctl.observe("x uses JWT", scope="project", entities=["auth"])
    ctl.observe("x uses cookies", scope="global", entities=["auth"])  # 不同 scope
    ctl.observe("y uses cookies", scope="project", entities=["billing"])  # 无共享 entity

    assert ctl.resolve_conflict() == 0


def test_resolve_conflict_ignores_identical_content(tmp_path):
    ctl = _controller(tmp_path)
    ctl.observe("same fact", scope="project", entities=["auth"])
    ctl.observe("same fact", scope="project", entities=["auth"])
    assert ctl.resolve_conflict() == 0


# ── promote / deprecate / explain_provenance ────────────────────────


def test_promote_requires_source_refs(tmp_path):
    ctl = _controller(tmp_path)
    record = ctl.observe("no source refs")
    assert ctl.promote(record.memory_id) is False
    assert ctl._store.get(record.memory_id).status == "candidate"


def test_promote_succeeds_with_source_episode(tmp_path):
    ctl = _controller(tmp_path)
    record = ctl.observe("has source", source_episode_ids=["episode_abc"])
    assert ctl.promote(record.memory_id) is True
    assert ctl._store.get(record.memory_id).status == "verified"


def test_promote_missing_id_returns_false(tmp_path):
    ctl = _controller(tmp_path)
    assert ctl.promote("nonexistent") is False


def test_deprecate(tmp_path):
    ctl = _controller(tmp_path)
    record = ctl.observe("to be deprecated")
    ctl.deprecate(record.memory_id)
    assert ctl._store.get(record.memory_id).status == "deprecated"


def test_explain_provenance(tmp_path):
    ctl = _controller(tmp_path)
    record = ctl.observe(
        "with provenance", provenance="goal_run:g1", source_episode_ids=["episode_1"]
    )
    info = ctl.explain_provenance(record.memory_id)
    assert info == {
        "provenance": "goal_run:g1",
        "source_episode_ids": ["episode_1"],
        "source_artifact_ids": [],
        "source_knowledge_ids": [],
        "trust_level": "unknown",
    }


def test_explain_provenance_missing_returns_none(tmp_path):
    ctl = _controller(tmp_path)
    assert ctl.explain_provenance("nonexistent") is None


# ── 持久化往返 ────────────────────────────────────────────────────────


def test_persistence_roundtrip_across_controller_instances(tmp_path):
    path = tmp_path / "shared.json"
    ctl1 = MemoryController(_MemoryStore(storage_path=path))
    record = ctl1.observe("persisted content")

    ctl2 = MemoryController(_MemoryStore(storage_path=path))
    reloaded = ctl2._store.get(record.memory_id)
    assert reloaded is not None
    assert reloaded.content == "persisted content"
