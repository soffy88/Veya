from __future__ import annotations

from collections.abc import Callable

_SKILL_REGISTRY: dict[str, Callable] = {}


def register_skill(name: str, fn: Callable) -> None:
    _SKILL_REGISTRY[name] = fn


def get_skill(name: str) -> Callable | None:
    return _SKILL_REGISTRY.get(name)


def list_skills() -> list[str]:
    return list(_SKILL_REGISTRY)
