"""W13 feature flag.

Unlike ``server/supervisor/__init__.py:17-28`` (which reads ``os.environ``
directly and therefore ignores the config-file layer), W13 reads through the
canonical authority so ``feature_flags.snapshot()`` and the settings surface
stay truthful.

Fail-closed: an unregistered flag (``KeyError``) is treated as **off**, so a
partial deploy can never half-enable the remote-control surface.
"""

from __future__ import annotations

import os

#: Registered in ``server/feature_flags.py`` as ``False`` (default OFF).
FLAG_NAME = "VEYA_REMOTE_CONTROL"


def enabled() -> bool:
    """Is the remote-control feature flag on?

    Resolution order is the repository's single authority: runtime > env >
    file > default (``config/authority.py:48``).
    """

    from server import feature_flags

    try:
        return feature_flags.enabled(FLAG_NAME)
    except KeyError:
        return False


def require_enabled() -> None:
    """Raise unless the flag is on. Used by routes, tools and stores alike."""

    if not enabled():
        raise RuntimeError(
            f"remote control is disabled; set {FLAG_NAME}=1 to enable"
        )


def env_float(name: str, default: float) -> float:
    """Read a numeric W13 tuning knob, falling back on any bad value."""

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def env_int(name: str, default: int) -> int:
    return int(env_float(name, float(default)))


__all__ = [
    "FLAG_NAME",
    "enabled",
    "env_float",
    "env_int",
    "require_enabled",
]
