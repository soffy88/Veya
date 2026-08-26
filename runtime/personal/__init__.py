"""Personal Agent Runtime projections backed by the Execution Runtime database.

This package is intentionally a capability layer below MasterAgent.  It stores
facts, candidates, skill versions, continuity projections, and learning
evidence; it never routes a user request or becomes a second orchestrator.
"""

from .runtime import (
    PersonalRuntimeError,
    PersonalRuntimeStore,
    get_personal_runtime,
    reset_personal_runtime,
)

__all__ = [
    "PersonalRuntimeError",
    "PersonalRuntimeStore",
    "get_personal_runtime",
    "reset_personal_runtime",
]
