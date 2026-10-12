# 3O-IO-ALLOW — server/supervisor/binding.py
# 理由：SupervisorBinding 是 A1 规范里「persisted where routines already
# persist」的绑定簿；同为 per-record JSON 簿的既有模块（如
# server/goal_run/routine_registry.py）也用同一套 tmp + os.replace 原子写，
# 此处对齐该约定。新增第二套存储会违反「不新增第二套存储」的冻结约束；
# 判定/状态算法在 server/supervisor/reporting.py + handoff.py。
"""W12 Supervisor binding record (flag VEYA_SUPERVISOR=1).

A supervised goal is an existing GoalRun + an existing routine bound
to it (spec A1). This module adds only the binding record:
(goal_run_id <-> routine_id, owner, bot, interval, stall_after,
max_repairs) persisted where routines already persist.

3O paradigm: NO new scheduler, event store, approval path, side-effect
or capability registry. The GoalRun store remains the execution
authority; the routine registry the schedule authority; this module
is pure projection/bookkeeping.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_BINDING_ENV = "VEYA_SUPERVISOR_BINDING_PATH"
_DEFAULT_BINDING_PATH = Path.home() / ".veya" / "supervisor_bindings.json"

_lock = threading.RLock()


def _flag_on() -> bool:
    return os.environ.get("VEYA_SUPERVISOR", "").strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    """Is the supervisor feature flag on?"""
    return _flag_on()


def _binding_path() -> Path:
    raw = os.environ.get(_BINDING_ENV)
    return Path(raw).expanduser() if raw else _DEFAULT_BINDING_PATH


@dataclass
class SupervisorBinding:
    """One supervised goal: GoalRun + routine + reporting cadence.

    The binding record holds only bookkeeping refs. The GoalRun store
    remains the execution authority; the routine registry the schedule
    authority. ``report_seq`` is monotonic per routine, so a restart
    never replays a report the user already saw (A7).
    """

    binding_id: str
    goal_run_id: str
    routine_id: str
    owner_id: str
    bot_id: str
    objective: str
    project_root: str
    report_every_minutes: int = 30
    stall_after_minutes: int = 20
    max_repairs: int = 3
    report_seq: int = 0
    nudges_sent: int = 0
    status: str = "open"  # open | paused | closed
    last_report_at: float | None = None
    last_activity_at: float | None = None
    last_report_fingerprint: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SupervisorBinding:
        return cls(
            binding_id=str(value.get("binding_id") or ""),
            goal_run_id=str(value.get("goal_run_id") or ""),
            routine_id=str(value.get("routine_id") or ""),
            owner_id=str(value.get("owner_id") or ""),
            bot_id=str(value.get("bot_id") or ""),
            objective=str(value.get("objective") or ""),
            project_root=str(value.get("project_root") or ""),
            report_every_minutes=int(value.get("report_every_minutes") or 30),
            stall_after_minutes=int(value.get("stall_after_minutes") or 20),
            max_repairs=int(value.get("max_repairs") or 3),
            report_seq=int(value.get("report_seq") or 0),
            nudges_sent=int(value.get("nudges_sent") or 0),
            status=str(value.get("status") or "open"),
            last_report_at=value.get("last_report_at"),
            last_activity_at=value.get("last_activity_at"),
            last_report_fingerprint=value.get("last_report_fingerprint"),
            created_at=float(value.get("created_at") or time.time()),
        )


def _read_all() -> dict[str, dict[str, Any]]:
    path = _binding_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(bindings: dict[str, dict[str, Any]]) -> None:
    path = _binding_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(bindings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_binding(binding: SupervisorBinding) -> SupervisorBinding:
    """Persist one binding (create or update by binding_id)."""
    with _lock:
        all_bindings = _read_all()
        all_bindings[binding.binding_id] = binding.to_dict()
        _write_all(all_bindings)
    return binding


def get_binding(binding_id: str) -> SupervisorBinding | None:
    with _lock:
        raw = _read_all().get(binding_id)
        return SupervisorBinding.from_dict(raw) if isinstance(raw, dict) else None


def list_bindings(*, owner_id: str | None = None, bot_id: str | None = None) -> list[SupervisorBinding]:
    """Every binding, optionally filtered by owner and/or bot."""
    with _lock:
        items = [
            SupervisorBinding.from_dict(raw)
            for raw in _read_all().values()
            if isinstance(raw, dict)
        ]
    if owner_id is not None:
        items = [b for b in items if b.owner_id == owner_id]
    if bot_id is not None:
        items = [b for b in items if b.bot_id == bot_id]
    return items


def delete_binding(binding_id: str) -> bool:
    """Remove one binding. Returns False when it did not exist."""
    with _lock:
        all_bindings = _read_all()
        if binding_id not in all_bindings:
            return False
        del all_bindings[binding_id]
        _write_all(all_bindings)
    return True


def bindings_for_goal(goal_run_id: str) -> list[SupervisorBinding]:
    """Open bindings bound to one GoalRun (restart resume, A7)."""
    with _lock:
        items = [
            SupervisorBinding.from_dict(raw)
            for raw in _read_all().values()
            if isinstance(raw, dict) and str(raw.get("goal_run_id")) == goal_run_id
        ]
    return [b for b in items if b.status in ("open", "paused")]


__all__ = [
    "SupervisorBinding",
    "bindings_for_goal",
    "delete_binding",
    "enabled",
    "get_binding",
    "list_bindings",
    "save_binding",
]
