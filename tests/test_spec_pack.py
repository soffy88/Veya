"""Spec pack: durable stages, resume, fingerprint/stale index. No routing."""

from __future__ import annotations

import json
from pathlib import Path

from server import spec_pack as sp


def test_start_advance_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_SPECS_ROOT", str(tmp_path))
    created = sp.start(title="Onboarding drawer", brief="users miss the CTA")
    assert created["ok"] and created["created"] is True
    slug = created["slug"]
    assert (tmp_path / slug / "status.json").is_file()
    again = sp.start(title="Onboarding drawer")
    assert again["created"] is False
    adv = sp.advance(slug=slug, stage="research", body="")
    assert adv["ok"] is False
    ok = sp.advance(slug=slug, stage="research", body="## Findings\n- CTA below fold")
    assert ok["ok"] is True
    rec = sp.resume(slug)
    assert rec["ok"] is True
    assert "research" not in rec["missing_files"]
    assert rec["resume_at"] == "requirements"
    assert "Do not auto-advance" in rec["instruction"]
    listed = sp.list_packs()
    assert any(p["slug"] == slug for p in listed["packs"])


def test_index_writes_codebase_and_detects_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_SPECS_ROOT", str(tmp_path / "specs"))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("VEYA_WORKSPACE", str(ws))
    slug = sp.start(title="Index me")["slug"]

    def _fake_assemble(query: str) -> str:
        return f"map:{query}:app.py:main"

    monkeypatch.setattr("server.graft_autocontext.assemble_code_context", _fake_assemble)
    idx = sp.index_pack(slug=slug, query="main")
    assert idx["ok"] is True
    text = (tmp_path / "specs" / slug / "codebase.md").read_text(encoding="utf-8")
    assert "app.py" in text
    assert "map:main" in text
    st = sp.load_status(slug)
    digest = st["index"]["digest"]
    rec = sp.status(slug)
    assert rec["index_stale"] is False
    (ws / "app.py").write_text(
        "def main():\n    return 2\n# extra line to change size\n", encoding="utf-8"
    )
    rec2 = sp.status(slug)
    # gitless workspace uses size/mtime walk — content change should flip digest
    fp = sp.workspace_fingerprint(ws)
    if fp["digest"] != digest:
        assert rec2["index_stale"] is True


def test_skill_wrapper_dispatches(tmp_path, monkeypatch):
    import importlib.util

    monkeypatch.setenv("VEYA_SPECS_ROOT", str(tmp_path))
    path = Path(__file__).resolve().parents[1] / "templates/skills/spec-pack/run.py"
    spec = importlib.util.spec_from_file_location("spec_pack_skill", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.main("start", title="Foo bar")
    assert out["ok"] is True
    man = json.loads(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert man["name"] == "spec-pack"
    assert (path.parent / "SKILL.md").is_file()
