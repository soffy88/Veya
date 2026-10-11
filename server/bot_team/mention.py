"""@runner mention routing (NANO-W6).

A chat message that starts with ``@<runner-name>`` is forwarded to that
runner instead of executing here. Name matching is case-insensitive against
the P6 runner registry (runner ids and display names). An unknown name gets a
helpful error listing the registered runners — never a silent fallback to
local execution.
"""

from __future__ import annotations

from typing import Any

from server.bot_team.runners import RunnerRegistry, runner_registry
from veya.oskill.pure.mention import parse_runner_mention


def route_text_to_runner(
    text: str, *, registry: RunnerRegistry | None = None
) -> dict[str, Any] | None:
    """Parse a leading @runner mention and resolve it against the registry.

    → None when the text is not a mention;
    → {"runner_id", "name", "rest"} on a match;
    → {"error", "runners"} when the name is unknown.
    """

    mention = parse_runner_mention(text)
    if mention is None:
        return None
    reg = registry if registry is not None else runner_registry()
    wanted = str(mention["name"] or "").lower()
    for record in reg.list():
        names = {str(record.runner_id).lower(), str(record.name).lower()}
        if wanted in names:
            return {
                "runner_id": record.runner_id,
                "name": record.name,
                "rest": str(mention["rest"] or ""),
            }
    return {
        "error": f"unknown runner {wanted!r}",
        "runners": reg.views(),
    }


__all__ = ["route_text_to_runner"]
