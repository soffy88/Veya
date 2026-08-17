"""Veya 对 project_eng_gates 的接线：只 +1 工具；不改 project_ask。"""

from __future__ import annotations

import json
from pathlib import Path

from server.project_eng_gates import project_eng_gates
from server.tool_registry import _RESIDENT_TOOLS, MasterToolRegistry


def test_resident_tools_includes_eng_gates_only_as_extra() -> None:
    assert "project_ask" in _RESIDENT_TOOLS
    assert "project_eng_gates" in _RESIDENT_TOOLS
    assert "code_review" not in _RESIDENT_TOOLS
    assert "pre_push_checks" not in _RESIDENT_TOOLS


def test_wire_is_idempotent_and_adds_one_tool() -> None:
    reg = MasterToolRegistry()
    # Isolate: patch the module's master_tools lookup by registering on a fresh registry
    # via the real wire against the process-global registry would pollute other tests.
    from server import project_eng_gates as peg
    from server import tool_registry as tr

    saved = tr.master_tools
    tr.master_tools = reg
    try:
        assert peg.wire_master_tools() == 1
        assert peg.wire_master_tools() == 0
        assert reg.has("project_eng_gates")
        assert reg.list_tools() == ["project_eng_gates"]
        schema = reg.get_all_schemas()[0]["function"]
        assert schema["name"] == "project_eng_gates"
        assert "profile" in schema["parameters"]["properties"]
    finally:
        tr.master_tools = saved


def test_adapter_calls_main_lib_via_platform(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    class _Fake:
        @staticmethod
        def project_eng_gates(project_root, **kwargs):
            captured["project_root"] = project_root
            captured["kwargs"] = kwargs
            return {"ok": True, "profile": kwargs.get("profile"), "pushed": False, "steps": []}

    monkeypatch.setattr("veya.platform.load", lambda lib: _Fake() if lib == "omodul" else None)
    raw = project_eng_gates(str(tmp_path), profile="hygiene", gui_required="false")
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["pushed"] is False
    assert captured["kwargs"]["profile"] == "hygiene"
    assert captured["kwargs"]["gui_required"] is False
