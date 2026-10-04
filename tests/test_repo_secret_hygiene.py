"""A backup of a secret-bearing file must never be committable by accident.

`.env` is ignored, but a timestamped copy of it is a different filename.
``.env.bak-before-free-pool-providers-20260927-123436`` sat in the worktree as an
untracked file holding every key from ``.env``; ``git status`` listed it and
``git add -A`` would have committed live credentials. The rules are asserted with
``git check-ignore`` against the names that actually exist, rather than by
reading .gitignore and trusting the prose.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# tests/test_repo_secret_hygiene.py -> parents[1] is the repo root
REPO_ROOT = Path(__file__).resolve().parents[1]

# Names that were observed in this worktree or are the shape it takes.
BACKUP_NAMES = [
    ".env.bak-before-free-pool-providers-20260927-123436",
    ".env.bak",
    ".env.production.bak",
    ".env.local.bak",
    "app.env.bak",
]


def _ignored(name: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", name],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def test_env_backup_files_are_ignored():
    not_ignored = [name for name in BACKUP_NAMES if not _ignored(name)]
    assert not not_ignored, f"git would track these secret-bearing files: {not_ignored}"


def test_the_observed_backup_is_actually_gone_from_git_status():
    """The real file, not just the pattern, must be invisible to git."""

    if not (REPO_ROOT / ".env.bak-before-free-pool-providers-20260927-123436").exists():
        return  # nothing to assert if the operator already removed it
    listed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "env.bak" not in listed, listed


def test_plain_env_stays_ignored():
    for name in (".env", ".env.local"):
        assert _ignored(name), f"{name} must remain ignored"
