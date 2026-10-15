"""Where a GoalRun keeps its durable files when the caller gave no project root.

GoalRun writes ``<project_root>/.veya-project/goal-runs/<goal_id>/``. Callers that
pass no root (``None``/``""``/``"."``) used to get the process cwd. In production
the cwd is the read-only image dir (``/app``), so every such run died with
``PermissionError: '.veya-project'`` before it planned anything (S6 routine,
2026-10-13: 119 failures in 3h).

Rule: an explicit absolute root is used as given. A missing or relative root
resolves against the cwd when the cwd is writable (CLI behaviour unchanged);
otherwise it falls back to ``$VEYA_WRITE_ROOT/goal_run/<bot_id>`` (default
``~/.veya/work``), which is the server's writable data volume.
"""

from __future__ import annotations

import os
from pathlib import Path


def default_write_root() -> Path:
    return Path(os.environ.get("VEYA_WRITE_ROOT", str(Path.home() / ".veya" / "work"))).expanduser()


def _writable_dir(path: Path) -> bool:
    try:
        return path.is_dir() and os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False


def resolve_goal_run_root(
    project_root: str | os.PathLike[str] | None, *, bot_id: str | None = None
) -> str:
    """Return an absolute, writable project root for GoalRun artifacts."""

    raw = str(project_root).strip() if project_root is not None else ""
    if raw and Path(raw).expanduser().is_absolute():
        return str(Path(raw).expanduser())
    candidate = (Path.cwd() / (raw or ".")).resolve()
    if _writable_dir(candidate):
        return str(candidate)
    fallback = default_write_root() / "goal_run" / (str(bot_id or "").strip() or "default")
    fallback.mkdir(parents=True, exist_ok=True)
    return str(fallback)


__all__ = ["default_write_root", "resolve_goal_run_root"]
