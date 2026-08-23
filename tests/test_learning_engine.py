"""server.learning_engine 测试(VAOM CandidateLearning/PromotionGate P4 落地,
见 docs/dev/rfc-01-vaom.md)。全部用 tmp_path 隔离存储, 不碰真实 ~/.veya 文件。"""

from __future__ import annotations

import pytest

from server.learning_engine import CandidateLearning, LearningEngine, _CandidateStore
from server.memory_controller import MemoryController, _MemoryStore


def _engine(tmp_path):
    memory = MemoryController(_MemoryStore(storage_path=tmp_path / "memory.json"))
    return LearningEngine(
        _CandidateStore(storage_path=tmp_path / "candidates.json"), memory
    ), memory


# ── reflect: 严格"完全相同内容+≥2个不同episode"规则 ─────────────────────


def test_reflect_finds_pattern_across_distinct_episodes(tmp_path):
    engine, memory = _engine(tmp_path)
    memory.observe("先跑迁移脚本再动代码", source_episode_ids=["ep1"], scope="project")
    memory.observe("先跑迁移脚本再动代码", source_episode_ids=["ep2"], scope="project")

    findings = engine.reflect()

    assert len(findings) == 1
    assert findings[0]["content"] == "先跑迁移脚本再动代码"
    assert findings[0]["source_episode_ids"] == ["ep1", "ep2"]
    assert len(findings[0]["memory_ids"]) == 2


def test_reflect_ignores_single_episode_content(tmp_path):
    engine, memory = _engine(tmp_path)
    memory.observe("只出现过一次", source_episode_ids=["ep1"])
    assert engine.reflect() == []


def test_reflect_ignores_same_episode_repeated_twice(tmp_path):
    """同一个 episode 里两条相同 content(比如两个 task 都提炼出同句话)不算
    "独立观察到两次"——source_episode_ids 去重后还是只有 1 个。"""
    engine, memory = _engine(tmp_path)
    memory.observe("重复内容", source_episode_ids=["ep1"])
    memory.observe("重复内容", source_episode_ids=["ep1"])
    assert engine.reflect() == []


def test_reflect_ignores_deprecated_records(tmp_path):
    engine, memory = _engine(tmp_path)
    r1 = memory.observe("内容", source_episode_ids=["ep1"])
    memory.observe("内容", source_episode_ids=["ep2"])
    memory.deprecate(r1.memory_id)

    assert engine.reflect() == []


def test_reflect_does_not_fuzzy_match_similar_but_different_content(tmp_path):
    engine, memory = _engine(tmp_path)
    memory.observe("先跑迁移脚本", source_episode_ids=["ep1"])
    memory.observe("先跑迁移脚本再测试", source_episode_ids=["ep2"])  # 相似但不同
    assert engine.reflect() == []


def test_reflect_filters_by_scope(tmp_path):
    engine, memory = _engine(tmp_path)
    memory.observe("内容", source_episode_ids=["ep1"], scope="project")
    memory.observe("内容", source_episode_ids=["ep2"], scope="global")
    assert engine.reflect(scope="project") == []  # project 里只有1个不同episode


# ── propose ──────────────────────────────────────────────────────────


def test_propose_creates_candidate_from_finding(tmp_path):
    engine, memory = _engine(tmp_path)
    memory.observe("lesson", source_episode_ids=["ep1"])
    memory.observe("lesson", source_episode_ids=["ep2"])
    finding = engine.reflect()[0]

    candidate = engine.propose(finding)

    assert candidate.status == "proposed"
    assert candidate.claim == "lesson"
    assert set(candidate.source_episode_ids) == {"ep1", "ep2"}
    assert len(candidate.evidence_for) == 2

    stored = engine._store.get(candidate.candidate_id)
    assert stored is not None
    assert stored.claim == "lesson"


# ── review (dual_axis_promotion_review 接线) ────────────────────────


@pytest.mark.asyncio
async def test_review_both_approve_moves_to_testing(tmp_path, monkeypatch):
    engine, memory = _engine(tmp_path)
    memory.observe("lesson", source_episode_ids=["ep1"])
    memory.observe("lesson", source_episode_ids=["ep2"])
    candidate = engine.propose(engine.reflect()[0])

    async def _approve(*, claim, evidence, llm_call_fn=None):
        approve = {"verdict": "approve", "concerns": [], "reasoning": "ok"}
        return {"value": approve, "safety": approve, "blocked": False}

    monkeypatch.setattr("server.learning_engine.dual_axis_promotion_review", _approve)

    reviewed = await engine.review(candidate.candidate_id)

    assert reviewed.status == "testing"
    assert reviewed.review["blocked"] is False


@pytest.mark.asyncio
async def test_review_rejected_axis_sets_status_rejected(tmp_path, monkeypatch):
    engine, memory = _engine(tmp_path)
    memory.observe("lesson", source_episode_ids=["ep1"])
    memory.observe("lesson", source_episode_ids=["ep2"])
    candidate = engine.propose(engine.reflect()[0])

    async def _reject_safety(*, claim, evidence, llm_call_fn=None):
        approve = {"verdict": "approve", "concerns": [], "reasoning": "ok"}
        reject = {"verdict": "reject", "concerns": ["too narrow"], "reasoning": "risky"}
        return {"value": approve, "safety": reject, "blocked": True}

    monkeypatch.setattr("server.learning_engine.dual_axis_promotion_review", _reject_safety)

    reviewed = await engine.review(candidate.candidate_id)

    assert reviewed.status == "rejected"


@pytest.mark.asyncio
async def test_review_missing_candidate_returns_none(tmp_path):
    engine, _ = _engine(tmp_path)
    assert await engine.review("nonexistent") is None


# ── promote: 要求 status=="testing" 且 ≥2 个独立 episode 证据 ──────────


def test_promote_requires_testing_status(tmp_path):
    engine, memory = _engine(tmp_path)
    memory.observe("lesson", source_episode_ids=["ep1"])
    memory.observe("lesson", source_episode_ids=["ep2"])
    candidate = engine.propose(engine.reflect()[0])

    assert engine.promote(candidate.candidate_id) is False  # 还是 proposed, 没审过


def test_promote_requires_min_source_episodes(tmp_path):
    engine, memory = _engine(tmp_path)
    single_ep_record = memory.observe("solo lesson", source_episode_ids=["ep1"])
    # 手工构造一个只有 1 个 episode 的 candidate, 绕过 reflect() 的天然过滤,
    # 直接验证 promote() 自己也守着这条门槛(不是只靠上游 reflect 挡)。
    candidate = CandidateLearning(
        claim="solo lesson",
        source_episode_ids=["ep1"],
        evidence_for=[single_ep_record.memory_id],
        status="testing",
    )
    engine._store.put(candidate)

    assert engine.promote(candidate.candidate_id) is False


def test_promote_succeeds_and_upgrades_memory_records(tmp_path):
    engine, memory = _engine(tmp_path)
    memory.observe("lesson", source_episode_ids=["ep1"])
    memory.observe("lesson", source_episode_ids=["ep2"])
    candidate = engine.propose(engine.reflect()[0])
    candidate.status = "testing"
    engine._store.put(candidate)

    assert engine.promote(candidate.candidate_id) is True
    assert engine._store.get(candidate.candidate_id).status == "promoted"

    for memory_id in candidate.evidence_for:
        assert memory.get(memory_id).status == "verified"


def test_promote_missing_candidate_returns_false(tmp_path):
    engine, _ = _engine(tmp_path)
    assert engine.promote("nonexistent") is False


# ── reject ───────────────────────────────────────────────────────────


def test_reject_sets_status_and_records_reason(tmp_path):
    engine, memory = _engine(tmp_path)
    memory.observe("lesson", source_episode_ids=["ep1"])
    memory.observe("lesson", source_episode_ids=["ep2"])
    candidate = engine.propose(engine.reflect()[0])

    engine.reject(candidate.candidate_id, reason="too speculative")

    stored = engine._store.get(candidate.candidate_id)
    assert stored.status == "rejected"
    assert "too speculative" in stored.evidence_against


def test_reject_missing_candidate_is_noop(tmp_path):
    engine, _ = _engine(tmp_path)
    engine.reject("nonexistent", reason="x")  # 不抛异常


# ── 持久化往返 ────────────────────────────────────────────────────────


def test_candidate_store_persistence_roundtrip(tmp_path):
    path = tmp_path / "shared.json"
    store1 = _CandidateStore(storage_path=path)
    record = CandidateLearning(claim="persisted", source_episode_ids=["ep1", "ep2"])
    store1.put(record)

    store2 = _CandidateStore(storage_path=path)
    reloaded = store2.get(record.candidate_id)
    assert reloaded is not None
    assert reloaded.claim == "persisted"


# ── MemoryController.get (新增的公开方法, learning_engine 依赖它) ──────


def test_memory_controller_get(tmp_path):
    memory = MemoryController(_MemoryStore(storage_path=tmp_path / "m.json"))
    record = memory.observe("content")
    assert memory.get(record.memory_id).content == "content"
    assert memory.get("nonexistent") is None
