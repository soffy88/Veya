"""Execution process-group ownership.

# 3O-IO-ALLOW: infrastructure boundary owns /proc identity inspection, pidfile
# persistence, and process-group signalling for durable execution recovery.

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

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

PIDFILE_ENV = "VEYA_EXEC_PIDFILE"

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# External-CLI watchdog
#
# A CLI agent engine may stop producing output for two very different reasons:
# it is thinking (healthy, possibly for minutes) or it is wedged (a deadlock on
# a pipe, a crashed helper still holding the terminal). Only the second is a
# failure, and only the *absence of any output* distinguishes them. The
# watchdog therefore measures silence, not runtime: it terminates the process
# group after ``idle_timeout`` seconds without a byte on stdout/stderr, while a
# separate, larger wall-clock deadline bounds the total run.
#
# Defaults come from the engine capability inventory
# (:mod:`server.cli_capabilities`); these env switches only override them:
#
#   VEYA_CLI_WATCHDOG=0          disable the idle guard (wall clock still applies)
#   VEYA_CLI_IDLE_TIMEOUT_S=...  idle deadline; <= 0 disables the idle guard
#   VEYA_CLI_KILL_GRACE_S=...    seconds between SIGTERM and SIGKILL (default 5)
#
# ``VEYA_CLI_WATCHDOG=0`` is an escape hatch, not a default: with it off a
# silent engine can hold a slot forever, so it is documented as a debugging
# switch and left on.
# ---------------------------------------------------------------------------

WATCHDOG_ENV = "VEYA_CLI_WATCHDOG"
IDLE_TIMEOUT_ENV = "VEYA_CLI_IDLE_TIMEOUT_S"
KILL_GRACE_ENV = "VEYA_CLI_KILL_GRACE_S"
DEFAULT_KILL_GRACE_S = 5.0

_FALSEY = frozenset({"0", "false", "off", "no", "disabled", "none"})


class WatchdogReason(StrEnum):
    """Why supervision ended: normal exit or one of the two deadlines."""

    EXITED = "exited"
    IDLE_TIMEOUT = "idle_timeout"
    WALL_TIMEOUT = "wall_timeout"


@dataclass(frozen=True, slots=True)
class WatchdogOutcome:
    """Result of :func:`supervise_process`, including what was signalled."""

    reason: WatchdogReason
    elapsed_s: float
    idle_timeout_s: float | None
    wall_timeout_s: float
    signal: str = "none"
    pids: tuple[int, ...] = ()


def watchdog_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Idle guard on unless explicitly switched off (default on)."""
    raw = (env or os.environ).get(WATCHDOG_ENV)
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in _FALSEY


def watchdog_idle_timeout(default: float) -> float | None:
    """Idle deadline in seconds; ``None`` means "no idle guard".

    ``VEYA_CLI_IDLE_TIMEOUT_S`` overrides the capability default. A malformed
    value falls back to the default with a warning instead of failing a run
    that a typo in an env var has nothing to do with; a value ``<= 0`` turns
    the guard off deliberately.
    """
    raw = os.environ.get(IDLE_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return float(default) if default and default > 0 else None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %.0fs", IDLE_TIMEOUT_ENV, raw, default)
        return float(default) if default and default > 0 else None
    if value <= 0:
        return None
    return value


def watchdog_kill_grace_s() -> float:
    """Seconds a terminated group gets between SIGTERM and SIGKILL."""
    raw = os.environ.get(KILL_GRACE_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_KILL_GRACE_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "%s=%r is not a number; using %.0fs", KILL_GRACE_ENV, raw, DEFAULT_KILL_GRACE_S
        )
        return DEFAULT_KILL_GRACE_S


class ActivityClock:
    """Monotonic "last output" stamp shared by the stdout/stderr readers."""

    __slots__ = ("_last", "_now")

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._last = now()

    def touch(self) -> None:
        self._last = self._now()

    def idle_for(self) -> float:
        return max(0.0, self._now() - self._last)


def _proc_state(pid: int) -> str | None:
    """Process state letter from /proc (``Z`` means exited, not running)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            tail = handle.read().decode("utf-8", "replace").rpartition(")")[2].split()
    except OSError:
        return None
    return tail[0] if tail else None


def _proc_alive(pid: int) -> bool:
    state = _proc_state(pid)
    return state is not None and state != "Z"


def _live_group_members(pgid: int) -> list[int]:
    return [pid for pid in _group_members(pgid) if _proc_alive(pid)]


def _owned_group(pid: int) -> int | None:
    """The child's process group, if it leads its own group (never ours)."""
    pgid = _proc_pgid(pid)
    if pgid is None or pgid != pid or pgid == os.getpgrp():
        return None
    return pgid


def _send_signal(pid: int, pgid: int | None, sig: int) -> None:
    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        # Leader already gone while descendants remain: target the leader too.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)


async def terminate_process(
    proc: Any,
    *,
    grace_s: float | None = None,
    poll_interval_s: float = 0.1,
) -> dict[str, Any]:
    """SIGTERM the child's group, wait out the grace, then SIGKILL survivors.

    Returns ``{"signal": "sigterm"|"sigkill"|"none", "group": bool, "pids": [...]}``.
    The group is signalled only when the child provably leads it; otherwise the
    leader alone is terminated, so a mis-grouped spawn can never take out an
    unrelated process group.
    """
    pid = int(getattr(proc, "pid", 0) or 0)
    if pid <= 1:
        return {"signal": "none", "group": False, "pids": []}
    if os.name != "posix":
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        return {"signal": "sigterm", "group": False, "pids": [pid]}

    pgid = _owned_group(pid)
    grace = watchdog_kill_grace_s() if grace_s is None else max(0.0, float(grace_s))
    members = _live_group_members(pgid) if pgid is not None else ([pid] if _proc_alive(pid) else [])
    _send_signal(pid, pgid, signal.SIGTERM)

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        survivors = (
            _live_group_members(pgid) if pgid is not None else ([pid] if _proc_alive(pid) else [])
        )
        if not survivors:
            return {"signal": "sigterm", "group": pgid is not None, "pids": members}
        await asyncio.sleep(poll_interval_s)

    survivors = (
        _live_group_members(pgid) if pgid is not None else ([pid] if _proc_alive(pid) else [])
    )
    if survivors:
        _send_signal(pid, pgid, signal.SIGKILL)
        return {"signal": "sigkill", "group": pgid is not None, "pids": survivors}
    return {"signal": "sigterm", "group": pgid is not None, "pids": members}


def _watchdog_poll_interval(idle_timeout_s: float | None, wall_timeout_s: float) -> float:
    budget = idle_timeout_s if idle_timeout_s else wall_timeout_s
    return min(0.5, max(0.01, float(budget) / 10.0))


async def supervise_process(
    proc: Any,
    activity: ActivityClock,
    *,
    idle_timeout_s: float | None,
    wall_timeout_s: float,
    poll_interval_s: float | None = None,
) -> WatchdogOutcome:
    """Watch ``proc`` until it exits or one of the deadlines fires.

    On timeout the process group is terminated before returning, so the caller
    never sees a "timed out" outcome with a live child behind it.
    """
    started = time.monotonic()
    wall = max(0.0, float(wall_timeout_s))
    idle = None if idle_timeout_s is None else max(0.0, float(idle_timeout_s))
    interval = poll_interval_s or _watchdog_poll_interval(idle, wall)

    while True:
        elapsed = time.monotonic() - started
        if getattr(proc, "returncode", None) is not None:
            return WatchdogOutcome(WatchdogReason.EXITED, elapsed, idle, wall)
        if elapsed >= wall:
            term = await terminate_process(proc)
            return WatchdogOutcome(
                WatchdogReason.WALL_TIMEOUT,
                time.monotonic() - started,
                idle,
                wall,
                str(term["signal"]),
                tuple(term["pids"]),
            )
        if idle is not None and idle > 0 and activity.idle_for() >= idle:
            term = await terminate_process(proc)
            return WatchdogOutcome(
                WatchdogReason.IDLE_TIMEOUT,
                time.monotonic() - started,
                idle,
                wall,
                str(term["signal"]),
                tuple(term["pids"]),
            )
        await asyncio.sleep(min(interval, max(0.01, wall - elapsed)))


__all__ = [
    "DEFAULT_KILL_GRACE_S",
    "IDLE_TIMEOUT_ENV",
    "KILL_GRACE_ENV",
    "PIDFILE_ENV",
    "WATCHDOG_ENV",
    "ActivityClock",
    "WatchdogOutcome",
    "WatchdogReason",
    "pidfile_for",
    "read",
    "record",
    "record_current",
    "supervise_process",
    "terminate",
    "terminate_process",
    "watchdog_enabled",
    "watchdog_idle_timeout",
    "watchdog_kill_grace_s",
]
