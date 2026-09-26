"""Thin, contract-pinned cfKanban provider substrate.

This package contains transport and typed provider contracts only.  Mission
creation, supervision, and completion remain Veya authorities.
"""

from .client import CfKanbanClient
from .models import (
    CfKanbanCursor,
    CfKanbanEvent,
    CfKanbanMutationResult,
    CfKanbanProviderConfig,
    CfKanbanTask,
    CfKanbanTaskRef,
)

__all__ = [
    "CfKanbanClient",
    "CfKanbanCursor",
    "CfKanbanEvent",
    "CfKanbanMutationResult",
    "CfKanbanProviderConfig",
    "CfKanbanTask",
    "CfKanbanTaskRef",
]
