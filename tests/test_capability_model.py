"""server.capability_model 测试(VAOM Capability/Skill/Knowledge/Harness P2 落地,
见 docs/dev/rfc-01-vaom.md)。全部用 tmp_path 隔离存储, 不碰真实 ~/.veya 文件。"""

from __future__ import annotations

import pytest

from server.capability_model import (
    CapabilityRegistry,
    CapabilitySpec,
    HarnessRegistry,
    HarnessSpec,
    KnowledgePack,
    KnowledgeRegistry,
    PerformanceStore,
    SkillRegistry,
    SkillSpec,
    _JsonRegistryStore,
    bootstrap_default_harnesses,
    sync_skills_from_hub,
)


def _store(tmp_path):
    return _JsonRegistryStore(storage_path=tmp_path / "registry.json")


# ── CapabilityRegistry ──────────────────────────────────────────────────


def test_capability_register_and_get(tmp_path):
    reg = CapabilityRegistry(_store(tmp_path))
    spec = CapabilitySpec(capability_id="c1", domain="coding", description="large refactor")
    reg.register_candidate(spec)

    got = reg.get("c1")
    assert got is not None
    assert got.status == "candidate"
    assert got.description == "large refactor"


def test_capability_search_matches_description(tmp_path):
    reg = CapabilityRegistry(_store(tmp_path))
    reg.register_candidate(
        CapabilitySpec(capability_id="c1", domain="coding", description="refactor auth")
    )
    reg.register_candidate(
        CapabilitySpec(capability_id="c2", domain="security", description="review deps")
    )

    results = reg.search("refactor")
    assert [r.capability_id for r in results] == ["c1"]


def test_capability_verify_requires_evaluators_and_benchmark(tmp_path):
    reg = CapabilityRegistry(_store(tmp_path))
    reg.register_candidate(CapabilitySpec(capability_id="c1", domain="coding", description="d"))

    assert reg.verify("c1") is False  # 没有 evaluators/benchmark_suite
    assert reg.get("c1").status == "candidate"

    reg.register_candidate(
        CapabilitySpec(
            capability_id="c1",
            domain="coding",
            description="d",
            evaluators=["e0"],
            benchmark_suite="suite1",
        )
    )
    assert reg.verify("c1") is True
    assert reg.get("c1").status == "verified"


def test_capability_deprecate(tmp_path):
    reg = CapabilityRegistry(_store(tmp_path))
    reg.register_candidate(CapabilitySpec(capability_id="c1", domain="d", description="d"))
    reg.deprecate("c1")
    assert reg.get("c1").status == "deprecated"


# ── SkillRegistry ───────────────────────────────────────────────────────


def test_skill_promote_requires_benchmark_data(tmp_path):
    reg = SkillRegistry(_store(tmp_path))
    reg.register_candidate(SkillSpec(skill_id="s1", instructions="do x"))

    assert reg.promote("s1") is False  # 没有 performance 数据

    reg.benchmark("s1", {"success_rate": 0.9, "tokens": 1200})
    spec = reg.get_version("s1")
    assert spec.performance == {"success_rate": 0.9, "tokens": 1200}

    assert reg.promote("s1") is True
    assert reg.get_version("s1").status == "verified"


def test_skill_rollback(tmp_path):
    reg = SkillRegistry(_store(tmp_path))
    reg.register_candidate(SkillSpec(skill_id="s1", instructions="do x"))
    reg.benchmark("s1", {"success_rate": 1.0})
    reg.promote("s1")
    reg.rollback("s1")
    assert reg.get_version("s1").status == "deprecated"


def test_skill_search(tmp_path):
    reg = SkillRegistry(_store(tmp_path))
    reg.register_candidate(SkillSpec(skill_id="s1", instructions="parse excel files"))
    reg.register_candidate(SkillSpec(skill_id="s2", instructions="send email"))
    assert [s.skill_id for s in reg.search("excel")] == ["s1"]


# ── P2-05 Skill Teaching UX (candidate → confirm flow) ──────────────────


def test_skill_propose_creates_candidate(tmp_path):
    reg = SkillRegistry(_store(tmp_path))
    spec = reg.propose_skill("A test skill for doing something useful")

    assert spec.status == "candidate"
    assert spec.skill_id is not None
    assert spec.version == 1
    assert spec.instructions == "A test skill for doing something useful"
    assert spec.provenance.startswith("skill_teach_proposal@")

    # Verify it's in the registry
    retrieved = reg.get_version(spec.skill_id)
    assert retrieved.status == "candidate"
    assert retrieved.skill_id == spec.skill_id


def test_skill_confirm_changes_to_verified(tmp_path):
    reg = SkillRegistry(_store(tmp_path))
    spec = reg.propose_skill("Test skill to confirm")
    skill_id = spec.skill_id

    confirmed = reg.confirm_skill(skill_id)

    assert confirmed is not None
    assert confirmed.status == "verified"
    assert confirmed.version == 2  # version incremented
    assert confirmed.skill_id == skill_id

    # Verify persisted
    retrieved = reg.get_version(skill_id)
    assert retrieved.status == "verified"
    assert retrieved.version == 2


def test_skill_confirm_non_candidate_raises(tmp_path):
    reg = SkillRegistry(_store(tmp_path))
    # Directly insert a verified skill (bypassing propose)
    reg._store.put(
        "skill",
        "s1",
        {
            "skill_id": "s1",
            "instructions": "test",
            "version": 1,
            "status": "verified",
            "performance": {},
            "applicable_when": [],
            "not_applicable_when": [],
            "required_tools": [],
            "knowledge_refs": [],
            "evaluators": [],
            "benchmark_suite": None,
            "provenance": "",
        },
    )

    try:
        reg.confirm_skill("s1")
        raise AssertionError("Should have raised ValueError")
    except ValueError as e:
        assert "not a candidate" in str(e)


def test_skill_reject_changes_to_deprecated(tmp_path):
    reg = SkillRegistry(_store(tmp_path))
    spec = reg.propose_skill("Skill to reject")
    skill_id = spec.skill_id

    rejected = reg.reject_skill(skill_id)

    assert rejected is True
    retrieved = reg.get_version(skill_id)
    assert retrieved.status == "deprecated"


def test_skill_reject_nonexistent_returns_false(tmp_path):
    reg = SkillRegistry(_store(tmp_path))
    rejected = reg.reject_skill("nonexistent")
    assert rejected is False


# ── KnowledgeRegistry ────────────────────────────────────────────────────


def test_knowledge_import_provenance_invalidate(tmp_path):
    reg = KnowledgeRegistry(_store(tmp_path))
    reg.import_pack(
        KnowledgePack(
            knowledge_id="k1",
            title="typography",
            domain="design",
            source="internal-doc",
            provenance="user-authored",
        )
    )
    assert reg.provenance("k1") == {"source": "internal-doc", "provenance": "user-authored"}
    reg.invalidate("k1")
    packs = reg.search()
    assert packs[0].status == "deprecated"


def test_knowledge_provenance_missing_returns_none(tmp_path):
    reg = KnowledgeRegistry(_store(tmp_path))
    assert reg.provenance("nonexistent") is None


# ── HarnessRegistry + PerformanceStore ───────────────────────────────────


def test_harness_list_and_capability_matrix(tmp_path):
    reg = HarnessRegistry(_store(tmp_path))
    reg.register(HarnessSpec(harness_id="hicode", version="1", capabilities=["coding", "test"]))
    reg.register(HarnessSpec(harness_id="dsh", version="1", capabilities=["domain"]))

    matrix = reg.capability_matrix()
    assert matrix == {"hicode": ["coding", "test"], "dsh": ["domain"]}


def test_harness_registry_has_no_resume_cancel():
    # resume/cancel 刻意不实现——见 capability_model.py::HarnessRegistry docstring,
    # hicode/dsh 都是"提交后等到底"的同步语义, 没有真实"恢复进行中调用"的场景。
    # execute() 已实现(PR-15), 见 test_harness_registry_execute_routes_*。
    assert not hasattr(HarnessRegistry, "resume")
    assert not hasattr(HarnessRegistry, "cancel")


@pytest.mark.asyncio
async def test_harness_registry_execute_routes_to_run_builtin(tmp_path, monkeypatch):
    calls = []

    def fake_run_builtin(store, task_id, request):
        calls.append(("builtin", store, task_id, request))
        return "builtin-response"

    monkeypatch.setattr("server.project_ask._run_builtin", fake_run_builtin)

    reg = HarnessRegistry(_store(tmp_path))
    result = await reg.execute("builtin", store="STORE", task_id="t1", request="do x")

    assert result == "builtin-response"
    assert calls == [("builtin", "STORE", "t1", "do x")]


@pytest.mark.asyncio
async def test_harness_registry_execute_routes_to_run_hicode(tmp_path, monkeypatch):
    calls = []

    async def fake_run_hicode(store, task_id, project_root, request, understand_prefix):
        calls.append(("hicode", store, task_id, project_root, request, understand_prefix))
        return "hicode-response"

    monkeypatch.setattr("server.project_ask._run_hicode", fake_run_hicode)

    reg = HarnessRegistry(_store(tmp_path))
    result = await reg.execute(
        "hicode",
        store="STORE",
        task_id="t1",
        request="do x",
        project_root="/proj",
        understand_prefix="prefix",
    )

    assert result == "hicode-response"
    assert calls == [("hicode", "STORE", "t1", "/proj", "do x", "prefix")]


@pytest.mark.asyncio
async def test_harness_registry_execute_routes_to_run_dsh(tmp_path, monkeypatch):
    calls = []

    async def fake_run_dsh(store, task_id, project_root, request, understand_prefix):
        calls.append(("dsh", store, task_id, project_root, request, understand_prefix))
        return "dsh-response"

    monkeypatch.setattr("server.project_ask._run_dsh", fake_run_dsh)

    reg = HarnessRegistry(_store(tmp_path))
    result = await reg.execute(
        "dsh", store="STORE", task_id="t1", request="do x", project_root="/proj"
    )

    assert result == "dsh-response"
    assert calls == [("dsh", "STORE", "t1", "/proj", "do x", "")]


@pytest.mark.asyncio
async def test_harness_registry_execute_unknown_harness_raises(tmp_path):
    reg = HarnessRegistry(_store(tmp_path))
    with pytest.raises(ValueError, match="unknown harness_id"):
        await reg.execute("nonexistent", store="STORE", task_id="t1", request="x")


def test_performance_store_record_and_aggregate(tmp_path):
    store = PerformanceStore(storage_path=tmp_path / "perf.jsonl")
    store.record_outcome(harness_id="hicode", task_archetype="refactor", success=True)
    store.record_outcome(harness_id="hicode", task_archetype="refactor", success=True)
    store.record_outcome(harness_id="hicode", task_archetype="refactor", success=False)

    profile = store.aggregate("hicode", "refactor")
    assert profile is not None
    assert profile.sample_size == 3
    assert profile.success_rate == 2 / 3


def test_performance_store_aggregate_missing_returns_none(tmp_path):
    store = PerformanceStore(storage_path=tmp_path / "perf.jsonl")
    assert store.aggregate("nonexistent") is None


def test_performance_store_compare(tmp_path):
    store = PerformanceStore(storage_path=tmp_path / "perf.jsonl")
    store.record_outcome(harness_id="hicode", task_archetype="t", success=True)
    store.record_outcome(harness_id="dsh", task_archetype="t", success=False)

    result = store.compare(["hicode", "dsh", "nonexistent"], "t")
    assert set(result.keys()) == {"hicode", "dsh"}
    assert result["hicode"].success_rate == 1.0
    assert result["dsh"].success_rate == 0.0


def test_performance_store_confidence_scales_with_sample_size(tmp_path):
    store = PerformanceStore(storage_path=tmp_path / "perf.jsonl")
    assert store.confidence("hicode") == 0.0  # 无样本

    for _ in range(20):
        store.record_outcome(harness_id="hicode", task_archetype="t", success=True)
    assert store.confidence("hicode", "t") == 1.0  # 封顶


# ── 桥接：sync_skills_from_hub ────────────────────────────────────────────


class _FakeSkillHub:
    """模拟 server.skill_hub.VeyaSkillHub 的公开接口, 不依赖真实技能目录。"""

    skills_dir = "/fake/skills"

    def get_stats(self):
        return {"skills": ["safe_skill", "risky_skill"]}

    def describe(self, name):
        return {"safe_skill": "does safe things", "risky_skill": "does risky things"}[name]

    def skill_risk(self, name):
        if name == "risky_skill":
            return {"max_severity": "high", "categories": ["subprocess"]}
        return {"max_severity": "none", "categories": []}


def test_sync_skills_from_hub(tmp_path, monkeypatch):
    import server.capability_model as cm

    monkeypatch.setattr(cm, "skill_registry", SkillRegistry(_store(tmp_path)))

    count = sync_skills_from_hub(_FakeSkillHub())
    assert count == 2

    safe = cm.skill_registry.get_version("safe_skill")
    assert safe.instructions == "does safe things"
    assert safe.not_applicable_when == []

    risky = cm.skill_registry.get_version("risky_skill")
    assert any("high risk" in w or "high" in w for w in risky.not_applicable_when)


# ── bootstrap_default_harnesses ──────────────────────────────────────────


def test_bootstrap_default_harnesses(tmp_path, monkeypatch):
    import server.capability_model as cm

    fresh = HarnessRegistry(_store(tmp_path))
    monkeypatch.setattr(cm, "harness_registry", fresh)

    bootstrap_default_harnesses()

    ids = {h.harness_id for h in cm.harness_registry.list()}
    assert ids == {"hicode", "dsh", "builtin"}
    hicode = cm.harness_registry.get("hicode")
    assert "SandboxBroker" in hicode.workspace_semantics
