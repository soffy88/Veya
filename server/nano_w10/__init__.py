"""server/nano_w10 — NANO-W10 layer-4 assembly.

Project service layer for the always-on ergonomics work.  Its job is
**assembly**: bind the 3O elements to the project's existing stores (the bot
turn path, the event store, the action gateway, GoalRun, the bot memory store,
the existing secret store) and expose thin routes.  It owns no algorithm and
introduces no second store, scheduler, approval system or capability registry.

Sub-modules:

===============================  =================================================
module                           element
===============================  =================================================
``flags``                        every env flag (default OFF except 7a)
``turn_queue``                   item 1 — priority queue in front of the turns
``wake_router``                  item 2 — one intake for every wake source
``auto_review``                  item 3 — ActionGateway pre-execution review
``prompt_cache``                 item 4 + M — cache discipline, steward
``memory_layers``                item 5 + O — memory layers and review
``a11y``                         item 6 + R — observation ladder and DOM reader
``tool_errors``                  item 7a — agent-friendly tool errors
``eval_flywheel``                item 7b — failure → EvalCase
``search_backends``              addendum T — search fallback order
``channel_ladder``               addendum V — per-channel backend ladders
``bot_secrets``                  addendum Q — per-bot secret cards
``group_roles``                  addendum P — group admin + reply mode
``usage_report``                 addendum S — token usage breakdown
``workers``                      addendum W1/W2 — persistent workers
``loop_mode``                    addendum W3 — loop policy
===============================  =================================================
"""

from __future__ import annotations

__all__ = ["flags"]