"""server.goal_session_map — session_id ↔ goal_id association for long-task governance.

A conversation opts into GoalKernel-backed budget/todo tracking by calling the
``goal_start`` tool once (server/goal_tools.py); this module is the only place
that remembers which session that goal belongs to, so ``chat_stream``'s
``_long_task_factory`` (server/coordinator_master.py) can pick it back up on
every subsequent turn without the model re-stating goal_id every message.

File-backed rather than in-memory: an association must survive a process
restart, matching chat_stream's own "长任务无损恢复" checkpoint discipline —
losing the association mid-goal would silently stop budget enforcement.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

GOAL_LOOPS_DIR = Path(os.environ.get("VEYA_GOAL_LOOPS_DIR", Path.home() / ".veya" / "loops"))


def _map_path() -> Path:
    GOAL_LOOPS_DIR.mkdir(parents=True, exist_ok=True)
    return GOAL_LOOPS_DIR / "_session_goals.json"


def _read_map() -> dict[str, str]:
    path = _map_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def get_goal_id(session_id: str) -> str | None:
    return _read_map().get(session_id)


def set_goal_id(session_id: str, goal_id: str) -> None:
    path = _map_path()
    data = _read_map()
    data[session_id] = goal_id
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def clear_goal_id(session_id: str) -> None:
    path = _map_path()
    data = _read_map()
    if session_id in data:
        del data[session_id]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
