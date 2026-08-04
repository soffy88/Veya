"""tui/widgets/diff.py — Diff viewer panel (shows compute_diff output inline)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog, Static


class DiffViewer(Widget):
    """Collapsible panel showing the latest unified diff with color coding."""

    DEFAULT_CSS = """
    DiffViewer {
        height: 0;
        border: round yellow;
        padding: 0 1;
        display: none;
    }
    DiffViewer.visible {
        height: 8;
        display: block;
    }
    #diff-title { color: yellow; text-style: bold; }
    #diff-log { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        yield Static("📄 Diff", id="diff-title", markup=True)
        yield RichLog(id="diff-log", markup=True, highlight=False, auto_scroll=True)

    def show_diff(self, diff_text: str, path: str = "") -> None:
        """Display a unified diff with green/red line coloring."""
        if not diff_text.strip():
            return

        self.add_class("visible")
        log = self.query_one("#diff-log", RichLog)
        log.clear()
        if path:
            log.write(f"[bold]{path}[/]")

        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                log.write(f"[bold green]{line}[/]")
            elif line.startswith("-") and not line.startswith("---"):
                log.write(f"[bold red]{line}[/]")
            elif line.startswith("@@"):
                log.write(f"[cyan]{line}[/]")
            else:
                log.write(f"[dim]{line}[/]")

    def hide(self) -> None:
        self.remove_class("visible")
        try:
            self.query_one("#diff-log", RichLog).clear()
        except Exception:
            pass
