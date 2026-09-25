from __future__ import annotations

import concurrent.futures
import subprocess
from pathlib import Path
from unittest.mock import patch

from runtime.coding.workspace_detect import detect_workspace
from runtime.coding.worktree import (
    WorktreeManager,
    collect_worktree_metrics,
    reap_stale_worktrees,
    teardown_worktree,
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
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Coding Tests")
    (root / ".gitignore").write_text(".veya/\n", encoding="utf-8")
    (root / "app.py").write_text("print('base')\n", encoding="utf-8")
    (root / "ren.py").write_text("print('rename')\n", encoding="utf-8")
    (root / "del.py").write_text("print('delete')\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


# ── P0-1: TERMINAL AUTHORITY ──────────────────────────────────────────


def test_teardown_terminal_authority_allowed_states(tmp_path: Path):
    """P0-1: COMPLETED, FAILED, CANCELLED allow teardown for clean worktrees."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))

    for status, task_id in [
        ("COMPLETED", "task-auth-completed"),
        ("FAILED", "task-auth-failed"),
        ("CANCELLED", "task-auth-cancelled"),
    ]:
        rec = manager.create(task_id, f"test {status}")
        wt = Path(rec.path)
        assert wt.is_dir()
        res = teardown_worktree(wt, execution_status=status)
        assert res["cleaned"] is True
        assert res["status"] == "CLEANED"
        assert not wt.exists()


def test_teardown_terminal_authority_non_terminal_preserved(tmp_path: Path):
    """P0-1: Non-terminal states (RUNNING, QUEUED, STARTING, DISPATCHING, FINALIZING) are preserved."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))

    for non_terminal in ["RUNNING", "QUEUED", "STARTING", "DISPATCHING", "FINALIZING"]:
        rec = manager.create(f"task-{non_terminal.lower()}", f"test {non_terminal}")
        wt = Path(rec.path)
        res = teardown_worktree(wt, execution_status=non_terminal)
        assert res["cleaned"] is False
        assert res["status"] == f"{non_terminal}_PRESERVED"
        assert wt.is_dir()
        # cleanup for next iteration
        manager.discard(rec.task_id, force=True)


def test_teardown_terminal_authority_unknown_and_none_fail_closed(tmp_path: Path):
    """P0-1: UNKNOWN, None, and arbitrary unrecognized strings fail-closed to PRESERVE."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))

    for i, (unknown_status, expected_tag) in enumerate(
        [
            (None, "UNKNOWN_PRESERVED"),
            ("", "UNKNOWN_PRESERVED"),
            ("UNKNOWN", "UNKNOWN_PRESERVED"),
            ("UNRECOGNIZED_PHASE", "UNRECOGNIZED_PHASE_PRESERVED"),
        ]
    ):
        rec = manager.create(f"task-unk-{i}", f"test unknown {i}")
        wt = Path(rec.path)
        res = teardown_worktree(wt, execution_status=unknown_status)
        assert res["cleaned"] is False
        assert res["status"] == expected_tag
        assert wt.is_dir()
        manager.discard(rec.task_id, force=True)


# ── P0-2: PROCESS REFERENCE SAFETY ───────────────────────────────────


def test_teardown_process_cwd_reference_preserved(tmp_path: Path):
    """P0-2: Worktree with an active process having cwd inside it is preserved."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("task-proc-cwd", "cwd test")
    wt = Path(rec.path)

    # Launch subprocess with cwd inside worktree
    proc = subprocess.Popen(["sleep", "5"], cwd=str(wt))
    try:
        res = teardown_worktree(wt, execution_status="COMPLETED")
        assert res["cleaned"] is False
        assert res["status"] == "ACTIVE_PRESERVED"
        assert "cwd" in str(res["reason"])
        assert wt.is_dir()
    finally:
        proc.terminate()
        proc.wait()

    # After process terminates, retry teardown succeeds
    retry_res = teardown_worktree(wt, execution_status="COMPLETED")
    assert retry_res["cleaned"] is True
    assert retry_res["status"] == "CLEANED"
    assert not wt.exists()


def test_teardown_process_open_fd_reference_preserved(tmp_path: Path):
    """P0-2: Worktree with an active process having open FD inside it (cwd outside) is preserved."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("task-proc-fd", "fd test")
    wt = Path(rec.path)
    target_file = wt / "app.py"

    # Open a file inside the worktree from a process whose cwd is outside (tmp_path)
    with target_file.open("r") as f:
        # Pass open file descriptor to child process keeping it open
        proc = subprocess.Popen(["sleep", "5"], cwd=str(tmp_path), stdin=f)
        try:
            res = teardown_worktree(wt, execution_status="COMPLETED")
            assert res["cleaned"] is False
            assert res["status"] == "ACTIVE_PRESERVED"
            assert "open fd" in str(res["reason"])
            assert wt.is_dir()
        finally:
            proc.terminate()
            proc.wait()

    # After closing FD and process termination, teardown succeeds
    retry_res = teardown_worktree(wt, execution_status="COMPLETED")
    assert retry_res["cleaned"] is True
    assert retry_res["status"] == "CLEANED"
    assert not wt.exists()


# ── P0-4: GIT CLEAN SEMANTICS ─────────────────────────────────────────


def test_teardown_dirty_tracked_modified_preserved(tmp_path: Path):
    """P0-4: Tracked modified files make worktree dirty and preserve it."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("task-dirty-mod", "dirty mod")
    wt = Path(rec.path)
    (wt / "app.py").write_text("print('modified')\n", encoding="utf-8")

    res = teardown_worktree(wt, execution_status="COMPLETED")
    assert res["cleaned"] is False
    assert res["status"] == "DIRTY_PRESERVED"
    assert wt.is_dir()


def test_teardown_dirty_staged_preserved(tmp_path: Path):
    """P0-4: Staged changes make worktree dirty and preserve it."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("task-dirty-staged", "dirty staged")
    wt = Path(rec.path)
    (wt / "app.py").write_text("print('staged')\n", encoding="utf-8")
    _git(wt, "add", "app.py")

    res = teardown_worktree(wt, execution_status="COMPLETED")
    assert res["cleaned"] is False
    assert res["status"] == "DIRTY_PRESERVED"
    assert wt.is_dir()


def test_teardown_dirty_untracked_preserved(tmp_path: Path):
    """P0-4: Untracked files make worktree dirty and preserve it."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("task-dirty-untracked", "dirty untracked")
    wt = Path(rec.path)
    (wt / "new_untracked.txt").write_text("new\n", encoding="utf-8")

    res = teardown_worktree(wt, execution_status="COMPLETED")
    assert res["cleaned"] is False
    assert res["status"] == "DIRTY_PRESERVED"
    assert wt.is_dir()


def test_teardown_dirty_deleted_preserved(tmp_path: Path):
    """P0-4: Deleted tracked files make worktree dirty and preserve it."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("task-dirty-deleted", "dirty deleted")
    wt = Path(rec.path)
    (wt / "del.py").unlink()

    res = teardown_worktree(wt, execution_status="COMPLETED")
    assert res["cleaned"] is False
    assert res["status"] == "DIRTY_PRESERVED"
    assert wt.is_dir()


def test_teardown_dirty_rename_preserved(tmp_path: Path):
    """P0-4: Renamed tracked files make worktree dirty and preserve it."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("task-dirty-ren", "dirty rename")
    wt = Path(rec.path)
    _git(wt, "mv", "ren.py", "renamed.py")

    res = teardown_worktree(wt, execution_status="COMPLETED")
    assert res["cleaned"] is False
    assert res["status"] == "DIRTY_PRESERVED"
    assert wt.is_dir()


# ── P0-5: LOCK SAFETY ─────────────────────────────────────────────────


def test_teardown_locked_worktree_preserved(tmp_path: Path):
    """P0-5: Locked worktree with reason is preserved."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("task-lock-safe", "lock test")
    wt = Path(rec.path)
    _git(root, "worktree", "lock", str(wt), "--reason", "mission active lock")

    status = manager.status(path=wt)
    assert status.locked is True
    assert status.lock_reason == "mission active lock"

    res = teardown_worktree(wt, execution_status="COMPLETED")
    assert res["cleaned"] is False
    assert res["status"] == "LOCKED_PRESERVED"
    assert "mission active lock" in str(res["reason"])
    assert wt.is_dir()


# ── P0-6: PRUNE STRATEGY ──────────────────────────────────────────────


def test_discard_does_not_call_global_prune(tmp_path: Path):
    """P0-6: Normal discard removes worktree without calling global worktree prune."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("task-no-prune", "no prune")
    wt = Path(rec.path)

    import runtime.coding.worktree as wt_mod

    real_git = wt_mod._git_command
    calls = []

    def tracked_git(r, args, **kwargs):
        calls.append(list(args))
        return real_git(r, args, **kwargs)

    with patch.object(wt_mod, "_git_command", side_effect=tracked_git):
        manager.discard(rec.task_id)

    # Verify 'worktree prune' was NOT called during discard
    assert not any("prune" in c for c in calls)
    assert not wt.exists()


# ── P0-7: IDEMPOTENCY / CONCURRENCY ───────────────────────────────────


def test_teardown_concurrent_calls_race_safe(tmp_path: Path):
    """P0-7: Multiple threads calling teardown_worktree concurrently on same worktree."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("task-concurrent-01", "concurrent test")
    wt = Path(rec.path)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(teardown_worktree, wt, execution_status="COMPLETED") for _ in range(5)
        ]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # Exactly one thread succeeds in CLEANED; other threads see NOT_FOUND
    cleaned_count = sum(1 for r in results if r["status"] == "CLEANED")
    not_found_count = sum(1 for r in results if r["status"] == "NOT_FOUND")
    assert cleaned_count == 1
    assert not_found_count == 4
    assert not wt.exists()


# ── P0-8: REAL E2E CASES A - H ────────────────────────────────────────


def test_e2e_case_a_completed_clean_artifact_durable_worktree_removed(tmp_path: Path):
    """CASE A: completed + clean => artifact durable => worktree removed."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("case-a", "Case A")
    wt = Path(rec.path)

    # Artifact is recorded/materialized into artifact store
    artifact_store = root / ".veya" / "artifacts" / "ex_case_a"
    artifact_store.mkdir(parents=True, exist_ok=True)
    (artifact_store / "output.txt").write_text("durable artifact\n", encoding="utf-8")

    res = teardown_worktree(wt, execution_status="COMPLETED")
    assert res["cleaned"] is True
    assert res["status"] == "CLEANED"
    assert not wt.exists()
    # Artifact remains completely intact and durable
    assert (artifact_store / "output.txt").read_text(encoding="utf-8") == "durable artifact\n"


def test_e2e_case_b_failed_clean_evidence_durable_worktree_removed(tmp_path: Path):
    """CASE B: failed + clean => evidence durable => worktree removed."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("case-b", "Case B")
    wt = Path(rec.path)

    res = teardown_worktree(wt, execution_status="FAILED")
    assert res["cleaned"] is True
    assert res["status"] == "CLEANED"
    assert not wt.exists()


def test_e2e_case_c_cancelled_clean_worktree_removed(tmp_path: Path):
    """CASE C: cancelled + clean => worktree removed."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("case-c", "Case C")
    wt = Path(rec.path)

    res = teardown_worktree(wt, execution_status="CANCELLED")
    assert res["cleaned"] is True
    assert res["status"] == "CLEANED"
    assert not wt.exists()


def test_e2e_case_d_completed_dirty_preserved(tmp_path: Path):
    """CASE D: completed + dirty => preserved."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("case-d", "Case D")
    wt = Path(rec.path)
    (wt / "dirty.txt").write_text("uncommitted change\n", encoding="utf-8")

    res = teardown_worktree(wt, execution_status="COMPLETED")
    assert res["cleaned"] is False
    assert res["status"] == "DIRTY_PRESERVED"
    assert wt.is_dir()
    assert (wt / "dirty.txt").exists()


def test_e2e_case_e_running_clean_preserved(tmp_path: Path):
    """CASE E: running + clean => preserved."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("case-e", "Case E")
    wt = Path(rec.path)

    res = teardown_worktree(wt, execution_status="RUNNING")
    assert res["cleaned"] is False
    assert res["status"] == "RUNNING_PRESERVED"
    assert wt.is_dir()


def test_e2e_case_f_open_fd_terminal_clean_preserved_then_cleaned(tmp_path: Path):
    """CASE F: open FD reference + terminal + clean => preserved until process exits => retry later succeeds."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("case-f", "Case F")
    wt = Path(rec.path)

    with (wt / "app.py").open("r") as f:
        proc = subprocess.Popen(["sleep", "3"], cwd=str(tmp_path), stdin=f)
        try:
            res1 = teardown_worktree(wt, execution_status="COMPLETED")
            assert res1["cleaned"] is False
            assert res1["status"] == "ACTIVE_PRESERVED"
            assert wt.is_dir()
        finally:
            proc.terminate()
            proc.wait()

    res2 = teardown_worktree(wt, execution_status="COMPLETED")
    assert res2["cleaned"] is True
    assert res2["status"] == "CLEANED"
    assert not wt.exists()


def test_e2e_case_g_debug_retention_preserved(tmp_path: Path, monkeypatch):
    """CASE G: debug retention => preserved."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("case-g", "Case G")
    wt = Path(rec.path)

    monkeypatch.setenv("VEYA_WORKTREE_DEBUG_RETENTION", "true")
    res = teardown_worktree(wt, execution_status="COMPLETED")
    assert res["cleaned"] is False
    assert res["status"] == "DEBUG_RETENTION_PRESERVED"
    assert wt.is_dir()


def test_e2e_case_h_unknown_lifecycle_preserved(tmp_path: Path):
    """CASE H: unknown lifecycle => preserved."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))
    rec = manager.create("case-h", "Case H")
    wt = Path(rec.path)

    res = teardown_worktree(wt, execution_status="SOMETHING_UNKNOWN")
    assert res["cleaned"] is False
    assert res["status"] == "SOMETHING_UNKNOWN_PRESERVED"
    assert wt.is_dir()


# ── P0-9: REAPER / RETRY ──────────────────────────────────────────────


def test_reap_stale_worktrees(tmp_path: Path):
    """P0-9: Periodic reconciliation reaps terminal clean unlocked worktrees and preserves dirty/active."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))

    # 1. Clean completed -> should reap
    rec1 = manager.create("task-reap-clean", "reap clean")
    # 2. Dirty completed -> should skip
    rec2 = manager.create("task-reap-dirty", "reap dirty")
    (Path(rec2.path) / "scratch.txt").write_text("dirty\n", encoding="utf-8")
    # 3. Running clean -> should skip
    rec3 = manager.create("task-reap-running", "reap running")

    status_map = {
        rec1.task_id: "COMPLETED",
        rec2.task_id: "COMPLETED",
        rec3.task_id: "RUNNING",
    }

    reap_result = reap_stale_worktrees(
        root, execution_status_lookup=lambda tid: status_map.get(tid)
    )
    assert reap_result["reaped"] == 1
    assert rec1.path in reap_result["reaped_paths"]
    assert not Path(rec1.path).exists()
    assert Path(rec2.path).exists()
    assert Path(rec3.path).exists()


# ── P0-10: STORAGE REGRESSION GATE METRICS ────────────────────────────


def test_collect_worktree_metrics(tmp_path: Path):
    """P0-10: Worktree storage metrics collection."""
    root = _repo(tmp_path)
    manager = WorktreeManager(detect_workspace(root))

    manager.create("task-m-1", "m1")
    rec2 = manager.create("task-m-2", "m2")
    (Path(rec2.path) / "test.dat").write_text("data bytes\n", encoding="utf-8")

    metrics = collect_worktree_metrics(root)
    m_dict = metrics.to_dict()
    assert m_dict["veya_worktrees_total"] == 2
    assert m_dict["veya_worktrees_dirty"] == 1
    assert m_dict["veya_worktree_bytes"] > 0
