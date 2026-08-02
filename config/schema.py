from __future__ import annotations

from typing import Any

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "providers": {"type": "object"},
        "lsp": {"type": "object"},
        "max_turns": {"type": "integer", "minimum": 1},
        "persona": {"type": "string", "enum": ["build", "plan", "research"]},
    },
}


def validate_schema(config: dict[str, Any]) -> list[str]:
    """Return list of validation errors (empty = valid)."""
    errors: list[str] = []
    if not isinstance(config, dict):
        errors.append("config must be a dict")
        return errors
    if "max_turns" in config and not isinstance(config["max_turns"], int):
        errors.append("max_turns must be int")
    if "persona" in config and config["persona"] not in ("build", "plan", "research"):
        errors.append(f"unknown persona: {config['persona']}")
    return errors
