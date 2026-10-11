"""server/nano_w9/flags — NANO-W9 feature flags (default OFF).

Every W9 surface is gated by one of these.  An **off** flag never crashes and
never silently no-ops: callers get ``FeatureDisabled`` and turn it into
``404``/``{"status":"error","error":{"code":"FEATURE_DISABLED"}}`` so an
operator can tell "disabled" apart from "broken".

Flags (HARD RULES: "New features behind env flags, default OFF"):

===============================  ========  ==============================
flag                             default   guards
===============================  ========  ==============================
``VEYA_BRANCH_ROOMS``            off       branch rooms (element 1)
``VEYA_ROUTINE_TRIGGERS``        off       routine triggers (element 2)
``VEYA_ACP_MEMBERS``             off       ACP members (element 3)
``VEYA_AUDIT_CHAIN``             off       hash-chained audit (element 4)
``VEYA_CLI_GEN``                 off       agent-native scaffold (addendum F)
===============================  ========  ==============================

Search-with-receipts (element 5) and the agent CLI (element 6) are read-only
surfaces and are therefore **on** — the spec explicitly allows that.
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
    "VEYA_BRANCH_ROOMS": False,
    "VEYA_ROUTINE_TRIGGERS": False,
    "VEYA_ACP_MEMBERS": False,
    "VEYA_AUDIT_CHAIN": False,
    "VEYA_CLI_GEN": False,
}

#: Error code returned by every disabled surface (stable, machine-readable).
FEATURE_DISABLED_ERROR = {"code": "FEATURE_DISABLED", "flag": ""}

_FALSY = frozenset({"", "0", "false", "off", "no"})


class FeatureDisabled(RuntimeError):
    """Raised when a caller touches a surface whose flag is off.

    Carries the flag name so routes can render a precise 404 body.
    """

    def __init__(self, flag: str) -> None:
        self.flag = flag
        super().__init__(f"{flag} is disabled")

    def as_error(self) -> dict[str, Any]:
        """Machine-readable error body (same shape every W9 route returns)."""
        return {"code": "FEATURE_DISABLED", "flag": self.flag}


def flag_enabled(flag: str, *, default: bool = False) -> bool:
    """Is ``flag`` on?

    Args:
        flag: one of :data:`ALL_FLAGS`.
        default: value when the env var is unset.

    Returns:
        The parsed truthiness (``"0"/"false"/"off"/"no"/""`` are all off).

    Example:
        >>> flag_enabled("VEYA_CLI_GEN", default=False) in (True, False)
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

    Example:
        >>> # require_flag("VEYA_BRANCH_ROOMS")
    """
    if not flag_enabled(flag):
        raise FeatureDisabled(flag)


def feature_flags() -> dict[str, bool]:
    """Current state of every W9 flag (for the CLI + the health surface).

    Returns:
        ``{flag: bool}`` over :data:`ALL_FLAGS`.

    Example:
        >>> sorted(feature_flags())[0]
        'VEYA_ACP_MEMBERS'
    """
    return {flag: flag_enabled(flag, default=default) for flag, default in ALL_FLAGS.items()}
