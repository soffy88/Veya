"""tui/widgets/statusbar.py — Status bar: persona / cost / session_id / provider."""

from __future__ import annotations

import os

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static


class StatusBar(Widget):
    """Single-line status bar docked at the bottom of the app."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $primary-darken-2;
        layout: horizontal;
        padding: 0 1;
        dock: bottom;
    }
    .sb-item { width: auto; margin-right: 3; color: $text-muted; }
    .sb-cost { color: magenta; }
    .sb-session { color: $accent; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._persona = "build"
        self._cost = 0.0
        self._session_id = ""
        self._provider = os.environ.get("HICODE_LLM_PROVIDER", "dashscope")

    def compose(self) -> ComposeResult:
        yield Static(self._render_persona(), id="sb-persona", markup=True, classes="sb-item")
        yield Static(self._render_cost(), id="sb-cost", markup=True, classes="sb-item sb-cost")
        yield Static(
            self._render_session(), id="sb-session", markup=True, classes="sb-item sb-session"
        )
        yield Static(f"provider:{self._provider}", id="sb-provider", markup=True, classes="sb-item")

    def _render_persona(self) -> str:
        return f"[bold]persona:[/bold]{self._persona}"

    def _render_cost(self) -> str:
        return f"cost:${self._cost:.5f}"

    def _render_session(self) -> str:
        sid = self._session_id[:8] + "…" if len(self._session_id) > 8 else self._session_id
        return f"session:{sid}" if sid else "session:—"

    def update_persona(self, persona: str) -> None:
        self._persona = persona
        try:
            self.query_one("#sb-persona", Static).update(self._render_persona())
        except Exception:
            pass

    def update_cost(self, cost: float) -> None:
        self._cost = cost
        try:
            self.query_one("#sb-cost", Static).update(self._render_cost())
        except Exception:
            pass

    def update_session(self, session_id: str) -> None:
        self._session_id = session_id
        try:
            self.query_one("#sb-session", Static).update(self._render_session())
        except Exception:
            pass
