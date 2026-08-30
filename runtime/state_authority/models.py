"""runtime/state_authority/models — read-only MemoryView.

PR-07 PHASE 6: a unified, read-only entry point so that session projection /
MasterAgent never holds a full memory object (semantic preference or
preference) inside ``session_tree``.  Only lightweight ``MemoryRef`` instances
are allowed in the projection; resolution happens on demand through
``resolve_memory_ref`` / ``MemoryView``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .memory_refs import MemoryRef, resolve_memory_ref


@dataclass(frozen=True)
class MemoryView:
    """Snapshot of memory references available to a session.

    This is intentionally a bag of *refs*, not a bag of full memory records.
    Storing full memory payloads in session_tree turns the projection into a
    stale authoritative copy and violates SA-06.
    """

    semantic: list[MemoryRef] = field(default_factory=list)
    preferences: list[MemoryRef] = field(default_factory=list)

    def all_refs(self) -> list[MemoryRef]:
        return [*self.semantic, *self.preferences]

    def resolve(self) -> dict[str, list[dict]]:
        """Resolve the view on demand; the view itself retains refs only."""
        return {
            "semantic": [resolve_memory_ref(ref) for ref in self.semantic],
            "preferences": [resolve_memory_ref(ref) for ref in self.preferences],
        }
