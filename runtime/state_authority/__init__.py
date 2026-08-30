"""Runtime state-authority primitives.

These are declarative contracts and thin guards that make Veya's existing
state authority boundaries explicit and enforceable.  They do NOT introduce a
second memory runtime, a second session projector, or rewrites of the existing
stores.  They formalise what is already true:

  * history_store          = immutable historical authority
  * session_tree           = derived projection (conversation | execution)
  * MemoryController/PR    = semantic memory authority
  * memory_bank            = preference memory authority
  * memory_hub             = distillation / retrieval pipeline (non-authoritative)

Where the codebase already owns a stable reference type (history event revision,
goal_run_id, artifact ref), this package does NOT re-wrap it.  It only adds the
one reference type that is genuinely missing: a unified, typed ``MemoryRef``
across the semantic and preference memory domains.
"""

from . import doctor, ownership
from .memory_refs import MemoryDomain, MemoryRef, resolve_memory_ref
from .models import MemoryView
from .session_projection import SessionProjectionWriter

__all__ = [
    "MemoryDomain",
    "MemoryRef",
    "MemoryView",
    "SessionProjectionWriter",
    "doctor",
    "ownership",
    "resolve_memory_ref",
]
