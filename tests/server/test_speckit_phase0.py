"""Spec Kit (SDD) Phase 0 tests: flag-off byte identity + flag-on discovery.

No network, no spawned process, no LLM. The upstream templates are the vendored
copies under ``templates/speckit/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server import (
    feature_flags,
    speckit_phases,
    speckit_skills,
    speckit_workspace,
    tool_registry,
)
from server.tool_registry import MasterToolRegistry, master_tools

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(speckit_phases.FLAG_NAME, raising=False)
    # The external corpora must stay out of these assertions.
    monkeypatch.delenv("VEYA_BUGHUNTER_INDEX", raising=False)
    monkeypatch.delenv("VEYA_WSHOBSON_INDEX", raising=False)
    yield


def _schema_bytes(schemas: list[dict[str, Any]]) -> bytes:
    return json.dumps(schemas, ensure_ascii=False, separators=(",", ":")).encode()


# ── flag registry ───────────────────────────────────────────────────────────


def test_flag_registered_at_table_tail_default_off() -> None:
    names = [spec.name for spec in feature_flags.FLAGS]
    assert speckit_phases.FLAG_NAME in names
    assert names[-1] == speckit_phases.FLAG_NAME
    spec = next(item for item in feature_flags.FLAGS if item.name == speckit_phases.FLAG_NAME)
    assert spec.default is False
    assert speckit_phases.enabled() is False


def test_flag_enabled_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    assert speckit_phases.enabled() is True


# ── flag off: tool surface is byte-identical ────────────────────────────────


def test_flag_off_search_tools_payload_unchanged() -> None:
    before = tool_registry._lazy_search_tools("specify a feature", top_k=5)
    payload = json.loads(before)
    assert "speckit_skills" not in payload
    assert "Spec Kit" not in payload["note"]


def test_flag_off_registers_no_tool_and_global_surface_unchanged() -> None:
    before = _schema_bytes(master_tools.get_all_schemas())
    assert speckit_phases.phase_templates() == {}
    assert speckit_phases.workflows() == ()
    assert speckit_skills.index.count() == 0
    assert speckit_skills.search_speckit_skills("specify") == []
    assert _schema_bytes(master_tools.get_all_schemas()) == before


def test_flag_off_injected_registry_unchanged() -> None:
    registry = MasterToolRegistry()
    registry.register("alpha", "alpha", {"type": "object", "properties": {}}, lambda: "ok")
    before = _schema_bytes(registry.get_all_schemas())
    assert speckit_workspace.init_workspace("/tmp/definitely-not-a-speckit-project") == {
        "ok": False,
        "error": f"flag disabled ({speckit_phases.FLAG_NAME})",
    }
    assert _schema_bytes(registry.get_all_schemas()) == before


# ── flag on: discovery ──────────────────────────────────────────────────────


def test_flag_on_search_tools_lists_templates() -> None:
    assert len(list((REPO_ROOT / "templates/speckit/commands").glob("*.md"))) == 10
    assert (REPO_ROOT / "templates/speckit/spec-template.md").is_file()


def test_flag_on_search_speckit_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    hits = speckit_skills.search_speckit_skills("specify")
    assert hits
    assert hits[0]["name"] == "specify"
    assert hits[0]["source"] == "speckit"
    assert Path(hits[0]["path"]).is_file()
    body = speckit_skills.index.read(hits[0]["name"])
    assert body and "User Input" in body


def test_flag_on_search_tools_payload_carries_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    payload = json.loads(tool_registry._lazy_search_tools("specify", top_k=5))
    assert payload["speckit_skills"]
    assert payload["speckit_skills"][0]["name"] == "specify"
    assert "speckit_skills" in payload["note"]


def test_flag_on_index_is_read_only_and_matches_vendored_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    entries = speckit_skills.index.entries()
    commands = [e for e in entries if e.kind == "command"]
    artifacts = [e for e in entries if e.kind == "artifact"]
    assert len(commands) == 10
    assert len(artifacts) == 5
    assert all(e.description for e in entries)


# ── workspace bootstrap ─────────────────────────────────────────────────────


def test_init_workspace_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    first = speckit_workspace.init_workspace(tmp_path)
    assert first["ok"] is True
    assert first["created"] == ["constitution.md"]
    assert (tmp_path / ".speckit" / "constitution.md").is_file()

    (tmp_path / ".speckit" / "constitution.md").write_text("ours\n", encoding="utf-8")
    second = speckit_workspace.init_workspace(tmp_path)
    assert second["created"] == []
    assert second["existing"] == ["constitution.md"]
    assert (tmp_path / ".speckit" / "constitution.md").read_text(encoding="utf-8") == "ours\n"


def test_init_workspace_seeds_upstream_constitution_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    speckit_workspace.init_workspace(tmp_path)
    seeded = (tmp_path / ".speckit" / "constitution.md").read_text(encoding="utf-8")
    upstream = (REPO_ROOT / "templates/speckit/constitution-template.md").read_text(
        encoding="utf-8"
    )
    assert seeded == upstream


def test_workspace_status_is_readable_with_flag_off(tmp_path: Path) -> None:
    rec = speckit_workspace.status(tmp_path)
    assert rec["available"] is False
    assert speckit_phases.FLAG_NAME in rec["reason"]
    assert rec["exists"] is False
    assert rec["ssot_ready"] is False


def test_workspace_status_reports_ssot_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    speckit = tmp_path / ".speckit"
    speckit.mkdir()
    (speckit / "tasks.md").write_text("- [ ] T1: x\n", encoding="utf-8")
    rec = speckit_workspace.status(tmp_path)
    assert rec["artifacts"]["tasks.md"] is True
    assert rec["artifacts"]["constitution.md"] is False
    assert rec["ssot_ready"] is False
    assert rec["next_artifact"] == "constitution.md"

    (speckit / "constitution.md").write_text("rules\n", encoding="utf-8")
    rec = speckit_workspace.status(tmp_path)
    assert rec["ssot_ready"] is True
    # spec/plan/checklist are still unwritten, so the pipeline continues there.
    assert rec["next_artifact"] == "spec.md"

    for name in ("spec.md", "plan.md", "checklist.md"):
        (speckit / name).write_text("x\n", encoding="utf-8")
    assert speckit_workspace.status(tmp_path)["next_artifact"] == ""


def test_copy_template_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    target = tmp_path / ".speckit" / "plan.md"
    assert speckit_workspace.copy_template("plan-template.md", target)["copied"] is True
    target.write_text("edited\n", encoding="utf-8")
    again = speckit_workspace.copy_template("plan-template.md", target)
    assert again["copied"] is False
    assert target.read_text(encoding="utf-8") == "edited\n"


def test_copy_template_rejects_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    rec = speckit_workspace.copy_template("nope.md", tmp_path / "nope.md")
    assert rec["ok"] is False
    assert "template not found" in rec["error"]


# ── phase templates ─────────────────────────────────────────────────────────


def test_phase_templates_cover_four_pipelines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    templates = speckit_phases.phase_templates()
    assert set(templates) == {"speckit", "bug", "assess"}
    assert [s["phase"] for s in templates["speckit"]] == [
        "constitution",
        "specify",
        "plan",
        "tasks",
        "implement",
        "converge",
    ]
    assert [s["phase"] for s in templates["bug"]] == ["assess", "fix", "test"]
    assert [s["phase"] for s in templates["assess"]] == [
        "intake",
        "research",
        "define",
        "shape",
        "decide",
    ]


def test_phase_template_rejects_unknown_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    assert speckit_phases.phase_template("nope") == ()
    assert speckit_phases.render_phase_instruction("nope", "x") == ""


def test_render_phase_instruction_points_at_artifact_and_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    text = speckit_phases.render_phase_instruction(
        "speckit", "tasks", slug="login-timeout", goal_text="ship login"
    )
    assert "Spec Kit stage: speckit / tasks" in text
    assert "Artifact: .speckit/tasks.md" in text
    assert "Upstream template: tasks.md" in text
    assert "checkbox" in text
    assert "Goal: ship login" in text


def test_render_phase_instruction_substitutes_slug_for_pipelines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    text = speckit_phases.render_phase_instruction("bug", "test", slug="login-timeout")
    assert "Artifact: .speckit/bugs/login-timeout/verification.md" in text
    text = speckit_phases.render_phase_instruction("assess", "decide", slug="cache")
    assert "Artifact: .speckit/assess/cache/decision.md" in text


def test_render_phase_instruction_empty_when_disabled() -> None:
    assert speckit_phases.render_phase_instruction("speckit", "tasks") == ""


def test_status_reports_flag_reason() -> None:
    assert speckit_phases.status()["reason"].startswith("unavailable")
    assert speckit_workspace.status("/tmp")["reason"].startswith("flag disabled")
    assert speckit_skills.status()["count"] == 0


def test_status_reports_pipelines_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(speckit_phases.FLAG_NAME, "1")
    rec = speckit_phases.status()
    assert rec["available"] is True
    assert rec["workflows"] == ["speckit", "bug", "assess"]
    assert rec["stages"]["bug"] == ["assess", "fix", "test"]
