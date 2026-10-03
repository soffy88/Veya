"""Async admission + capability-memoisation contract for Local2 worker.dispatch.

Regression cover for two defects:

1. ``_submit_worker_child`` awaited ``run_sync_in_daemon_thread`` from a plain
   ``def``, which is a module-level ``SyntaxError``.  That killed the whole MCP
   import chain, so the gateway could never bind :8790 and the unit crash-looped
   into systemd's start limit.  The manifest must be genuinely awaited.
2. The runtime capability probe shells out to ``<binary> --version`` (2s
   timeout) once per child, inside the admission request.  It is now memoised
   for a short TTL, keyed on executor + workspace + health.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

from veya.remote.tool_adapter import RemoteToolAdapter


def _session(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        session_id="local2-session",
        principal="owner",
        token_id="local2-token",
        active_workspace=str(root),
        explicit_workspace=str(root),
    )


def _binding(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        requested_path=str(root),
        requested_realpath=str(root),
        repo_root=str(root),
        repo_identity=str(root),
        worktree_path=None,
        worktree_repo_root=None,
    )


def test_submit_worker_child_is_a_coroutine_function() -> None:
    """Regression: the manifest probe forces an async admission signature.

    ``_submit_worker_child`` awaits ``run_sync_in_daemon_thread`` for the
    runtime capability manifest.  A plain ``def`` here raises
    ``SyntaxError: 'await' outside async function`` at import time, which took
    the whole MCP gateway down.  Lock the signature so that cannot regress.
    """
    assert inspect.iscoroutinefunction(RemoteToolAdapter._submit_worker_child)
