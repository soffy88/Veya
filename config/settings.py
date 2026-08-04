"""
config/settings.py — settings loader (compat shim)

Legacy module restored for backward compatibility with E2E tests that
import `from config.settings import load_settings`. Delegates to the
canonical loader in `config.loader`.
"""

from __future__ import annotations

from typing import Any

from config.loader import load_config


def load_settings(path: str | None = None) -> dict[str, Any]:
    """Load settings as a dict (delegates to config.loader.load_config)."""
    return load_config(path)


__all__ = ["load_settings"]
