"""ExecutionReport evidence projection — real evidence only, never a self-reported PASS."""

from __future__ import annotations

import subprocess
import types

from veya.supervision.evidence import build_execution_report
from veya.supervision.models import Mission

_SUCCESS_WORDS = {"done", "accepted", "success", "succeeded", "pass", "passed"}


def _state(text: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        goal_id=None, status="executed", tasks={}, final_summary=text, unfinished_work=[]
    )


def _mission(workspace: str) -> Mission:
    return Mission(mission_id="m1", goal="do the thing", workspace=workspace)


def _report(tmp_path, text: str, *, git: bool = False):
    if git:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return build_execution_report(_mission(str(tmp_path)), _state(text), iteration=0)


def test_report_change_projection(tmp_path):
    (tmp_path / "made.txt").write_text("hi", encoding="utf-8")
    report = _report(tmp_path, "Created made.txt and finished.", git=True)
    assert any(entry["path"].endswith("made.txt") for entry in report.changes)
    assert report.git_diff_summary.get("changed", 0) >= 1


def test_report_test_projection(tmp_path):
    report = _report(tmp_path, "$ pytest -q tests/\n12 passed in 3.1s\n")
    assert report.tests, "test output must be projected into tests[]"
    assert report.tests[0]["passed"] == 12


def test_report_artifact_projection(tmp_path):
    (tmp_path / "out.txt").write_text("real", encoding="utf-8")
    report = _report(tmp_path, "Created out.txt and read it back.")
    paths = [entry["path"] for entry in report.artifacts]
    assert "out.txt" in paths
    assert all(entry.get("verified") is True for entry in report.artifacts)


def test_report_failure_projection(tmp_path):
    report = _report(tmp_path, "\u26d4 blocked: dsh not available (binary not found on PATH)")
    assert report.failures, "a blocked line must project into failures[]"
    assert report.blocked_items, "a blocked line must project into blocked_items[]"
    assert report.proposed_next_action == "revise"


def test_report_no_false_evidence(tmp_path):
    report = _report(tmp_path, "Created ghost.txt with the answer. VERDICT: completed")
    assert not any(entry["path"] == "ghost.txt" for entry in report.artifacts), (
        "a file that does not exist must never be reported as an artifact"
    )
    assert any(entry.get("kind") == "artifact_missing" for entry in report.failures)
    # status only ever means 'the executor stopped', never 'accepted'
    assert str(report.status).lower() not in _SUCCESS_WORDS


def test_report_runtime_evidence_and_summary(tmp_path):
    report = _report(tmp_path, "did things without touching files")
    assert report.runtime_evidence and report.executor_summary
    assert str(report.status).lower() not in _SUCCESS_WORDS
