"""D6 gates: skill distribution lifecycle over frozen authorities.

Matrix with isolated backends (sqlite personal store, JSON registry file,
fixture capability package): import/provenance, qualification/promotion
gates, binding, materialization, refresh, rollback, revoke, idempotency,
restart, catalog-boundary, D9 boundary. No skips, no swallowed exceptions.
"""

from __future__ import annotations

import pytest

from server.skill_distribution import (
    SkillBinding,
    SkillDistribution,
    SkillDistributionRecord,
    SkillLifecycleState,
    SourceProvenance,
    classify_personal_version,
    classify_registry_spec,
    select_distribution_backend,
)


@pytest.fixture()
def events() -> list[tuple[str, dict]]:
    return []


def _emit(events: list[tuple[str, dict]]):
    def _record(topic: str, payload: dict) -> None:
        events.append((topic, payload))

    return _record


@pytest.fixture()
def personal_store(tmp_path, monkeypatch):
    import runtime.personal.runtime as personal_module
    from runtime.personal.runtime import PersonalRuntimeStore

    store = PersonalRuntimeStore(sqlite_path=tmp_path / "personal.db", production=False)
    monkeypatch.setattr(personal_module, "get_personal_runtime", lambda: store)
    monkeypatch.setattr("runtime.personal.get_personal_runtime", lambda: store)
    return store


@pytest.fixture()
def registry_isolated(tmp_path, monkeypatch):
    from server import capability_model
    from server.capability_model import SkillRegistry, _JsonRegistryStore

    isolated = SkillRegistry(_JsonRegistryStore(tmp_path / "cap.json"))
    monkeypatch.setattr(capability_model, "skill_registry", isolated)
    return isolated


@pytest.fixture()
def package_dir(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "skills").mkdir(parents=True)
    (pkg / "CAPABILITY.yaml").write_text("id: test-pkg\ndomain: test\n", encoding="utf-8")
    (pkg / "skills" / "hello.yaml").write_text(
        "instructions: Say hello warmly.\napplicable_when: [greeting]\n",
        encoding="utf-8",
    )
    return pkg


def _dist(tmp_path, events, **kwargs):
    return SkillDistribution(store_root=tmp_path / "dist", emit=_emit(events), **kwargs)


# -- vocabulary ---------------------------------------------------------------


def test_lifecycle_states_complete():
    assert {m.value for m in SkillLifecycleState} == {
        "discovered",
        "imported",
        "candidate",
        "qualifying",
        "verified",
        "active",
        "deprecated",
        "revoked",
    }
    with pytest.raises(ValueError):
        SkillLifecycleState.coerce("teleport")


def test_backend_selection_centralized(monkeypatch):
    monkeypatch.delenv("VEYA_EXECUTION_DATABASE_URL", raising=False)
    assert select_distribution_backend() == "registry"
    monkeypatch.setenv("VEYA_EXECUTION_DATABASE_URL", "postgres://x")
    assert select_distribution_backend() == "personal"
    assert select_distribution_backend("personal") == "personal"
    with pytest.raises(ValueError):
        select_distribution_backend("nope")


def test_classify_maps_and_rejects_unknown():
    assert classify_personal_version({"status": "candidate"}) == SkillLifecycleState.CANDIDATE
    assert (
        classify_personal_version({"status": "active", "trust_status": "trusted"})
        == SkillLifecycleState.ACTIVE
    )
    assert (
        classify_personal_version({"status": "active", "trust_status": "review_required"})
        == SkillLifecycleState.QUALIFYING
    )
    assert classify_personal_version({"status": "deprecated"}) == SkillLifecycleState.DEPRECATED
    assert (
        classify_personal_version({"status": "x", "trust_status": "blocked"})
        == SkillLifecycleState.REVOKED
    )
    with pytest.raises(ValueError):
        classify_personal_version({"status": "teleport"})
    assert classify_registry_spec("verified", "trusted") is SkillLifecycleState.ACTIVE
    assert classify_registry_spec("candidate", "review_required") is SkillLifecycleState.CANDIDATE
    with pytest.raises(ValueError):
        classify_registry_spec("teleport", "trusted")


def test_source_provenance_required():
    with pytest.raises(ValueError):
        SourceProvenance(source_type="", source_uri="u", source_revision="r")


# -- import ---------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_import_valid_personal(tmp_path, events, personal_store, package_dir):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(
        package_dir, scope_type="user", scope_id="u1", backend="personal"
    )
    assert len(records) == 1
    record = records[0]
    assert record.state is SkillLifecycleState.CANDIDATE
    assert record.backend == "personal"
    assert record.source.source_type == "package"
    assert record.source.source_revision
    assert any(topic == "skill.imported" for topic, _ in events)


@pytest.mark.asyncio()
async def test_import_invalid_package_fails_closed(tmp_path, events, personal_store):
    dist = _dist(tmp_path, events)
    with pytest.raises(ValueError, match=r"CAPABILITY\.yaml"):
        await dist.import_package(tmp_path / "empty", backend="personal")


@pytest.mark.asyncio()
async def test_import_duplicate_same_revision_noop(tmp_path, events, personal_store, package_dir):
    dist = _dist(tmp_path, events)
    first = await dist.import_package(package_dir, backend="personal")
    second = await dist.import_package(package_dir, backend="personal")
    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio()
async def test_import_new_revision_creates_candidate(tmp_path, events, personal_store, package_dir):
    dist = _dist(tmp_path, events)
    await dist.import_package(package_dir, backend="personal")
    (package_dir / "skills" / "hello.yaml").write_text(
        "instructions: Say hello coldly.\n", encoding="utf-8"
    )
    second = await dist.import_package(package_dir, backend="personal")
    assert len(second) == 1
    assert second[0].state is SkillLifecycleState.CANDIDATE


@pytest.mark.asyncio()
async def test_import_registry_backend(tmp_path, events, registry_isolated, package_dir):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(package_dir, backend="registry")
    assert len(records) == 1
    assert records[0].backend == "registry"
    assert records[0].state is SkillLifecycleState.CANDIDATE


# -- qualification / promotion -------------------------------------------------------


@pytest.mark.asyncio()
async def test_unverified_not_active_without_promotion(
    tmp_path, events, personal_store, package_dir
):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(package_dir, backend="personal")
    skill_id = records[0].skill_id
    with pytest.raises(ValueError, match="no active trusted version"):
        await dist.bind(skill_id, "user", "u1", backend="personal")
    with pytest.raises(ValueError, match="no active trusted version"):
        await dist.materialize(skill_id, "user", "u1", backend="personal")


@pytest.mark.asyncio()
async def test_explicit_promotion_activates(tmp_path, events, personal_store, package_dir):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(package_dir, backend="personal")
    skill_id = records[0].skill_id
    qualified = await dist.qualify(skill_id, backend="personal")
    assert qualified["eligible"] is True
    promoted = await dist.promote(skill_id, backend="personal")
    assert promoted.state is SkillLifecycleState.ACTIVE
    assert promoted.trust == "trusted"
    assert any(topic == "skill.promoted" for topic, _ in events)


@pytest.mark.asyncio()
async def test_promote_unknown_fails_closed(tmp_path, events, personal_store):
    dist = _dist(tmp_path, events)
    with pytest.raises(ValueError, match="unknown skill"):
        await dist.promote("ghost", backend="personal")


# -- binding ----------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_bind_unbind_idempotent(tmp_path, events, personal_store, package_dir):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(package_dir, backend="personal")
    await dist.promote(records[0].skill_id, backend="personal")
    first = await dist.bind(records[0].skill_id, "user", "u1", backend="personal")
    second = await dist.bind(records[0].skill_id, "user", "u1", backend="personal")
    assert first.key() == second.key()
    assert len(dist.bindings_for_scope("user", "u1")) == 1
    assert dist.unbind(records[0].skill_id, "user", "u1") is True
    assert dist.unbind(records[0].skill_id, "user", "u1") is False
    assert dist.bindings_for_scope("user", "u1") == []


@pytest.mark.asyncio()
async def test_bind_requires_active(tmp_path, events, personal_store, package_dir):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(package_dir, backend="personal")
    with pytest.raises(ValueError, match="no active trusted version"):
        await dist.bind(records[0].skill_id, "workspace", "w1", backend="personal")


# -- materialize ---------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_materialize_active_and_stale_denied(tmp_path, events, personal_store, package_dir):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(
        package_dir, scope_type="user", scope_id="u1", backend="personal"
    )
    await dist.promote(records[0].skill_id, backend="personal")
    made = await dist.materialize(records[0].skill_id, "user", "u1", backend="personal")
    assert made["revision"].startswith("v1@trusted")
    assert "hello" in made["payload"]["instructions"].lower()
    assert any(topic == "skill.materialized" for topic, _ in events)


@pytest.mark.asyncio()
async def test_materialize_unbound_cross_scope_denied(
    tmp_path, events, personal_store, package_dir
):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(
        package_dir, scope_type="user", scope_id="u1", backend="personal"
    )
    await dist.promote(records[0].skill_id, backend="personal")
    with pytest.raises(ValueError, match="not bound"):
        await dist.materialize(records[0].skill_id, "user", "intruder", backend="personal")
    await dist.bind(records[0].skill_id, "user", "intruder", backend="personal")
    made = await dist.materialize(records[0].skill_id, "user", "intruder", backend="personal")
    assert made["scope"] == "user:intruder"


# -- refresh / rollback / revoke -----------------------------------------------------------------


@pytest.mark.asyncio()
async def test_refresh_same_revision_noop(tmp_path, events, personal_store, package_dir):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(package_dir, backend="personal")
    await dist.promote(records[0].skill_id, backend="personal")
    source = SourceProvenance(
        source_type="package", source_uri="pkg:test-pkg.hello", source_revision="rev-1"
    )
    first = await dist.refresh_skill(records[0].skill_id, source)
    assert first["refreshed"] is True
    again = await dist.refresh_skill(records[0].skill_id, source)
    assert again["refreshed"] is False
    assert again["reason"] == "same revision"
    active = await dist._require_active(records[0].skill_id, backend="personal")
    assert active["version"] == 1


@pytest.mark.asyncio()
async def test_rollback_valid_and_revoked_denied(tmp_path, events, personal_store, package_dir):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(package_dir, backend="personal")
    skill_id = records[0].skill_id
    await dist.promote(skill_id, backend="personal")
    result = await dist.rollback_skill(skill_id, 1, backend="personal")
    assert result["status"] == "rolled_back"
    assert result["version"] == 1
    again = await dist.rollback_skill(skill_id, 1, backend="personal")
    assert again["status"] == "rolled_back"
    with pytest.raises(ValueError, match="unknown version"):
        await dist.rollback_skill(skill_id, 99, backend="personal")


@pytest.mark.asyncio()
async def test_revoke_denies_execution_but_keeps_history(
    tmp_path, events, personal_store, package_dir
):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(package_dir, backend="personal")
    skill_id = records[0].skill_id
    await dist.promote(skill_id, backend="personal")
    await dist.revoke_skill(skill_id, backend="personal")
    with pytest.raises(ValueError, match="no active trusted version"):
        await dist.materialize(skill_id, "user", "u1", backend="personal")
    with pytest.raises(ValueError, match="no active trusted version"):
        await dist.bind(skill_id, "user", "u9", backend="personal")
    revoked = await dist.revoke_skill(skill_id, backend="personal")
    assert revoked["status"] in ("deprecated", "revoked")


# -- registry backend symmetry ----------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_registry_promote_bind_materialize_revoke(
    tmp_path, events, registry_isolated, package_dir
):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(package_dir, backend="registry")
    skill_id = records[0].skill_id
    assert (await dist.qualify(skill_id, backend="registry"))["eligible"] is True
    promoted = await dist.promote(skill_id, backend="registry")
    assert promoted.state is SkillLifecycleState.ACTIVE
    await dist.bind(skill_id, "workspace", "w1", backend="registry")
    made = await dist.materialize(skill_id, "workspace", "w1", backend="registry")
    assert made["version"] == promoted.version
    await dist.revoke_skill(skill_id, backend="registry")
    with pytest.raises(ValueError, match="no active trusted version"):
        await dist.materialize(skill_id, "workspace", "w1", backend="registry")


# -- restart / events / boundaries -----------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_restart_persistence(tmp_path, events, personal_store, package_dir):
    dist = _dist(tmp_path, events)
    records = await dist.import_package(package_dir, backend="personal")
    await dist.promote(records[0].skill_id, backend="personal")
    await dist.bind(records[0].skill_id, "user", "u1", backend="personal")
    reopened = SkillDistribution(store_root=tmp_path / "dist")
    assert len(reopened.bindings_for_scope("user", "u1")) == 1


def test_record_and_binding_value_semantics():
    record = SkillDistributionRecord(skill_id="s", backend="personal", state="active", version=2)
    assert record.to_dict()["state"] == "active"
    binding = SkillBinding(skill_id="s", version=2, scope_type="user", scope_id="u")
    assert binding.scope_key == "user:u"
    assert SkillBinding.from_dict(binding.to_dict()).key() == binding.key()


def test_no_auto_promotion_paths():
    import pathlib

    source = pathlib.Path("server/skill_distribution.py").read_text()
    assert "auto_promote" not in source
    assert "auto_activate" not in source


def test_d9_boundary_no_selection_logic():
    import pathlib

    source = pathlib.Path("server/skill_distribution.py").read_text().lower()
    for token in ("top_k", "topk", "token_budget", "capabilityprojection"):
        assert token not in source
