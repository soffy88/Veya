"""design-shotgun + retro skills: scaffolds only, no routing, persist via Genesis."""

from __future__ import annotations

import json
from pathlib import Path

import importlib.util


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ROOT = Path(__file__).resolve().parents[1]


def test_design_shotgun_scaffold():
    mod = _load("design_shotgun_run", ROOT / "templates/skills/design-shotgun/run.py")
    board = mod.main("shotgun", brief="onboarding")
    assert board["ok"] is True
    assert "user_job" in board["axes"]
    assert len(board["slots"]) == 3
    pick = mod.main(
        "pick",
        options_json=json.dumps(
            [
                {"id": "A", "title": "min", "scores": {"user_job": 3, "risk": 5}},
                {"id": "B", "title": "bold", "scores": {"user_job": 5, "risk": 1}},
            ]
        ),
    )
    assert pick["ok"] is True
    assert pick["winner"]["id"] in {"A", "B"}
    steps = mod.main("html_to_code")
    assert any("hicode_run" in s for s in steps["steps"])
    assert (ROOT / "templates/skills/design-shotgun/SKILL.md").is_file()
    man = json.loads((ROOT / "templates/skills/design-shotgun/manifest.json").read_text())
    assert man["name"] == "design-shotgun"


def test_retro_writes_genesis(tmp_path):
    mod = _load("retro_run", ROOT / "templates/skills/retro/run.py")
    bad = mod.main("record", mistake="x", lesson="", storage_dir=str(tmp_path))
    assert bad["ok"] is False
    ok = mod.main(
        "record",
        mistake="shipped without tests",
        lesson="always add a failing test first",
        storage_dir=str(tmp_path),
    )
    assert ok["ok"] is True
    recent = mod.main("recent", n=3, storage_dir=str(tmp_path))
    assert recent["ok"] is True
    assert any("shipped without tests" in e.get("mistake", "") for e in recent["entries"])
    assert (tmp_path / "experiences.jsonl").is_file()
