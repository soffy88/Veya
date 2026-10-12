"""W12 Supervisor — supervised long tasks (flag VEYA_SUPERVISOR=1).

A supervised goal is an existing GoalRun + an existing routine bound to it.
The binding record persists with the routine registry; no new scheduler,
event store, or capability registry is introduced.

3O paradigm: the MasterAgent is the semantic authority; the GoalRun is the
execution authority. The supervisor only observes GoalRun state and posts
progress reports — it never invents progress or creates a second runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from server.goal_run.bot_identity import DEFAULT_BOT_ID

#: The env-var flag that gates the supervisor (default OFF).
_SUPERVISOR_ENV = "VEYA_SUPERVISOR"


def _flag_on() -> bool:
    import os

    return os.environ.get(_SUPERVISOR_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    """Is the supervisor feature flag on?"""
    return _flag_on()


__all__ = ["enabled"]
