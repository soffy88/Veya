"""Deterministic Acceptance Contract tests (spec §13)."""

from __future__ import annotations

import sys

from server.acceptance import (
    _split_windows_command,
    evaluate_acceptance,
    evaluate_criterion,
    normalize_criteria,
)


def test_file_exists_and_schema_are_deterministic(tmp_path):
    (tmp_path / "result.txt").write_text("ok", encoding="utf-8")
    results = evaluate_acceptance(
        [
            {"id": "file", "type": "file_exists", "path": "result.txt"},
            {
                "id": "schema",
                "type": "schema",
                "value": {"status": "ok"},
                "required_keys": ["status"],
            },
        ],
        workspace=tmp_path,
    )
    assert [item["status"] for item in results] == ["passed", "passed"]


def test_command_uses_argv_without_shell_and_manual_stays_blocked(tmp_path):
    results = evaluate_acceptance(
        [
            {"id": "command", "type": "command", "command": f"{sys.executable} -c 'print(\"ok\")'"},
            {"id": "manual", "type": "manual", "description": "human review"},
        ],
        workspace=tmp_path,
    )
    assert results[0]["status"] == "passed"
    assert results[1]["status"] == "blocked"


def test_windows_command_split_preserves_executable_and_quoted_argument():
    argv = _split_windows_command('"C:\\Program Files\\Python\\python.exe" -c \'print("ok")\'')
    assert argv == [
        r"C:\Program Files\Python\python.exe",
        "-c",
        'print("ok")',
    ]


def test_file_exists_rejects_workspace_escape(tmp_path):
    result = evaluate_criterion(
        {"id": "escape", "type": "file_exists", "path": "../outside.txt"},
        workspace=tmp_path,
    )
    assert result["status"] == "failed"
    assert "outside" in result["evidence"][-1]


def test_normalize_criteria_assigns_stable_ids():
    items = normalize_criteria([{"description": "one"}, {"id": "two", "type": "manual"}])
    assert [item["id"] for item in items] == ["criterion_1", "two"]
