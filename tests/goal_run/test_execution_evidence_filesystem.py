"""Ignored paths are first-class execution evidence.

Git is blind to ignored paths, so a real execution effect under a gitignored
directory would otherwise be unattributable. These tests pin the combined
Git + filesystem execution evidence, using ``.veya/`` (the path this repository
actually ignores) as the regression subject.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

from server.goal_run.execution_delta import (
    capture_filesystem_state,
    capture_git_state,
    cleanup_filesystem_delta,
    declared_target_paths,
    execution_delta,
    filesystem_delta,
)
from veya.supervision.evidence import build_execution_report
from veya.supervision.models import Mission, ReviewDecision, SupervisorReview
from veya.supervision.retask import plan_retask

IGNORED = ".veya/qualification/q4-positive/probe.txt"


def _ignored_repo(path: Path) -> Path:
    """A repo that ignores .veya/ exactly like the real one does."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / ".gitignore").write_text(".veya/\n", encoding="utf-8")
    (path / "existing.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


def _write(path: Path, relative: str, content: str) -> None:
    target = path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _assert_ignored(root: Path, relative: str) -> None:
    """The subject must really be invisible to Git, or the test proves nothing."""
    ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", relative], check=False)
    assert ignored.returncode == 0, f"{relative} is not gitignored; regression is not exercised"


# ── ignored file create / modify / delete ─────────────────────────────────


def test_ignored_file_creation_in_filesystem_delta(tmp_path: Path) -> None:
    root = _ignored_repo(tmp_path / "repo")
    _assert_ignored(root, IGNORED)
    targets = [IGNORED]
    before = capture_filesystem_state(str(root), targets)
    _write(root, IGNORED, "VEYA_Q4_TOKEN\n")
    after = capture_filesystem_state(str(root), targets)

    delta = filesystem_delta(before, after)
    assert delta["created"] == [IGNORED]
    assert delta["modified"] == []
    assert delta["deleted"] == []
    assert delta["observed"][IGNORED]["after"]["content_sha256"]


def test_ignored_file_modification_in_filesystem_delta(tmp_path: Path) -> None:
    root = _ignored_repo(tmp_path / "repo")
    _write(root, IGNORED, "before\n")
    _assert_ignored(root, IGNORED)
    targets = [IGNORED]
    before = capture_filesystem_state(str(root), targets)
    _write(root, IGNORED, "after\n")
    after = capture_filesystem_state(str(root), targets)

    delta = filesystem_delta(before, after)
    assert delta["modified"] == [IGNORED]
    assert delta["created"] == []
    assert delta["deleted"] == []


def test_ignored_file_deletion_in_filesystem_delta(tmp_path: Path) -> None:
    root = _ignored_repo(tmp_path / "repo")
    _write(root, IGNORED, "token\n")
    _assert_ignored(root, IGNORED)
    targets = [IGNORED]
    before = capture_filesystem_state(str(root), targets)
    (root / IGNORED).unlink()
    after = capture_filesystem_state(str(root), targets)

    delta = filesystem_delta(before, after)
    assert delta["deleted"] == [IGNORED]
    assert delta["created"] == []
    assert delta["modified"] == []


def test_gitignored_veya_artifact_is_execution_evidence(tmp_path: Path) -> None:
    """The artifact is invisible to Git but must still be attributed."""
    root = _ignored_repo(tmp_path / "repo")
    _assert_ignored(root, IGNORED)
    targets = [IGNORED]
    before_git = capture_git_state(str(root))
    before_fs = capture_filesystem_state(str(root), targets)
    _write(root, IGNORED, "VEYA_Q4_ABC\n")
    after_git = capture_git_state(str(root))
    after_fs = capture_filesystem_state(str(root), targets)

    delta = execution_delta(
        before_git, after_git, before_filesystem=before_fs, after_filesystem=after_fs
    )
    # Git saw nothing at all...
    assert delta["git_delta"]["created"] == []
    assert delta["git_delta"]["modified"] == []
    assert delta["git_delta"]["deleted"] == []
    # ...but the execution effect is still attributed.
    assert delta["filesystem_delta"]["created"] == [IGNORED]
    assert IGNORED in delta["attributed_paths"]
    assert delta["attributed_path_count"] == 1


def test_verifier_accepts_verified_ignored_artifact_delta(tmp_path: Path) -> None:
    """Attribution plus readback hash is the acceptance condition for the artifact.

    Filesystem existence alone is not sufficient: the recorded content hash must
    match the expected token, otherwise a wrong payload is not evidence.
    """
    import hashlib

    root = _ignored_repo(tmp_path / "repo")
    token = "VEYA_Q4_EXPECTED_TOKEN"
    targets = [IGNORED]
    before_fs = capture_filesystem_state(str(root), targets)
    before_git = capture_git_state(str(root))
    _write(root, IGNORED, token + "\n")
    after_fs = capture_filesystem_state(str(root), targets)
    after_git = capture_git_state(str(root))
    delta = execution_delta(
        before_git, after_git, before_filesystem=before_fs, after_filesystem=after_fs
    )

    created = delta["filesystem_delta"]["created"]
    assert created == [IGNORED]
    recorded = delta["filesystem_delta"]["observed"][IGNORED]["after"]["content_sha256"]
    expected = hashlib.sha256((token + "\n").encode()).hexdigest()
    assert recorded == expected, "readback hash must match the expected token"

    # a wrong payload must NOT satisfy attribution
    _write(root, IGNORED, "WRONG\n")
    wrong = filesystem_delta(after_fs, capture_filesystem_state(str(root), targets))
    assert wrong["modified"] == [IGNORED]
    assert (
        filesystem_delta(before_fs, capture_filesystem_state(str(root), targets))["observed"][
            IGNORED
        ]["after"]["content_sha256"]
        != expected
    )


# ── cleanup semantics ────────────────────────────────────────────────────


def test_cleanup_preserves_created_evidence(tmp_path: Path) -> None:
    root = _ignored_repo(tmp_path / "repo")
    targets = [IGNORED]
    before_fs = capture_filesystem_state(str(root), targets)
    _write(root, IGNORED, "token\n")
    execution_end = capture_filesystem_state(str(root), targets)
    execution = execution_delta(
        capture_git_state(str(root)),
        capture_git_state(str(root)),
        before_filesystem=before_fs,
        after_filesystem=execution_end,
    )
    assert execution["filesystem_delta"]["created"] == [IGNORED]

    (root / IGNORED).unlink()
    after_cleanup = capture_filesystem_state(str(root), targets)
    cleanup = cleanup_filesystem_delta(
        execution_end,
        after_cleanup,
        execution_delta_snapshot=execution["filesystem_delta"],
    )
    assert cleanup["deleted"] == [IGNORED]
    # the creation fact survives the cleanup
    assert cleanup["execution_created"] == [IGNORED]
    # and the execution delta itself is untouched
    assert execution["filesystem_delta"]["created"] == [IGNORED]
    assert execution["filesystem_delta"]["deleted"] == []


def test_cleanup_records_delete_without_erasing_creation(tmp_path: Path) -> None:
    """Final absence must not become the only recorded fact."""
    root = _ignored_repo(tmp_path / "repo")
    targets = [IGNORED]
    before_fs = capture_filesystem_state(str(root), targets)
    _write(root, IGNORED, "token\n")
    execution_end = capture_filesystem_state(str(root), targets)
    execution = execution_delta(
        capture_git_state(str(root)),
        capture_git_state(str(root)),
        before_filesystem=before_fs,
        after_filesystem=execution_end,
    )
    (root / IGNORED).unlink()
    cleanup = cleanup_filesystem_delta(
        execution_end,
        capture_filesystem_state(str(root), targets),
        execution_delta_snapshot=execution["filesystem_delta"],
    )

    assert cleanup["phase"] == "cleanup"
    assert cleanup["deleted"] == [IGNORED]
    assert cleanup["execution_created"] == [IGNORED]
    # both facts are independently readable from one cleanup record
    assert set(cleanup["execution_created"]) & set(cleanup["deleted"]) == {IGNORED}
    # and the artifact really is gone
    assert not (root / IGNORED).exists()


# ── no double counting ────────────────────────────────────────────────────


def test_git_delta_and_filesystem_delta_do_not_double_count(tmp_path: Path) -> None:
    """A path visible to both views is one attributed effect, not two.

    ``tracked`` is modified: Git sees it as a new status entry while the
    filesystem view sees a real modification. ``IGNORED`` is created: only the
    filesystem view can see it. Two effects in total, not three.
    """
    root = _ignored_repo(tmp_path / "repo")
    tracked = "existing.py"
    targets = [tracked, IGNORED]
    _assert_ignored(root, IGNORED)
    before_git = capture_git_state(str(root))
    before_fs = capture_filesystem_state(str(root), targets)

    (root / tracked).write_text("x = 2\n", encoding="utf-8")
    _write(root, IGNORED, "token\n")

    after_git = capture_git_state(str(root))
    after_fs = capture_filesystem_state(str(root), targets)
    delta = execution_delta(
        before_git, after_git, before_filesystem=before_fs, after_filesystem=after_fs
    )

    # the tracked path is reported by BOTH views
    git_seen = set(delta["git_delta"]["created"]) | set(delta["git_delta"]["modified"])
    assert tracked in git_seen
    assert delta["filesystem_delta"]["modified"] == [tracked]
    # the ignored path is reported only by the filesystem view
    assert delta["filesystem_delta"]["created"] == [IGNORED]
    assert IGNORED not in git_seen

    # counted once each, not once per view
    assert delta["attributed_path_count"] == 2
    assert sorted(delta["attributed_paths"]) == sorted([tracked, IGNORED])
    assert len(set(delta["attributed_paths"])) == len(delta["attributed_paths"])
    # the two views remain separately reportable
    assert delta["git_delta"] is not delta["filesystem_delta"]


def test_head_transition_preserves_preexisting_paths(tmp_path: Path) -> None:
    """The 24e2ee08 Git fix must survive the combined evidence model."""
    root = _ignored_repo(tmp_path / "repo")
    tracked = "existing.py"
    targets = [tracked, IGNORED]

    (root / tracked).write_text("x = 99\n", encoding="utf-8")
    before_git = capture_git_state(str(root))
    before_fs = capture_filesystem_state(str(root), targets)

    # the executor commits the pre-existing dirty file, then writes an ignored artifact
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "executor commit"], check=True)
    _write(root, IGNORED, "token\n")

    after_git = capture_git_state(str(root))
    after_fs = capture_filesystem_state(str(root), targets)
    delta = execution_delta(
        before_git, after_git, before_filesystem=before_fs, after_filesystem=after_fs
    )

    # the pre-existing committed path is not an execution deletion
    assert delta["git_delta"]["deleted"] == []
    assert delta["git_delta"]["preexisting_committed"] == [tracked]
    # and the ignored artifact is still attributed despite the HEAD transition
    assert delta["filesystem_delta"]["created"] == [IGNORED]
    assert delta["before_head"] != delta["after_head"]


# ── boundedness and projection ───────────────────────────────────────────


def test_declared_targets_are_bounded_and_scoped(tmp_path: Path) -> None:
    root = _ignored_repo(tmp_path / "repo")
    actions = [
        {"tool": "write_file", "arguments": {"filepath": IGNORED, "content": "x"}},
        {"tool": "read_hashline", "arguments": {"filepath": IGNORED}},
        {"tool": "list_files", "arguments": {}},
        {"tool": "no_args"},
        "not-a-mapping",
    ]
    targets = declared_target_paths(str(root), actions)
    # deduplicated, and no pathless action widened the surface
    assert targets == [IGNORED]

    outside = declared_target_paths(
        str(root), [{"tool": "write_file", "arguments": {"filepath": "/etc/passwd"}}]
    )
    assert outside == [], "a target outside the workspace must not be observed"


def test_filesystem_observation_never_walks_the_repository(tmp_path: Path) -> None:
    root = _ignored_repo(tmp_path / "repo")
    for index in range(50):
        (root / f"noise{index}.txt").write_text("n", encoding="utf-8")
    state = capture_filesystem_state(str(root), [IGNORED])
    assert list(state["targets"]) == [IGNORED]


def test_cleanup_delta_survives_goal_run_state_round_trip(tmp_path: Path) -> None:
    """The combined and cleanup evidence must survive durable persistence."""
    from server.goal_run.models import GoalRunState
    from server.goal_run.store import load_goal_run, save_goal_run

    state = GoalRunState(goal_id="g", goal_text="t")
    state.baseline_filesystem_state = {"targets": {IGNORED: {"exists": False}}}
    state.execution_delta = {"filesystem_delta": {"created": [IGNORED]}}
    state.cleanup_delta = {"phase": "cleanup", "deleted": [IGNORED]}
    save_goal_run(state, str(tmp_path))

    restored = load_goal_run(str(tmp_path), "g")
    assert restored is not None
    assert restored.baseline_filesystem_state == state.baseline_filesystem_state
    assert restored.execution_delta == state.execution_delta
    assert restored.cleanup_delta == state.cleanup_delta


def test_acceptance_evidence_exposes_combined_and_cleanup_views(tmp_path: Path) -> None:
    """The reviewer must see Git, filesystem and cleanup evidence together."""
    execution = {
        "git_delta": {"created": [], "modified": [], "deleted": []},
        "filesystem_delta": {"created": [IGNORED], "modified": [], "deleted": []},
        "execution_created": [],
    }
    cleanup = {"phase": "cleanup", "deleted": [IGNORED], "execution_created": [IGNORED]}
    state = types.SimpleNamespace(
        goal_id="g",
        status="completed",
        tasks={},
        final_summary="verified",
        unfinished_work=[],
        baseline_git_state={},
        baseline_filesystem_state={"targets": {IGNORED: {"exists": False}}},
        execution_delta=execution,
        cleanup_delta=cleanup,
    )
    report = build_execution_report(
        Mission(mission_id="m", goal="g", workspace=str(tmp_path)), state, iteration=0
    )
    evidence = next(
        item for item in report.runtime_evidence if item.get("kind") == "execution_delta"
    )
    assert evidence["filesystem_delta"]["created"] == [IGNORED]
    assert evidence["git_delta"]["created"] == []
    assert evidence["cleanup_delta"]["deleted"] == [IGNORED]
    assert evidence["baseline_filesystem_state"]["targets"][IGNORED]["exists"] is False
    assert report.evidence_chain


def test_ignored_artifact_delta_is_acceptable_work(tmp_path: Path) -> None:
    """End to end: combined evidence for an ignored artifact is acceptable."""
    root = _ignored_repo(tmp_path / "repo")
    targets = [IGNORED]
    before_git = capture_git_state(str(root))
    before_fs = capture_filesystem_state(str(root), targets)
    _write(root, IGNORED, "token\n")
    execution = execution_delta(
        before_git,
        capture_git_state(str(root)),
        before_filesystem=before_fs,
        after_filesystem=capture_filesystem_state(str(root), targets),
    )
    state = types.SimpleNamespace(
        goal_id="g",
        status="completed",
        tasks={},
        final_summary="verified",
        unfinished_work=[],
        baseline_git_state=before_git,
        baseline_filesystem_state=before_fs,
        execution_delta=execution,
        cleanup_delta=None,
    )
    mission = Mission(mission_id="m", goal="g", workspace=str(root))
    report = build_execution_report(mission, state, iteration=0)
    review = SupervisorReview(
        mission_id="m", iteration=0, supervisor="t", decision=ReviewDecision.accept
    )
    outcome = plan_retask(review, mission=mission, report=report)
    assert outcome.mission_status.value == "ACCEPTED"
    assert IGNORED in execution["filesystem_delta"]["created"]


@pytest.mark.parametrize("missing", ["before", "after"])
def test_filesystem_delta_is_empty_without_a_baseline(missing: str) -> None:
    """A one-sided observation must not manufacture effects."""
    before = capture_filesystem_state("/tmp", [IGNORED]) if missing == "before" else None
    after = capture_filesystem_state("/tmp", [IGNORED]) if missing == "after" else None
    delta = filesystem_delta(before, after)
    assert delta["created"] == []
    assert delta["modified"] == []
    assert delta["deleted"] == []


def test_undeclared_paths_are_never_claimed(tmp_path: Path) -> None:
    """A path absent from the baseline cannot be an attributed effect."""
    root = _ignored_repo(tmp_path / "repo")
    before = capture_filesystem_state(str(root), [])
    _write(root, IGNORED, "token\n")
    delta = filesystem_delta(before, capture_filesystem_state(str(root), []))
    assert delta["created"] == []
    assert delta["modified"] == []
    assert delta["deleted"] == []


def test_git_only_delta_stays_backward_compatible(tmp_path: Path) -> None:
    """No filesystem baseline means the original flat Git shape."""
    root = _ignored_repo(tmp_path / "repo")
    before = capture_git_state(str(root))
    (root / "created.txt").write_text("x", encoding="utf-8")
    delta: dict[str, Any] = execution_delta(before, capture_git_state(str(root)))
    assert "filesystem_delta" not in delta
    assert delta["execution_created"] == ["created.txt"]
    assert delta["attributed_path_count"] == 1
