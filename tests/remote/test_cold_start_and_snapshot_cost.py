"""Cold-start and per-execution cost fixes (Local2 audit 2026-10-12).

- reconcile_worktrees decides once per worktree path (822 records -> 33 paths
  on ubuntu26 cost ~65 s on the first initialize) and never reclaims a path a
  live execution still holds.
- capture_git_state batches its per-path git calls.
- the memory store serialises once instead of streaming millions of writes.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from test_execution_worktree_lease_lifecycle import (
    _execution_with_worktree,
    _manager,
    _repo,
    _terminalize_without_release,
)

import runtime.coding.worktree as worktree_mod
from server.goal_run import execution_delta
from veya.remote.execution import ExecutionStatus


def _clone_record(manager, execution_id: str, *, status: str):
    record = manager._record_for_update(execution_id)
    clone = dataclasses.replace(record, execution_id=f"exec_{uuid.uuid4().hex[:12]}")
    clone.status = status
    clone.phase = status
    clone.worktree_released_at = None
    with manager._lock:
        manager._records[clone.execution_id] = clone
    return clone


def _count_teardowns(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = worktree_mod.teardown_worktree

    def counting(path, **kwargs):
        calls.append(str(path))
        return real(path, **kwargs)

    monkeypatch.setattr(worktree_mod, "teardown_worktree", counting)
    return calls


async def test_reconcile_inspects_a_shared_dirty_path_once(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    exec_id, worktree = await _execution_with_worktree(manager, root, parent_execution_id=None)
    _terminalize_without_release(manager, exec_id, ExecutionStatus.COMPLETED)
    (Path(worktree) / "app.py").write_text("print('wip')\n", encoding="utf-8")
    clones = [_clone_record(manager, exec_id, status="COMPLETED") for _ in range(20)]
    calls = _count_teardowns(monkeypatch)

    report = manager.reconcile_worktrees()

    assert calls == [str(Path(worktree))] or len(calls) == 1
    assert Path(worktree).is_dir()
    assert report["released"] == 0
    retained_ids = {item["execution_id"] for item in report["retained"]}
    assert {exec_id, *(c.execution_id for c in clones)} <= retained_ids


async def test_reconcile_releases_a_shared_clean_path_once_for_every_record(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    exec_id, worktree = await _execution_with_worktree(manager, root, parent_execution_id=None)
    _terminalize_without_release(manager, exec_id, ExecutionStatus.COMPLETED)
    clone = _clone_record(manager, exec_id, status="FAILED")
    calls = _count_teardowns(monkeypatch)

    report = manager.reconcile_worktrees()

    assert len(calls) == 1
    assert not Path(worktree).exists()
    assert report["released"] == 1
    assert manager._record_for_update(clone.execution_id).worktree_released_at is not None


async def test_reconcile_never_reclaims_a_path_a_live_execution_holds(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    exec_id, worktree = await _execution_with_worktree(manager, root, parent_execution_id=None)
    terminal = _clone_record(manager, exec_id, status="COMPLETED")
    calls = _count_teardowns(monkeypatch)

    report = manager.reconcile_worktrees()

    assert calls == []
    assert Path(worktree).is_dir(), "reclaimed a worktree a live execution still uses"
    reasons = {item["execution_id"]: item["reason"] for item in report["retained"]}
    assert reasons[terminal.execution_id] == "SHARED_WITH_LIVE_EXECUTION"
    assert reasons[exec_id] == "NOT_TERMINAL"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture()
def messy_repo(tmp_path: Path) -> Path:
    root = tmp_path / "messy"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for name in ("tracked.py", "deleted.py", "staged_mod.py", "with space.txt"):
        (root / name).write_text(f"{name}\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    (root / "tracked.py").write_text("changed\n", encoding="utf-8")
    (root / "deleted.py").unlink()
    (root / "staged_mod.py").write_text("staged\n", encoding="utf-8")
    _git(root, "add", "staged_mod.py")
    (root / "staged_new.py").write_text("new\n", encoding="utf-8")
    _git(root, "add", "staged_new.py")
    (root / "with space.txt").write_text("edited\n", encoding="utf-8")
    (root / "untracked").mkdir()
    for i in range(5):
        (root / "untracked" / f"u{i}.txt").write_text(str(i), encoding="utf-8")
    (root / "caf\u00e9.txt").write_text("unicode\n", encoding="utf-8")
    os.symlink("tracked.py", root / "link_ok")
    os.symlink("missing-target", root / "link_broken")
    nested = root / "nested"
    nested.mkdir()
    _git(nested, "init", "-q", "-b", "main")
    return root


def _per_path_snapshot(root: Path) -> dict:
    status = execution_delta._git(str(root), ["status", "--porcelain=v1", "--untracked-files=all"])
    records = {}
    for line in status.splitlines():
        code, relative = line[:2], line[3:]
        if " -> " in relative:
            relative = relative.split(" -> ", 1)[1]
        records[relative] = execution_delta._path_record(root, relative, code)
    return records


def test_batched_snapshot_matches_the_per_path_records(messy_repo: Path) -> None:
    expected = _per_path_snapshot(messy_repo)
    assert len(expected) >= 12
    assert execution_delta.capture_git_state(str(messy_repo))["files"] == expected


def test_batched_snapshot_spawns_a_bounded_number_of_git_processes(
    messy_repo: Path, monkeypatch
) -> None:
    for i in range(60):
        (messy_repo / f"bulk_{i}.txt").write_text(str(i), encoding="utf-8")
    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting(argv, *args, **kwargs):
        if argv and argv[0] == "git":
            calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(execution_delta.subprocess, "run", counting)
    execution_delta.capture_git_state(str(messy_repo))
    # status, ls-files, hash-object, rev-parse, ls-tree, plus per-path fallbacks
    # for the few non-regular entries (nested repo, broken link, quoted name).
    assert len(calls) < 25, len(calls)


def test_memory_store_writes_the_same_bytes(tmp_path: Path) -> None:
    from server.memory_controller import _MemoryStore

    store = _MemoryStore(tmp_path / "mem.json")
    store._records = {
        f"m{i}": {"text": f"caf\u00e9 {i}", "n": i, "tags": ["a", "b"]} for i in range(50)
    }
    store._save()
    expected = json.dumps(store._records, ensure_ascii=False, indent=2)
    assert (tmp_path / "mem.json").read_text(encoding="utf-8") == expected
