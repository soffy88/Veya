"""tui/widgets/input.py — Input widget with submit / persona switch support."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static


class HicodeInput(Widget):
    """Input area: single-line prompt + persona indicator.

    Enter      → submit (triggers Submit message)
    Ctrl+L     → clear input
    """

    DEFAULT_CSS = """
    HicodeInput {
        height: 3;
        border: round $primary-lighten-2;
        padding: 0 1;
        layout: horizontal;
    }
    #persona-badge {
        width: auto;
        color: $accent;
        text-style: bold;
        padding: 0 1 0 0;
    }
    #prompt-input {
        width: 1fr;
        border: none;
        background: transparent;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+l", "clear_input", "Clear", show=False),
    ]

    class Submit(Message):
        """Fired when the user submits a command."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, persona: str = "build", **kwargs) -> None:
        super().__init__(**kwargs)
        self._persona = persona

    def compose(self) -> ComposeResult:
        yield Static(f"[{self._persona}]", id="persona-badge", markup=True)
        yield Input(placeholder="Enter a coding task…", id="prompt-input")

    def update_persona(self, persona: str) -> None:
        self._persona = persona
        try:
            badge = self.query_one("#persona-badge", Static)
            badge.update(f"[{persona}]")
        except Exception:
            pass

    def action_clear_input(self) -> None:
        try:
            inp = self.query_one("#prompt-input", Input)
            inp.value = ""
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            self.post_message(self.Submit(text))
            event.input.value = ""
