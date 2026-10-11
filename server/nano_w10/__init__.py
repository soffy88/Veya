"""server/nano_w10 — NANO-W10 layer-4 assembly.

Project service layer for the always-on ergonomics work.  Its job is
**assembly**: bind the 3O elements to the project's existing stores (the bot
turn path, the event store, the action gateway, GoalRun, the bot memory store,
the existing secret store) and expose thin routes.  It owns no algorithm and
introduces no second store, scheduler, approval system or capability registry.

Sub-modules shipped by **group A** (`W10_GROUP_A.md`):

==============================  =================================================
module                           element
==============================  =================================================
``flags``                        every env flag (default OFF except 7a)
``turn_queue``                   item 1 — priority queue in front of the turns
``wake_router``                  item 2 — one intake for every wake source
``loop_mode``                    addendum W3 — Goal vs Loop as a GoalRun policy
``live_stream``                  addendum W4 — live tool results, plan, corrections
==============================  =================================================

Sub-modules owned by the **other groups** in this wave (contract only, not
implemented here): ``auto_review`` (item 3), ``prompt_cache`` (item 4 + M),
``memory_layers`` (item 5 + O), ``a11y`` (item 6 + R), ``tool_errors`` (item
7a), ``eval_flywheel`` (item 7b), ``search_backends`` (addendum T),
``channel_ladder`` (addendum V), ``bot_secrets`` (addendum Q),
``group_roles`` (addendum P), ``usage_report`` (addendum S), ``workers``
(addendum W1/W2).
"""

from __future__ import annotations

__all__ = ["flags"]