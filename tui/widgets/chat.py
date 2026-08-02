"""tui/widgets/chat.py — Chat log widget (conversation history + tool calls)."""
from __future__ import annotations

from textual.widgets import RichLog
from tui.theme import COLORS


class ChatLog(RichLog):
    """Scrollable log of the conversation: user / assistant / tool calls."""

    DEFAULT_CSS = """
    ChatLog {
        height: 1fr;
        scrollbar-gutter: stable;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(markup=True, highlight=False, auto_scroll=True, **kwargs)

    def add_user(self, text: str) -> None:
        self.write(f"[{COLORS['user']}]you>[/] {text}")

    def add_assistant(self, text: str) -> None:
        """Display assistant text response (full or delta)."""
        self.write(f"[{COLORS['assistant']}]{text}[/]")

    def add_tool_call(self, tool_name: str, brief: str = "") -> None:
        line = f"[{COLORS['tool_call']}]  ⚙ {tool_name}[/]"
        if brief:
            line += f"[{COLORS['tool_result']}] ({brief})[/]"
        self.write(line)

    def add_system(self, text: str) -> None:
        self.write(f"[{COLORS['system']}]{text}[/]")

    def add_error(self, text: str) -> None:
        self.write(f"[{COLORS['error']}]✗ {text}[/]")

    def add_diff(self, diff_text: str) -> None:
        """Display a unified diff inline with color coding."""
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                self.write(f"[bold green]{line}[/]")
            elif line.startswith("-") and not line.startswith("---"):
                self.write(f"[bold red]{line}[/]")
            elif line.startswith("@@"):
                self.write(f"[cyan]{line}[/]")
            else:
                self.write(f"[dim]{line}[/]")
