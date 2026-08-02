from __future__ import annotations

from typing import Callable


def get_registered_tools() -> dict[str, Callable]:
    """Return the canonical tool map (mirrors assembly._ALL_TOOLS)."""
    from server.assembly import _ALL_TOOLS
    return dict(_ALL_TOOLS)
