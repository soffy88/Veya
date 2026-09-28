"""D3 regression cover: ``workspace.list`` must cost the requested scope.

The implementation used ``sorted(target.rglob("*"))`` and filtered noise
*after* the walk.  ``rglob`` cannot prune, so every noisy subtree was fully
traversed and materialised before the filter ran.  On the veya repo, which
carries hundreds of execution worktrees, measured p50 was 40.7s and p95 42.0s.

These tests pin the two properties that matter: it excludes execution
worktrees, and its cost is bounded rather than proportional to repository size.
"""

from __future__ import annotations

from pathlib import Path

from veya.remote.tool_adapter import _list_workspace


def _tree(root: Path, *, worktrees: int, noise: bool) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('x')\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    for name in ("venv", ".venv", "node_modules", "__pycache__", ".git"):
        if noise:
            (root / name).mkdir()
            (root / name / "junk.bin").write_bytes(b"0" * 4096)
    worktree_root = root / ".veya" / "worktrees"
    worktree_root.mkdir(parents=True)
    for i in range(worktrees):
        target = worktree_root / f"task-exec-{i:04d}"
        target.mkdir()
        (target / "payload.bin").write_bytes(b"0" * 8192)
    return root


def test_workspace_list_excludes_execution_worktrees(tmp_path: Path) -> None:
    root = _tree(tmp_path / "repo", worktrees=50, noise=True)
    lines = _list_workspace(root)
    assert not any("worktrees/task-exec" in line for line in lines), lines[:20]
    assert not any(line.startswith(".git") for line in lines)
    assert not any("node_modules" in line for line in lines)
    assert not any("venv" in line for line in lines)


def test_workspace_list_still_reports_real_content(tmp_path: Path) -> None:
    root = _tree(tmp_path / "repo", worktrees=3, noise=False)
    lines = _list_workspace(root)
    assert any(line.startswith("README.md (") for line in lines), lines
    assert "src/" in lines
    assert any(line.startswith("src/app.py (") for line in lines), lines
    # the worktree directory is pruned as a whole, not merely its contents
    assert ".veya/worktrees/" not in lines, lines


def test_workspace_list_cost_is_independent_of_worktree_count(tmp_path: Path) -> None:
    """Cost must track the requested scope, not total execution history.

    A 400x larger worktree population must not multiply the traversal.
    """
    import time

    small = _tree(tmp_path / "small", worktrees=1, noise=True)
    large = _tree(tmp_path / "large", worktrees=400, noise=True)

    def timed(target: Path) -> tuple[float, int]:
        start = time.perf_counter()
        lines = _list_workspace(target)
        return time.perf_counter() - start, len(lines)

    # warm the page cache so this compares traversal, not first-touch disk
    timed(small)
    small_s, small_n = timed(small)
    large_s, large_n = timed(large)

    assert small_n == large_n, (small_n, large_n)
    # Allow generous slack for a noisy shared box; rglob scaled ~linearly here
    # (400 worktrees cost orders of magnitude more) while pruning does not.
    assert large_s < max(0.75, small_s * 25), (small_s, large_s)


def test_workspace_list_respects_limit_and_marks_truncation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for i in range(50):
        (root / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
    lines = _list_workspace(root, limit=10)
    assert len(lines) <= 11, len(lines)
    assert lines[-1] == "... (truncated)"


def test_workspace_list_depth_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    deep = root / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("x", encoding="utf-8")
    lines = _list_workspace(root)
    assert not any("h/deep.txt" in line for line in lines), lines
    assert any(line.startswith("a/") for line in lines)
