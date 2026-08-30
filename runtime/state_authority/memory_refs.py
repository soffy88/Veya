"""runtime/state_authority/memory_refs — unified MemoryRef across memory domains.

PR-07 PHASE 4/5: the three memory lines are NOT merged into one database.  They
keep their separate physical stores:

  * semantic memory   -> MemoryController / Personal Runtime (authority)
  * preference memory  -> memory_bank (authority)
  * distillation       -> memory_hub (pipeline, non-authoritative)

What was missing is a single, typed reference so that projections (session_tree)
and composite retrieval can point at a memory record without copying its payload.
``MemoryRef`` is that reference.  It intentionally does NOT cover history events
(already referenced by ``(session_id, revision)``) or GoalRuns (already referenced
by ``goal_run_id``) or Artifacts (already referenced by ArtifactRef) — re-wrapping
those would be unification for its own sake.

Resolution is delegated to the owning store per ``domain``; this module contains
no second memory backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MemoryDomain = Literal["semantic", "preference"]


@dataclass(frozen=True)
class MemoryRef:
    """Typed, stable reference to a memory record in one of the memory domains.

    Attributes:
        domain:  which memory authority owns the record (semantic | preference)
        id:      stable record id in that authority (e.g. memory_<hash> / mem_<ts>_<hash>)
        version: optional monotonic version / supersession marker
        source:  optional provenance pointer (e.g. session_id or goal_run_id)
    """

    domain: MemoryDomain
    id: str
    version: int | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if self.domain not in ("semantic", "preference"):
            raise ValueError(f"unknown memory domain: {self.domain!r}")
        if not self.id.strip():
            raise ValueError("memory ref id must not be empty")
        if self.version is not None and self.version < 0:
            raise ValueError("memory ref version must be non-negative")


def resolve_memory_ref(ref: MemoryRef) -> dict:
    """Resolve a MemoryRef to its owning authority's current record.

    The resolver dispatches by ``domain`` and returns the live record dict.  If
    the owning authority is unavailable or the ref is dangling, raises KeyError
    (callers decide how to degrade).  This never invents a record.

    semantic  -> Personal Runtime / MemoryController (server.memory_controller)
    preference -> VeyaMemoryBank (server.memory_bank)
    """
    if ref.domain == "semantic":
        from server.memory_controller import memory_controller

        rec = memory_controller.get(ref.id)
        if rec is None:
            raise KeyError(f"semantic memory ref not found: {ref.id}")
        return rec.canonical_dict()
    if ref.domain == "preference":
        from server.memory_bank import memory_bank

        for p in memory_bank.list_preferences():
            if p["id"] == ref.id:
                return p
        raise KeyError(f"preference memory ref not found: {ref.id}")
    raise KeyError(f"unknown memory domain: {ref.domain}")
