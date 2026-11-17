"""Agent Client Protocol (ACP) adapter for external engines."""

from .client import ACPClient, ACPError, ACPResult, acp_enabled, normalize_adapter
from .registry import ACPAdapter, adapter_for, available_adapters

__all__ = [
    "ACPAdapter",
    "ACPClient",
    "ACPError",
    "ACPResult",
    "acp_enabled",
    "adapter_for",
    "available_adapters",
    "normalize_adapter",
]
