"""W16 Project Brain — Hark-style autonomous project mainbrain.

Gated by feature flag VEYA_PROJECT_BRAIN (default OFF).
All storage is a projection over existing .veya-project/ files and BotRegistry;
no new scheduler, event store, approval path, or capability registry is introduced.
"""

from __future__ import annotations

from server.feature_flags import enabled as _flag_enabled

PROJECT_BRAIN_FLAG = "VEYA_PROJECT_BRAIN"


def enabled() -> bool:
    """Return whether the project brain feature is enabled."""
    try:
        return _flag_enabled(PROJECT_BRAIN_FLAG)
    except Exception:
        return False


__all__ = ["PROJECT_BRAIN_FLAG", "enabled"]
