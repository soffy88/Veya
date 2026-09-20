"""Execution process-group ownership.

An executor is spawned in its own process group (`start_new_session=True`) and the
group identity is recorded durably next to the mission, so reconciliation can
terminate exactly the processes that belong to one execution — instead of
guessing from `cwd`/`cmdline` scans.

Identity rules (never weaken these):

* the recorded `pid` must still be the same process (start-time ticks match), and
  the recorded `pgid` must equal that pid (the child is its own group leader);
* we never signal our own process group;
* a bare pgid that happens to exist is not proof of ownership — if the leader is
  gone we require at least one live member with the same pgid, a start time no
  earlier than the record, and the workspace as cwd.

The `cwd`/`cmdline` scanner in :mod:`veya.supervision.reap` remains only as a
legacy fallback for executions started before this lifecycle existed.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

PIDFILE_ENV = "VEYA_EXEC_PIDFILE"


def _proc_start_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    tail = data.rpartition(")")[2].split()
    if len(tail) < 20:
        return None
    try:
        return int(tail[19])  # field 22 (starttime), 0-based after pid/comm/state
    except ValueError:
        return None


def _proc_pgid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            tail = handle.read().decode("utf-8", "replace").rpartition(")")[2].split()
    except OSError:
        return None
    try:
        return int(tail[2])  # field 5 (pgrp)
    except (IndexError, ValueError):
        return None


def _proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _group_members(pgid: int) -> list[int]:
    out: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if _proc_pgid(pid) == pgid:
            out.append(pid)
    return out


def pidfile_for(mission_dir: Path, iteration: int) -> Path:
    return Path(mission_dir) / f"execution-{int(iteration)}.pid.json"


def record(path: Path, *, pid: int, pgid: int, workspace: str = "") -> dict[str, Any]:
    """Persist the group identity of a freshly spawned executor."""
    payload = {
        "pid": int(pid),
        "pgid": int(pgid),
        "pid_start_ticks": _proc_start_ticks(pid),
        "workspace": str(workspace or ""),
        "recorded_at": __import__("time").time(),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(target)
    return payload


def record_current(path: Path | str, proc: Any, workspace: str = "") -> None:
    """Record the process group of `proc` (no-op when the hook is absent)."""
    if not path:
        return
    pid = int(getattr(proc, "pid", 0) or 0)
    if pid <= 1:
        return
    pgid = _proc_pgid(pid) or pid
    record(Path(path), pid=pid, pgid=pgid, workspace=workspace)


def read(path: Path | str) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def terminate(path: Path | str, workspace: str = "") -> dict[str, Any]:
    """Kill the recorded process group, after proving it is ours.

    Returns a small audit dict: {"killed": n, "pids": [...], "refused": reason|None}.
    """
    rec = read(path)
    if not rec:
        return {"killed": 0, "pids": [], "refused": "no_record"}
    pid, pgid = int(rec.get("pid", 0)), int(rec.get("pgid", 0))
    if pid <= 1 or pgid <= 1:
        return {"killed": 0, "pids": [], "refused": "invalid_record"}
    mine = os.getpgid(0)
    if pgid == mine or pid == os.getpid():
        return {"killed": 0, "pids": [], "refused": "own_process_group"}

    alive_pid = os.path.exists(f"/proc/{pid}")
    if alive_pid:
        if _proc_start_ticks(pid) != rec.get("pid_start_ticks") or _proc_pgid(pid) != pgid:
            return {"killed": 0, "pids": [], "refused": "identity_mismatch"}
        members = _group_members(pgid)
    else:
        # Leader gone: require live members that cannot predate the record.
        recorded_ticks = int(rec.get("pid_start_ticks") or 0)
        wanted = str(workspace or rec.get("workspace") or "")
        members = [
            m
            for m in _group_members(pgid)
            if (_proc_start_ticks(m) or 0) >= recorded_ticks
            and (not wanted or _proc_cwd(m) == str(Path(wanted).resolve()))
        ]
        if not members:
            return {"killed": 0, "pids": [], "refused": "unverified_group"}

    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        # fall back to the individual members we verified
        killed: list[int] = []
        for pid_ in members:
            try:
                os.kill(pid_, signal.SIGKILL)
                killed.append(pid_)
            except OSError:
                continue
        return {"killed": len(killed), "pids": killed, "refused": None}
    return {"killed": len(members), "pids": members, "refused": None}


__all__ = [
    "PIDFILE_ENV",
    "pidfile_for",
    "read",
    "record",
    "record_current",
    "terminate",
]
