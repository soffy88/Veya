"""P0-1: the legacy coordinator is not a production authority."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _imports_legacy(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        if any(
            name == "server.coordinator" or name.startswith("server.coordinator.") for name in names
        ):
            return True
    return False


def test_no_production_imports_legacy_coordinator():
    roots = (ROOT / "server", ROOT / "runtime", ROOT / "veya")
    hits = [
        str(path.relative_to(ROOT))
        for base in roots
        for path in base.rglob("*.py")
        if path.name != "coordinator.py"
        and "__pycache__" not in path.parts
        and _imports_legacy(path)
    ]
    assert hits == []


def test_legacy_module_is_explicitly_archive_only():
    source = (ROOT / "server/coordinator.py").read_text(encoding="utf-8")
    assert "ARCHIVE_ONLY = True" in source


def test_flow_phase1_is_workflow_plane_without_legacy_react():
    source = (ROOT / "server/routes/flow.py").read_text(encoding="utf-8")
    assert "propose_requirement" in source
    assert "server.coordinator" not in source


def test_agent_invoke_uses_canonical_master():
    source = (ROOT / "server/routes/agent.py").read_text(encoding="utf-8")
    assert "master_coordinator.chat_stream" in source
    assert "assemble_main_agent" not in source
