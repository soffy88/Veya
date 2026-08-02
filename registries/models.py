from __future__ import annotations

try:
    from obase import ProviderRegistry
    _registry = ProviderRegistry.get()
except Exception:
    _registry = None


def list_models() -> list[str]:
    if _registry is None:
        return []
    try:
        return list(_registry.list_models())
    except Exception:
        return []
