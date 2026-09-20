"""Reap leftover executor processes when an execution is reconciled.

`PR_SET_PDEATHSIG` (server/process_guard.py) covers the common case, but some
executors re-exec or fork their own workers, which loses the parent-death signal
and leaves an orphan running for an execution reconciliation already marked
interrupted. Such an orphan would keep producing side effects that a later retry
duplicates.

This module closes that gap from the supervision side: at reconciliation we kill
processes that (a) have the mission workspace as their cwd and (b) match the
executor's process marker. Both conditions together keep it from touching
unrelated processes.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

# Process-name markers per executor kind. An empty tuple means "cannot identify
# safely" -> we never guess, we just report nothing.
EXECUTOR_MARKERS: dict[str, tuple[str, ...]] = {
    "hicode": ("reasonix",),
    "dsh": ("dsh",),
    "worker": (),
    "builtin": (),
    "native_tool": (),
}


def _proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().decode("utf-8", "replace").replace("\0", " ")
    except OSError:
        return ""


def find_orphans(workspace: str, executor: str) -> list[int]:
    """PIDs that look like a leftover executor for this workspace."""
    markers = EXECUTOR_MARKERS.get(str(executor or "").lower(), ())
    if not markers:
        return []
    wanted = str(Path(workspace).resolve()) if workspace else ""
    if not wanted:
        return []
    mine = os.getpid()
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == mine:
            continue
        if _proc_cwd(pid) != wanted:
            continue
        cmdline = _proc_cmdline(pid)
        if any(marker in cmdline for marker in markers):
            found.append(pid)
    return found


def _ppid_map() -> dict[int, int]:
    """pid -> ppid for every process (from /proc/<pid>/stat, tolerant of comm)."""
    out: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                data = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        tail = data.rpartition(")")[2].split()
        if len(tail) > 1:
            out[int(entry)] = int(tail[1])
    return out


def _with_descendants(roots: list[int]) -> list[int]:
    """An orphan's children (bwrap, workers, ...) are orphans too."""
    if not roots:
        return []
    ppids = _ppid_map()
    targets = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, ppid in ppids.items():
            if ppid in targets and pid not in targets:
                targets.add(pid)
                changed = True
    return sorted(targets)


def reap_orphans(workspace: str, executor: str) -> list[int]:
    """SIGKILL leftover executors and their descendants; returns PIDs signalled."""
    targets = _with_descendants(find_orphans(workspace, executor))
    killed: list[int] = []
    for pid in reversed(targets):  # children first
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except OSError:
            continue
    return killed


__all__ = ["EXECUTOR_MARKERS", "find_orphans", "reap_orphans"]
