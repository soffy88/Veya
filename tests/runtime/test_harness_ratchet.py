from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runtime.coding.workspace_detect import detect_workspace
from runtime.harness.guides import load_guides
from runtime.harness.models import Sensor
from runtime.harness.ratchet import (
    RatchetError,
    RatchetStore,
    apply_candidate,
    create_ratchet_candidate,
    record_candidate,
    transition_candidate,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "harness@example.invalid")
    _git(root, "config", "user.name", "Harness Tests")
    (root / ".gitignore").write_text(".veya/\n", encoding="utf-8")
    (root / "README.md").write_text("harness\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def _guide_candidate(root: Path):
    return create_ratchet_candidate(
        workspace_id=detect_workspace(root).id,
        source_task_id="task-1",
        failure_class="acceptance_failed",
        observed_failure="required test was not run",
        proposed_fix_layer="guide",
        proposed_rule="Always run pytest before finalizing.",
        evidence_ids=["e-failure-1"],
    )


def test_failed_acceptance_candidate_has_evidence_and_is_not_auto_applied(tmp_path: Path):
    root = _repo(tmp_path)
    candidate = record_candidate(root, _guide_candidate(root))

    loaded = RatchetStore(root).list()

    assert loaded[0].status == "candidate"
    assert loaded[0].evidence_ids == ["e-failure-1"]
    assert not (root / ".veya" / "GUIDES.md").exists()
    assert candidate.status == "candidate"


def test_approved_guide_candidate_is_written_with_source_and_can_be_applied(tmp_path: Path):
    root = _repo(tmp_path)
    candidate = record_candidate(root, _guide_candidate(root))

    approved = transition_candidate(root, candidate.id, "approved")
    applied = apply_candidate(root, approved.id)

    assert applied.status == "applied"
    assert applied.applied_path == str((root / ".veya" / "GUIDES.md").resolve())
    guides = load_guides(root)
    rule = next(rule for guide in guides for rule in guide.rules if "pytest" in rule.text)
    assert rule.source_path.endswith(".veya/GUIDES.md")
    assert rule.source_line is not None


def test_rejected_candidate_cannot_be_applied(tmp_path: Path):
    root = _repo(tmp_path)
    candidate = create_ratchet_candidate(
        workspace_id=detect_workspace(root).id,
        source_task_id="task-2",
        failure_class="user_correction",
        observed_failure="the suggested rule was too broad",
        proposed_fix_layer="test",
        evidence_ids=["e-correction-1"],
    )
    record_candidate(root, candidate)
    transition_candidate(root, candidate.id, "rejected")

    with pytest.raises(RatchetError, match="only approved"):
        apply_candidate(root, candidate.id)
    assert not (root / ".veya" / "GUIDES.md").exists()


def test_sensor_candidate_requires_sensor_and_evidence(tmp_path: Path):
    root = _repo(tmp_path)
    sensor = Sensor("sensor-1", "pytest", "test", "pytest", True, "low", True, 60)

    candidate = create_ratchet_candidate(
        workspace_id=detect_workspace(root).id,
        source_task_id="task-3",
        failure_class="test_failure",
        observed_failure="pytest command missing",
        proposed_fix_layer="sensor",
        proposed_sensor=sensor,
        evidence_ids=["e-test-1"],
    )
    assert candidate.proposed_sensor is sensor
    with pytest.raises(RatchetError, match="evidence"):
        create_ratchet_candidate(
            workspace_id=detect_workspace(root).id,
            source_task_id="task-4",
            failure_class="test_failure",
            observed_failure="missing evidence",
            proposed_fix_layer="test",
            evidence_ids=[],
        )
