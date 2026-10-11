"""server/nano_w10/flags — NANO-W10 feature flags.

Every mutating surface is gated. An **off** flag never crashes and never
silently no-ops: callers get :class:`FeatureDisabled` and routes render
``{"code": "FEATURE_DISABLED", "flag": ...}`` so an operator can tell
"disabled" from "broken".

Per the spec ("Flags default OFF except 7a") every flag below defaults OFF
except the three read-only surfaces the addendum explicitly allows to be on:

===============================  ========  ===================================
flag                             default   guards
===============================  ========  ===================================
``VEYA_TURN_QUEUE``              off       item 1 turn queue
``VEYA_WAKE_ROUTER``             off       item 2 wake routing
``VEYA_AUTO_REVIEW``             off       item 3 command auto-review
``VEYA_PROMPT_CACHE``            off       item 4 prompt-cache discipline
``VEYA_CONTEXT_STEWARD``         off       addendum M context steward
``VEYA_MEMORY_REVIEW``           off       addendum O memory review
``VEYA_A11Y_FIRST``              off       item 6 a11y-first observation
``VEYA_DOM_READER``              off       addendum R DOM reader
``VEYA_GROUP_ROLES``             off       addendum P group roles
``VEYA_BOT_SECRETS``             off       addendum Q per-bot secrets
``VEYA_CHANNEL_LADDER``          off       addendum V channel ladder order
``VEYA_PERSISTENT_WORKERS``      off       addendum W1 persistent workers
``VEYA_LOOP_MODE``               off       addendum W3 loop mode
``VEYA_LIVE_STREAM``             **on**    addendum W4 live stream + correction
``VEYA_TOOL_ERROR_FRIENDLY``     **on**    item 7a agent-friendly errors
``VEYA_LLM_TAP``                 **on**    addendum U logging pass-through
``VEYA_SEARCH_FALLBACK``         **on**    addendum T search fallback
``VEYA_CHANNEL_STATUS``          **on**    addendum V read-only status/doctor
===============================  ========  ===================================

``VEYA_BOT_MEMORY_MD`` is not listed here: it already exists on
:mod:`server.bot_team.memory` and keeps its own home (spec item 5: "Flag stays
``VEYA_BOT_MEMORY_MD``").
"""

from __future__ import annotations

import os
from typing import Any

__all__ = [
    "ALL_FLAGS",
    "FEATURE_DISABLED_ERROR",
    "FeatureDisabled",
    "feature_flags",
    "flag_enabled",
    "require_flag",
]

#: flag name -> default enabled
ALL_FLAGS: dict[str, bool] = {
    "VEYA_TURN_QUEUE": False,
    "VEYA_WAKE_ROUTER": False,
    "VEYA_AUTO_REVIEW": False,
    "VEYA_PROMPT_CACHE": False,
    "VEYA_CONTEXT_STEWARD": False,
    "VEYA_MEMORY_REVIEW": False,
    "VEYA_A11Y_FIRST": False,
    "VEYA_DOM_READER": False,
    "VEYA_GROUP_ROLES": False,
    "VEYA_BOT_SECRETS": False,
    "VEYA_CHANNEL_LADDER": False,
    "VEYA_PERSISTENT_WORKERS": False,
    "VEYA_LOOP_MODE": False,
    "VEYA_LIVE_STREAM": True,
    "VEYA_TOOL_ERROR_FRIENDLY": True,
    "VEYA_LLM_TAP": True,
    "VEYA_SEARCH_FALLBACK": True,
    "VEYA_CHANNEL_STATUS": True,
}

FEATURE_DISABLED_ERROR = {"code": "FEATURE_DISABLED", "flag": ""}

_FALSY = frozenset({"", "0", "false", "off", "no"})


class FeatureDisabled(RuntimeError):
    """Raised when a caller touches a surface whose flag is off."""

    def __init__(self, flag: str) -> None:
        self.flag = flag
        super().__init__(f"{flag} is disabled")

    def as_error(self) -> dict[str, Any]:
        """Machine-readable error body (same shape on every W10 route)."""
        return {"code": "FEATURE_DISABLED", "flag": self.flag}


def flag_enabled(flag: str, *, default: bool = False) -> bool:
    """Is ``flag`` on?

    Args:
        flag: one of :data:`ALL_FLAGS`.
        default: value used when the env var is unset.

    Returns:
        Parsed truthiness (``""``/``"0"``/``"false"``/``"off"``/``"no"`` are off).

    Example:
        >>> flag_enabled("VEYA_LOOP_MODE", default=False) in (True, False)
        True
    """
    raw = os.environ.get(flag)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSY


def require_flag(flag: str) -> None:
    """Raise :class:`FeatureDisabled` unless ``flag`` is on.

    Args:
        flag: one of :data:`ALL_FLAGS`.

    Raises:
        FeatureDisabled: when the flag is off.
    """
    if not flag_enabled(flag):
        raise FeatureDisabled(flag)


def feature_flags() -> dict[str, bool]:
    """Current state of every W10 flag (CLI + health surface).

    Returns:
        ``{flag: bool}`` over :data:`ALL_FLAGS`.

    Example:
        >>> "VEYA_TURN_QUEUE" in feature_flags()
        True
    """
    return {flag: flag_enabled(flag, default=default) for flag, default in ALL_FLAGS.items()}