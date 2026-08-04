"""
server/providers.py — backward-compatible re-export of hicode.llm

The canonical multi-provider LLM client lives in ``hicode/llm.py``. This module
keeps the historical import path (``from server.providers import provider_call``)
working for ``server/assembly.py`` and validation scripts without duplicating logic.
"""

from hicode.llm import (  # noqa: F401
    _DEFAULT_MODELS,
    _ENDPOINTS,
    _PRICING,
    calc_cost,
    get_api_key,
    get_provider_config,
    llm_call,
    llm_stream,
    provider_call,
    provider_stream,
)


def _get_provider(config: dict | None) -> str:
    """Legacy helper: resolve the active provider name from config/env."""
    return get_provider_config(config)[0]
