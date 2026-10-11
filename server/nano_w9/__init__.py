"""server/nano_w9 — NANO-W9 layer-4 assembly.

Project service layer for the six NANO-W9 elements.  Its job is **assembly**:
bind the 3O elements to the project's existing stores (P1/P2 groups, the routine
registry/clock, the event store, the action gateway, GoalRun) and expose thin
routes.  It owns no algorithm and no second store.
"""

from __future__ import annotations

__all__ = ["flags"]
