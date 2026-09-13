"""P3 qualification harness entry points."""

from __future__ import annotations

import ast
from pathlib import Path


def test_no_fake_sleep_in_p3_harness() -> None:
    root = Path(__file__).resolve().parents[2] / "evals" / "p3_qualification"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not any(
            isinstance(node, ast.Attribute)
            and node.attr == "sleep"
            for node in ast.walk(tree)
        ), path


def test_p3_harness_defines_formal_duration_gate() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "evals" / "p3_qualification" / "run_qualification.py"
    ).read_text(encoding="utf-8")
    assert "300 <= target <= 600" in source
    assert "run_independent_verifier" in source
    assert "restart_supervisors" in source
