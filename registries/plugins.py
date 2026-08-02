from __future__ import annotations

from typing import Callable

_PLUGIN_REGISTRY: dict[str, Callable] = {}


def register_plugin(name: str, fn: Callable) -> None:
    _PLUGIN_REGISTRY[name] = fn


def get_plugin(name: str) -> Callable | None:
    return _PLUGIN_REGISTRY.get(name)


def list_plugins() -> list[str]:
    return list(_PLUGIN_REGISTRY)
