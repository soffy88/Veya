"""SKILL_REGISTRY_AUTHORITY_CONVERGENCE tests.

Exactly ONE write authority: ``server.skill_hub.VeyaSkillHub``. The remote
registry is an ADAPTER, the capability registry a PROJECTION, and
``registries.skills`` is LEGACY-DEAD. See ``server.skill_authority``.

Legs that need the real hub executable stack are guarded with
``pytest.importorskip("server.skill_hub")``: they run wherever the 3O
submodules are mounted (CI) and skip with a recorded reason where they are
not. Every other leg runs everywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server import skill_authority
from server.capability_model import (
    SkillRegistry,
    SkillSpec,
    _JsonRegistryStore,
)
from server.skill_authority import (
    CANDIDATE_CLASSIFICATION,
    CANONICAL_SKILL_AUTHORITY,
    SkillAuthorityError,
    canonical_skill_id,
)
from veya.remote.skills import SkillRecord
from veya.remote.skills import SkillRegistry as RemoteSkillRegistry


def _store(tmp_path: Path) -> _JsonRegistryStore:
    return _JsonRegistryStore(storage_path=tmp_path / "registry.json")


def _hub_leg():
    return pytest.importorskip(
        "server.skill_hub", reason="3O submodules unmounted in this worktree"
    )


class _FakeHub:
    """Minimal canonical-shaped hub (public interface only)."""

    skills_dir = "/fake/skills"

    def __init__(self, skills: dict[str, str], paths: dict[str, str] | None = None):
        self._skills_def = dict(skills)
        self._paths = dict(paths or {})

    def get_stats(self):
        return {"skills": sorted(self._skills_def)}

    def describe(self, name):
        return self._skills_def[name]

    def skill_risk(self, name):
        return {"max_severity": "none", "categories": []}

    def skill_source_path(self, name):
        return self._paths.get(name, "")


# ── authority declaration ─────────────────────────────────────────────


def test_canonical_authority_is_declared_exactly_once():
    assert CANONICAL_SKILL_AUTHORITY == "server.skill_hub.VeyaSkillHub"
    assert CANDIDATE_CLASSIFICATION == {
        "server.skill_hub.VeyaSkillHub": "AUTHORITATIVE",
        "veya.remote.skills.SkillRegistry": "ADAPTER",
        "server.capability_model.SkillRegistry": "PROJECTION",
        "registries.skills": "LEGACY-DEAD",
    }
    assert skill_authority.SKILL_AUTHORITY_ROLE_AUTHORITATIVE == "AUTHORITATIVE"
    from veya.remote import skills as remote_skills

    assert remote_skills.SKILL_AUTHORITY_ROLE == "ADAPTER"
    from server import capability_model

    assert capability_model.SKILL_AUTHORITY_ROLE == "PROJECTION"
    import registries.skills as dead

    assert dead.SKILL_AUTHORITY_ROLE == "LEGACY-DEAD"
    assert dead._DEACTIVATED is True


# ── canonical identity ────────────────────────────────────────────────


def test_canonical_skill_identity_valid_and_normalized():
    assert canonical_skill_id("  weather  ") == "weather"
    assert canonical_skill_id("brief.render") == "brief.render"
    assert canonical_skill_id("my-skill_2") == "my-skill_2"


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "a\\b", ".", "..", "x" * 129, "a\x00b"])
def test_canonical_skill_identity_rejects(bad):
    with pytest.raises(SkillAuthorityError):
        canonical_skill_id(bad)


# ── remote ADAPTER ────────────────────────────────────────────────────


def _record(tmp_path: Path, skill_id: str = "conv-skill") -> SkillRecord:
    root = tmp_path / skill_id
    (root / "references").mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text("# Workflow\nconv step\n", encoding="utf-8")
    (root / "references" / "notes.md").write_text("notes", encoding="utf-8")
    return SkillRecord(
        skill_id=skill_id,
        name="Conv Skill",
        description="convergence probe",
        triggers=["conv", "probe"],
        permissions_required=["filesystem.read"],
        root=str(root),
    )


def test_remote_projection_lookup_and_progressive_loading(tmp_path: Path):
    registry = RemoteSkillRegistry()
    assert registry.sync_from_canonical([_record(tmp_path)]) == 1
    assert registry.get("conv-skill") is not None
    assert registry.get("conv-skill").skill_id == "conv-skill"  # no second identity
    metadata = registry.metadata()
    assert "conv step" not in str(metadata)  # Level 1 only
    assert "conv step" in registry.load_core("conv-skill")  # Level 2 on demand
    assert "conv step" in registry.loaded_core("conv-skill")
    resources = registry.load_resources("conv-skill")  # Level 3 on demand
    assert any("notes.md" in path for path in resources)
    resolution = registry.resolve("run the conv probe")
    assert "conv-skill" in resolution["candidates"]
    ok, _ = registry.check_permissions("conv-skill", ["filesystem.read"])
    assert ok is True


def test_remote_direct_register_is_rejected(tmp_path: Path):
    registry = RemoteSkillRegistry()
    with pytest.raises(SkillAuthorityError):
        registry.register(_record(tmp_path))
    assert registry.get("conv-skill") is None  # rejected write stored nothing


def test_remote_sync_rejects_bad_identity(tmp_path: Path):
    registry = RemoteSkillRegistry()
    bad = _record(tmp_path, skill_id="nope/bad")
    with pytest.raises(SkillAuthorityError):
        registry.sync_from_canonical([bad])
    assert registry.metadata() == []


def test_remote_sync_is_idempotent_no_fork(tmp_path: Path):
    registry = RemoteSkillRegistry()
    registry.sync_from_canonical([_record(tmp_path)])
    updated = _record(tmp_path)
    updated.description = "second sync wins"
    registry.sync_from_canonical([updated])
    assert len(registry.metadata()) == 1
    assert registry.get("conv-skill").description == "second sync wins"


async def test_remote_adapter_compatibility_with_execution_builder(tmp_path: Path):
    from veya.remote.execution_context import ExecutionContextBuilder

    registry = RemoteSkillRegistry()
    registry.sync_from_canonical([_record(tmp_path)])
    builder = ExecutionContextBuilder(
        skill_registry=registry,
        permission_policy={"allowed": ["filesystem.read"]},
    )
    context, evidence = await builder.build(
        mission_id="m1",
        execution_id="ex_1",
        worker_type="hicode",
        workspace=str(tmp_path),
        task_text="run the conv probe",
    )
    assert evidence["selected_skills"] == ["conv-skill"]
    assert "conv step" in context.selected_skills[0]["level2"]


# ── capability PROJECTION ─────────────────────────────────────────────


def test_projection_lookup_after_delegated_sync(tmp_path: Path):
    from server.skill_authority import sync_capability_projection

    registry = SkillRegistry(_store(tmp_path))
    hub = _FakeHub({"conv-skill": "does conv things"})
    assert sync_capability_projection(hub, registry=registry) == 1
    spec = registry.get_version("conv-skill")
    assert spec is not None
    assert spec.skill_id == "conv-skill"  # identity reused, never re-minted
    assert "does conv things" in spec.instructions
    assert registry.get("conv-skill").skill_id == "conv-skill"


def test_projection_mutation_only_through_canonical(tmp_path: Path):
    registry = SkillRegistry(_store(tmp_path))
    with pytest.raises(SkillAuthorityError):
        registry.register_candidate(SkillSpec(skill_id="rogue", instructions="x"))
    with pytest.raises(SkillAuthorityError):
        registry.propose_skill("rogue teaching")
    assert registry.get_version("rogue") is None

    # Delegated paths succeed.
    skill_authority.stage_skill_spec(
        registry, SkillSpec(skill_id="pkg.skill", instructions="imported")
    )
    assert registry.get_version("pkg.skill") is not None
    taught = skill_authority.propose_teaching_candidate(registry, "teach me conv")
    assert registry.get_version(taught.skill_id) is not None
    # Staged candidates never leak into the default contract query.
    assert registry.query() == []


def test_projection_duplicate_registration_last_write_wins(tmp_path: Path):
    registry = SkillRegistry(_store(tmp_path))
    registry.sync_from_canonical([SkillSpec(skill_id="dup", instructions="v1")])
    registry.sync_from_canonical([SkillSpec(skill_id="dup", instructions="v2")])
    assert registry.get_version("dup").instructions == "v2"


def test_projection_persists_across_restart(tmp_path: Path):
    path = tmp_path / "registry.json"
    registry = SkillRegistry(_JsonRegistryStore(storage_path=path))
    registry.sync_from_canonical([SkillSpec(skill_id="kept", instructions="stay")])
    reloaded = SkillRegistry(_JsonRegistryStore(storage_path=path))
    assert reloaded.get_version("kept") is not None
    assert reloaded.get_version("kept").instructions == "stay"


def test_projection_sync_bridge_uses_delegated_path(tmp_path: Path, monkeypatch):
    import server.capability_model as cm

    fresh = SkillRegistry(_store(tmp_path))
    monkeypatch.setattr(cm, "skill_registry", fresh)
    count = cm.sync_skills_from_hub(_FakeHub({"a": "does a", "b": "does b"}))
    assert count == 2
    assert fresh.get_version("a") is not None
    assert fresh.get_version("b") is not None


# ── LEGACY-DEAD ───────────────────────────────────────────────────────


def test_dead_registry_stays_deactivated():
    import registries.skills as dead

    with pytest.raises(RuntimeError):
        dead.register_skill("x", lambda: 1)
    with pytest.raises(RuntimeError):
        dead.get_skill("x")
    with pytest.raises(RuntimeError):
        dead.list_skills()


# ── canonical hub legs (need the real executable stack) ───────────────


def _write_hub_skill(skills_dir: Path, name: str, body: str = "") -> None:
    pkg = skills_dir / name
    pkg.mkdir(parents=True, exist_ok=True)
    import json

    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": f"{name} does things",
                "parameters": {"type": "object", "properties": {"goal": {"type": "string"}}},
            }
        ),
        encoding="utf-8",
    )
    (pkg / "run.py").write_text(
        "async def main(goal='', **kwargs):\n    return f'done:{goal}'\n",
        encoding="utf-8",
    )
    if body:
        # Real oskill progressive loading extracts the body only when the
        # SKILL.md carries a YAML frontmatter block — mirror that contract.
        (pkg / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} guide\n---\n{body}",
            encoding="utf-8",
        )


def test_canonical_registration_and_lookup(tmp_path: Path, monkeypatch):
    hub_mod = _hub_leg()
    monkeypatch.setenv("VEYA_SKILL_DISPATCHER", "0")
    skills_dir = tmp_path / "skills"
    _write_hub_skill(skills_dir, "conv-echo")
    hub = hub_mod.VeyaSkillHub(skills_dir=skills_dir)
    assert hub.has("conv-echo")
    assert any(s["function"]["name"] == "conv-echo" for s in hub.get_all_schemas())
    assert hub.skill_source_path("conv-echo") != ""


async def test_canonical_execute_and_progressive_loading(tmp_path: Path, monkeypatch):
    hub_mod = _hub_leg()
    monkeypatch.setenv("VEYA_SKILL_DISPATCHER", "0")
    skills_dir = tmp_path / "skills"
    _write_hub_skill(skills_dir, "conv-echo", body="# Conv Guide\nconv step\n")
    hub = hub_mod.VeyaSkillHub(skills_dir=skills_dir)
    assert hub._skills["conv-echo"]["has_body"] is True
    out = await hub.execute("conv-echo", {"goal": "hi"})
    assert "done:hi" in out
    routed = hub._route_skills("conv task", 3)
    assert any("conv step" in str(entry) for entry in routed)


def test_canonical_duplicate_and_restart(tmp_path: Path, monkeypatch):
    hub_mod = _hub_leg()
    monkeypatch.setenv("VEYA_SKILL_DISPATCHER", "0")
    skills_dir = tmp_path / "skills"
    _write_hub_skill(skills_dir, "conv-echo")
    hub = hub_mod.VeyaSkillHub(skills_dir=skills_dir)
    assert hub.has("conv-echo")
    # Rewrite + reload: last writer wins, single identity, no fork.
    _write_hub_skill(skills_dir, "conv-echo")
    stats = hub.reload_skills()
    assert stats["loaded"] == 1
    assert hub.has("conv-echo")
    # Restart: a fresh instance over the same dir sees the same skill.
    hub2 = hub_mod.VeyaSkillHub(skills_dir=skills_dir)
    assert hub2.has("conv-echo")
    assert hub2.describe("conv-echo") == hub.describe("conv-echo")


def test_canonical_identity_rejects_bad_manifest(tmp_path: Path, monkeypatch):
    hub_mod = _hub_leg()
    monkeypatch.setenv("VEYA_SKILL_DISPATCHER", "0")
    import json

    skills_dir = tmp_path / "skills"
    bad = skills_dir / "evil"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text(
        json.dumps(
            {
                "name": "../evil",
                "description": "path escape",
                "parameters": {"type": "object", "properties": {}},
            }
        ),
        encoding="utf-8",
    )
    hub = hub_mod.VeyaSkillHub(skills_dir=skills_dir)
    assert not hub.has("../evil")
    assert hub.get_stats()["loaded"] == 0


async def test_end_to_end_authority_chain(tmp_path: Path, monkeypatch):
    """register → canonical → persistence → projection/adapter → lookup."""
    hub_mod = _hub_leg()
    monkeypatch.setenv("VEYA_SKILL_DISPATCHER", "0")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    from server.skill_authority import (
        register_canonical_skill,
        sync_capability_projection,
        sync_remote_adapter,
    )

    skill_id = register_canonical_skill(
        name="conv-chain",
        description="chain probe",
        parameters={"type": "object", "properties": {"goal": {"type": "string"}}},
        skills_dir=skills_dir,
        code="async def main(goal='', **kwargs):\n    return f'chain:{goal}'\n",
    )
    assert skill_id == "conv-chain"
    assert (skills_dir / "conv-chain" / "manifest.json").is_file()  # persisted

    hub = hub_mod.VeyaSkillHub(skills_dir=skills_dir)
    assert hub.has("conv-chain")
    assert "chain:go" in await hub.execute("conv-chain", {"goal": "go"})

    projection = SkillRegistry(_store(tmp_path))
    assert sync_capability_projection(hub, registry=projection) == 1
    assert projection.get_version("conv-chain") is not None

    adapter = RemoteSkillRegistry()
    assert sync_remote_adapter(adapter, hub) == 1
    assert adapter.get("conv-chain") is not None
    assert adapter.get("conv-chain").skill_id == "conv-chain"  # identity stable

    # Secondary writes stay rejected after the chain ran.
    with pytest.raises(SkillAuthorityError):
        projection.register_candidate(SkillSpec(skill_id="rogue", instructions="x"))
    with pytest.raises(SkillAuthorityError):
        adapter.register(SkillRecord(skill_id="rogue", name="rogue", description="rogue"))
