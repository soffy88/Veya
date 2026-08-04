from __future__ import annotations

from collections.abc import Callable


def get_registered_tools() -> dict[str, Callable]:
    """Return the canonical tool map (mirrors assembly._ALL_TOOLS)."""
    from server.assembly import _ALL_TOOLS

    return dict(_ALL_TOOLS)


def register_tool(name: str, fn: Callable) -> None:
    """Register a tool into the canonical tool map (G10 plugin SDK)."""
    from server.assembly import _ALL_TOOLS

    _ALL_TOOLS[name] = fn


def list_tools() -> list[str]:
    """List currently registered tool names."""
    return list(get_registered_tools())
