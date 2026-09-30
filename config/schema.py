from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from config.authority import KEYS, coerce

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "providers": {"type": "object"},
        "lsp": {"type": "object"},
        "max_turns": {"type": "integer", "minimum": 1},
        "persona": {"type": "string", "enum": ["build", "plan", "research"]},
    },
}

# Keys validated through the ONE canonical authority (same coercion and
# constraints :func:`config.authority.resolve` enforces at runtime).
_AUTHORITY_VALIDATED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("max_turns", ("max_turns",)),
    ("persona", ("persona",)),
    ("llm.provider", ("llm", "provider")),
    ("llm.model", ("llm", "model")),
)


def _walk(config: Mapping[str, Any], path: tuple[str, ...]) -> tuple[Any, bool]:
    node: Any = config
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None, False
        node = node[part]
    return node, True


def validate_schema(config: dict[str, Any]) -> list[str]:
    """Return list of validation errors (empty = valid).

    Legacy checks are preserved verbatim; registered keys are additionally
    checked with the canonical authority coercion (so ``max_turns: 0`` or a
    boolean ``max_turns`` is reported the same way ``resolve`` sees it —
    fail closed instead of silently accepting).
    """
    errors: list[str] = []
    if not isinstance(config, dict):
        errors.append("config must be a dict")
        return errors
    if "max_turns" in config and not isinstance(config["max_turns"], int):
        errors.append("max_turns must be int")
    if "persona" in config and config["persona"] not in ("build", "plan", "research"):
        errors.append(f"unknown persona: {config['persona']}")
    for key, path in _AUTHORITY_VALIDATED:
        raw, present = _walk(config, path)
        if not present or raw is None:
            continue
        _, coercion_errors = coerce(KEYS[key], raw)
        errors.extend(coercion_errors)
    return errors
