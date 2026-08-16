"""
tui/stream.py — Bridge between coordinator on_step events and Textual widgets.

The coordinator fires on_step(event: dict) callbacks via a ContextVar.
This module provides a StreamAdapter that the TUI App uses to create
the on_step callback and route events to the correct widgets.

Event types fired by coordinator/assembly:
  session_start  — {"type": "session_start", "session_id": str}
  squad_start    — {"type": "squad_start", "squad_id": str, "role": str}
  squad_done     — {"type": "squad_done", "squad_id": str, "role": str, "status": str, "cost_usd": float}
  tool_call      — {"type": "tool_call", "tool_name": str, "tool_args": dict}
  text_delta     — {"type": "text_delta", "squad_id": str, "text": str}
  cost_update    — {"type": "cost_update", "total_cost": float, "session_id": str}
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tui.app import HicodeApp


class StreamAdapter:
    """Creates the on_step callback that routes events to TUI widgets."""

    def __init__(self, app: HicodeApp) -> None:
        self._app = app

    def make_on_step(self) -> Callable[[dict], None]:
        """Return a sync callback suitable for coordinator's on_step parameter."""
        app = self._app

        def on_step(event: dict[str, Any]) -> None:
            event_type = event.get("type", "")
            squad_id = event.get("squad_id", "")

            if event_type == "session_start":
                pass  # App already knows the session_id from coordinator return

            elif event_type == "squad_start":
                role = event.get("role", "")
                squads = app.query_one("#squads-panel")
                squads.add_squad(squad_id, role)
                chat = app.query_one("#chat-log")
                chat.add_system(f"▶ [{role}] squad started")

            elif event_type == "tool_call":
                tool_name = event.get("tool_name", "")
                tool_args = event.get("tool_args", {})
                # Show brief args (first key=value)
                brief = ", ".join(f"{k}={str(v)[:30]!r}" for k, v in list(tool_args.items())[:2])
                squads = app.query_one("#squads-panel")
                sid = squad_id or "master"
                squads.add_squad(sid, sid)
                squads.update_squad(sid, f"→ {tool_name}")
                chat = app.query_one("#chat-log")
                chat.add_tool_call(tool_name, brief)

            elif event_type == "text_delta":
                text = event.get("text", "")
                if text:
                    chat = app.query_one("#chat-log")
                    chat.add_assistant(text)
                    squads = app.query_one("#squads-panel")
                    squads.update_squad(squad_id, "✓ response")

            elif event_type == "tool_error":
                tool_name = event.get("tool_name", "")
                err = str(event.get("error") or "")[:80]
                chat = app.query_one("#chat-log")
                chat.add_tool_call(tool_name, f"✗ {err}")

            elif event_type in ("squad_done", "master_done"):
                role = event.get("role", "master")
                status = event.get("status", "unknown")
                cost = event.get("cost_usd", 0.0)
                squads = app.query_one("#squads-panel")
                if squad_id:
                    squads.complete_squad(squad_id, status, cost)
                elif event_type == "master_done":
                    squads.add_squad("master", "master")
                    squads.complete_squad("master", status, cost)

            elif event_type == "cost_update":
                total = event.get("total_cost", 0.0)
                status_bar = app.query_one("#status-bar")
                status_bar.update_cost(total)

        return on_step
