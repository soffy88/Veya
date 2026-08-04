from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
    from veya.compat import permission_evaluate  # type: ignore[attr-defined]

    _HAS_PERMISSION_EVALUATE = True
except (ImportError, AttributeError):
    _HAS_PERMISSION_EVALUATE = False


_RULES_BY_PERSONA: dict[str, list[str]] = {
    "build": ["allow:*", "ask:bash"],
    "plan": ["deny:write", "deny:edit", "deny:bash", "allow:*"],
    "research": ["deny:write", "deny:edit", "deny:bash", "allow:*"],
}


def load_permission_rules(persona: str) -> list[Callable]:
    rules = _RULES_BY_PERSONA.get(persona, _RULES_BY_PERSONA["build"])

    def filter_fn(tool_call: dict[str, Any]) -> dict[str, Any]:
        if _HAS_PERMISSION_EVALUATE:
            return permission_evaluate(tool_call, rules=rules, persona=persona)  # type: ignore[name-defined]
        return {"allowed": True}

    return [filter_fn]
