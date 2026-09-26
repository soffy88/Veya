"""D7 gates: capability authoring pipeline over frozen authorities.

End-to-end on real substrates (isolated registry JSON, tmp workspaces,
real ArtifactStore, real D4 lifecycle, real D6 materialize gate): draft,
isolated compile, candidate registration, qualification, explicit
promotion, tamper blocking, supersede, rejection, revocation, restart,
idempotency, D6/D9 boundaries. No skips, no swallowed exceptions.
"""

from __future__ import annotations

import pytest

from server.capability_authoring import (
    CandidateArtifact,
    CapabilityAuthoring,
    CapabilityDraft,
)
from server.capability_model import SkillRegistry, SkillSpec, _JsonRegistryStore


@pytest.fixture()
def events() -> list[tuple[str, dict]]:
    return []


def _emit(events: list[tuple[str, dict]]):
    def _record(topic: str, payload: dict) -> None:
        events.append((topic, payload))

    return _record


@pytest.fixture()
def isolated_registry(tmp_path, monkeypatch):
    from server import capability_model

    isolated = SkillRegistry(_JsonRegistryStore(tmp_path / "cap.json"))
    monkeypatch.setattr(capability_model, "skill_registry", isolated)
    return isolated


@pytest.fixture()
def authoring(tmp_path, events, isolated_registry, monkeypatch):
    from server import capability_model

    registry = capability_model.CapabilityRegistry(
        capability_model._JsonRegistryStore(tmp_path / "caps.json")
    )
    return CapabilityAuthoring(
        tmp_path / "author",
        emit=_emit(events),
        registry=registry,
        skill_registry=isolated_registry,
    ), registry


def _approve_skill(isolated_registry, skill_id: str = "greet") -> None:
    isolated_registry.register_candidate(
        SkillSpec(skill_id=skill_id, instructions="Say hello warmly.")
    )
    confirmed = isolated_registry.confirm_skill(skill_id)
    assert confirmed is not None and confirmed.status == "verified"


def _draft(authoring, **kwargs):
    params = {
        "requirement": "Greet users warmly.",
        "capability_id": "greeting",
        "skills": ["greet"],
        "domain": "hospitality",
        "evaluators": ["greet-smoke"],
        "benchmark_suite": "greet-bench",
    }
    params.update(kwargs)
    return authoring.create_draft(**params)


def _ws(authoring_root, tmp_path):
    managed = tmp_path / "managed"
    (managed / "ws").mkdir(parents=True)
    return managed / "ws", managed


# -- draft / compile ---------------------------------------------------------------


def test_draft_records_requirement(authoring, events):
    pipeline, _ = authoring
    draft = _draft(pipeline)
    assert draft.status == "draft"
    assert draft.skills == ("greet",)
    assert any(topic == "capability.drafted" for topic, _ in events)
    with pytest.raises(ValueError):
        pipeline.create_draft("", "cap-x")


def test_compile_isolated_and_candidate_registered(authoring, tmp_path, events):
    pipeline, registry = authoring
    draft = _draft(pipeline)
    ws_root, managed = _ws(tmp_path, tmp_path)
    artifact = pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    assert artifact.version == 1
    assert artifact.package_hash
    assert len(artifact.files) == 2
    assert artifact.workspace_id == f"ws-{draft.draft_id}"
    assert registry.get("greeting") is not None
    assert registry.get("greeting").status == "candidate"
    assert any(topic == "capability.compiled" for topic, _ in events)
    # Artifact bytes live hash-addressed in the authoring store; the D4
    # workspace record proves the isolated compile context.
    stored = tmp_path / "author" / ".veya" / "runs" / f"cap-{draft.draft_id}"
    assert (stored / "package" / "greeting" / "CAPABILITY.yaml").is_file()
    from runtime.coding.workspace_lifecycle import WorkspaceStore

    assert (
        WorkspaceStore(tmp_path / "author" / "workspaces").get(f"ws-{draft.draft_id}") is not None
    )


def test_compile_unknown_draft_fails_closed(authoring, tmp_path):
    pipeline, _ = authoring
    ws_root, managed = _ws(tmp_path, tmp_path)
    with pytest.raises(ValueError, match="unknown draft"):
        pipeline.compile_draft("ghost", workspace_root=ws_root, managed_root=managed)


def test_compile_workspace_escape_denied(authoring, tmp_path):
    pipeline, _ = authoring
    draft = _draft(pipeline)
    with pytest.raises(ValueError, match="escapes managed root"):
        pipeline.compile_draft(
            draft.draft_id,
            workspace_root=tmp_path / "elsewhere",
            managed_root=tmp_path / "managed",
        )


# -- qualification ----------------------------------------------------------------------


async def test_qualification_full_chain(authoring, tmp_path, isolated_registry, events):
    pipeline, registry = authoring
    _approve_skill(isolated_registry)
    draft = _draft(pipeline)
    ws_root, managed = _ws(tmp_path, tmp_path)
    artifact = pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    outcome = await pipeline.qualify_capability("greeting")
    assert outcome == {
        "capability_id": "greeting",
        "qualified": True,
        "reasons": ["verified"],
        "artifact_id": artifact.artifact_id,
    }
    assert registry.get("greeting").status == "verified"
    assert any(topic == "capability.qualified" for topic, _ in events)


async def test_qualification_missing_evaluators_keeps_evidence(
    authoring, tmp_path, isolated_registry
):
    pipeline, registry = authoring
    _approve_skill(isolated_registry)
    draft = _draft(pipeline, evaluators=[], benchmark_suite=None)
    ws_root, managed = _ws(tmp_path, tmp_path)
    artifact = pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    outcome = await pipeline.qualify_capability("greeting")
    assert outcome["qualified"] is False
    assert registry.get("greeting").status == "candidate"
    stored = pipeline._load_state()["artifacts"][artifact.artifact_id]
    assert stored["qualification"]["passed"] is False


async def test_qualification_untrusted_skill_blocks(authoring, tmp_path, isolated_registry):
    pipeline, _ = authoring
    isolated_registry.register_candidate(SkillSpec(skill_id="shady", instructions="Do things."))
    draft = _draft(pipeline, skills=["shady"])
    ws_root, managed = _ws(tmp_path, tmp_path)
    pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    outcome = await pipeline.qualify_capability("greeting")
    assert outcome["qualified"] is False
    assert any("shady" in reason for reason in outcome["reasons"])


async def test_qualification_tamper_detected(authoring, tmp_path, isolated_registry):
    pipeline, _ = authoring
    _approve_skill(isolated_registry)
    draft = _draft(pipeline)
    ws_root, managed = _ws(tmp_path, tmp_path)
    artifact = pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    target = (
        tmp_path
        / "author"
        / ".veya"
        / "runs"
        / f"cap-{draft.draft_id}"
        / "package"
        / "greeting"
        / "CAPABILITY.yaml"
    )
    target.write_text("tampered: true\n", encoding="utf-8")
    outcome = await pipeline.qualify_capability("greeting")
    assert outcome["qualified"] is False
    assert any("tamper" in reason for reason in outcome["reasons"])
    assert outcome["artifact_id"] == artifact.artifact_id


async def test_qualify_unknown_capability(authoring):
    pipeline, _ = authoring
    outcome = await pipeline.qualify_capability("ghost")
    assert outcome == {
        "capability_id": "ghost",
        "qualified": False,
        "reasons": ["not registered"],
    }


# -- promotion -----------------------------------------------------------------------


async def test_promotion_explicit_and_tamper_blocked(
    authoring, tmp_path, isolated_registry, events
):
    pipeline, _registry = authoring
    _approve_skill(isolated_registry)
    draft = _draft(pipeline)
    ws_root, managed = _ws(tmp_path, tmp_path)
    artifact = pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    with pytest.raises(ValueError, match="not VERIFIED"):
        await pipeline.promote_capability("greeting")
    assert (await pipeline.qualify_capability("greeting"))["qualified"] is True
    promoted = await pipeline.promote_capability("greeting", by="test")
    assert promoted == {
        "capability_id": "greeting",
        "status": "promoted",
        "artifact_id": artifact.artifact_id,
        "version": 1,
    }
    assert any(topic == "capability.promoted" for topic, _ in events)
    visible = await pipeline.runtime_capabilities()
    assert [item["capability_id"] for item in visible] == ["greeting"]
    assert visible[0]["package_hash"] == artifact.package_hash

    target = (
        tmp_path
        / "author"
        / ".veya"
        / "runs"
        / f"cap-{draft.draft_id}"
        / "package"
        / "greeting"
        / "SKILL_REFS.md"
    )
    target.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tamper"):
        await pipeline.promote_capability("greeting")
    assert [item["capability_id"] for item in await pipeline.runtime_capabilities()] == []


async def test_promote_unverified_refused(authoring, tmp_path):
    pipeline, _ = authoring
    draft = _draft(pipeline)
    ws_root, managed = _ws(tmp_path, tmp_path)
    pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    with pytest.raises(ValueError, match="not VERIFIED"):
        await pipeline.promote_capability("greeting")


def test_supersede_chain(authoring, tmp_path, isolated_registry):
    pipeline, _ = authoring
    _approve_skill(isolated_registry)
    draft = _draft(pipeline)
    ws_root, managed = _ws(tmp_path, tmp_path)
    first = pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    second = pipeline.compile_draft(
        draft.draft_id,
        workspace_root=ws_root,
        managed_root=managed,
        supersedes=first.artifact_id,
    )
    assert second.version == 2
    assert second.supersedes == first.artifact_id


# -- rejection / revocation -----------------------------------------------------------------


def test_reject_keeps_candidate_and_evidence(authoring, tmp_path, events):
    pipeline, registry = authoring
    draft = _draft(pipeline)
    ws_root, managed = _ws(tmp_path, tmp_path)
    pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    result = pipeline.reject_capability("greeting", reason="not needed")
    assert result["status"] == "rejected"
    assert registry.get("greeting").status == "deprecated"
    assert any(topic == "capability.rejected" for topic, _ in events)


async def test_revoke_unbinds_and_hides_but_keeps_history(
    authoring, tmp_path, isolated_registry, events
):
    pipeline, registry = authoring
    _approve_skill(isolated_registry)
    draft = _draft(pipeline)
    ws_root, managed = _ws(tmp_path, tmp_path)
    pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    await pipeline.qualify_capability("greeting")
    await pipeline.promote_capability("greeting")
    assert await pipeline.runtime_capabilities()
    result = pipeline.revoke_capability("greeting", reason="incident")
    assert result["status"] == "revoked"
    assert await pipeline.runtime_capabilities() == []
    assert registry.get("greeting").status == "deprecated"
    assert any(topic == "capability.revoked" for topic, _ in events)
    state = pipeline._load_state()
    assert state["promotions"]["greeting"]["revoked_at"] is not None
    assert state["promotions"]["greeting"]["artifact_id"] is not None


# -- restart / idempotency / boundaries -----------------------------------------------------------------


async def test_restart_preserves_promotions(authoring, tmp_path, isolated_registry):
    pipeline, _ = authoring
    _approve_skill(isolated_registry)
    draft = _draft(pipeline)
    ws_root, managed = _ws(tmp_path, tmp_path)
    pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    await pipeline.qualify_capability("greeting")
    await pipeline.promote_capability("greeting")
    reopened = CapabilityAuthoring(tmp_path / "author", registry=pipeline._registry())
    assert [item["capability_id"] for item in await reopened.runtime_capabilities()] == ["greeting"]


async def test_promote_twice_idempotent(authoring, tmp_path, isolated_registry):
    pipeline, _ = authoring
    _approve_skill(isolated_registry)
    draft = _draft(pipeline)
    ws_root, managed = _ws(tmp_path, tmp_path)
    pipeline.compile_draft(draft.draft_id, workspace_root=ws_root, managed_root=managed)
    await pipeline.qualify_capability("greeting")
    first = await pipeline.promote_capability("greeting")
    second = await pipeline.promote_capability("greeting")
    assert first["artifact_id"] == second["artifact_id"]
    assert first["version"] == second["version"]


def test_value_objects_validate():
    with pytest.raises(ValueError):
        CapabilityDraft(draft_id=" ", requirement="r", capability_id="c")
    artifact = CandidateArtifact(
        artifact_id="a", draft_id="d", capability_id="c", version=1, package_hash="h"
    )
    assert artifact.to_dict()["package_hash"] == "h"


def test_d9_boundary_no_selection_logic():
    import pathlib

    source = pathlib.Path("server/capability_authoring.py").read_text().lower()
    for token in (
        "top_k",
        "topk",
        "token_budget",
        "capabilityprojection",
        "semantic rout",
        "ranking",
    ):
        assert token not in source


def test_no_auto_transitions_in_source():
    import pathlib

    source = pathlib.Path("server/capability_authoring.py").read_text()
    assert "auto_promote" not in source
    assert "auto_verify" not in source
