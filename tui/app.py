"""
tui/app.py — HicodeApp: Textual TUI main application

Layout:
  ┌─────────────────────────────────────────────────┐
  │  Header                                         │
  ├──────────────────────────┬──────────────────────┤
  │ Chat log (2fr)           │ Squads panel (1fr)   │
  │  user / assistant /      │  per-squad blocks    │
  │  tool calls inline       │  role + status + $   │
  ├──────────────────────────┴──────────────────────┤
  │ Diff viewer (collapsible, height 0 → 8)         │
  ├─────────────────────────────────────────────────┤
  │ Input: [persona] prompt_______________________  │
  ├─────────────────────────────────────────────────┤
  │ StatusBar: persona | cost | session | provider  │
  └─────────────────────────────────────────────────┘

Architecture rule: TUI never calls oprim/omodul/engine directly.
All logic flows through coordinator.handle() — same as headless.
"""

from __future__ import annotations

from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header

from tui.theme import TCSS
from tui.widgets.chat import ChatLog
from tui.widgets.diff import DiffViewer
from tui.widgets.input import HicodeInput
from tui.widgets.squads import SquadsPanel
from tui.widgets.statusbar import StatusBar

_PERSONAS = ["build", "research", "plan", "execute"]


class HicodeApp(App):
    """veya TUI — calls coordinator.handle() for all logic."""

    TITLE = "veya"
    SUB_TITLE = "AI coding agent"
    CSS = TCSS

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+x", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+p", "cycle_persona", "Persona", show=True),
        Binding("ctrl+n", "new_session", "New session", show=True),
        Binding("f1", "toggle_squads", "Squads", show=True),
        Binding("escape", "cancel_task", "Cancel", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._persona = "build"
        self._session_id: str | None = None
        self._total_cost: float = 0.0
        self._persona_idx: int = 0
        self._task_running: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-pane"):
            with Vertical(id="chat-panel"):
                yield ChatLog(id="chat-log")
            yield SquadsPanel(id="squads-panel")
        yield DiffViewer(id="diff-panel")
        yield HicodeInput(persona=self._persona, id="input-area")
        yield StatusBar(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        chat.add_system("veya ready — enter a coding task")
        chat.add_system("Ctrl+P = cycle persona | Ctrl+N = new session | Ctrl+Q = quit")

    # ── Input submit ──────────────────────────────────────────────────

    def on_veya_input_submit(self, event: HicodeInput.Submit) -> None:
        """User submitted a command — kick off coordinator via worker."""
        text = event.text.strip()
        if not text:
            return
        if self._task_running:
            self.notify("Task already running — press Escape to cancel", severity="warning")
            return
        self._run_command(text)

    @work(exclusive=True, exit_on_error=False)
    async def _run_command(self, text: str) -> None:
        """Worker: calls coordinator.handle() and routes on_step events to widgets."""
        from server.coordinator import coordinator
        from tui.stream import StreamAdapter

        self._task_running = True
        chat = self.query_one("#chat-log", ChatLog)
        squads = self.query_one("#squads-panel", SquadsPanel)
        status_bar = self.query_one("#status-bar", StatusBar)

        # Clear squads from previous request
        squads.clear_squads()
        chat.add_user(text)

        adapter = StreamAdapter(self)
        on_step = adapter.make_on_step()

        try:
            result = await coordinator.handle(
                {"text": text, "persona": self._persona},
                session_id=self._session_id,
                on_step=on_step,
            )
        except Exception as exc:
            chat.add_error(f"Error: {exc}")
            self._task_running = False
            return

        # Update session and cost from result
        new_sid = result.get("session_id")
        if new_sid:
            self._session_id = new_sid
            status_bar.update_session(new_sid)

        self._total_cost = result.get("cost_usd", self._total_cost)
        status_bar.update_cost(self._total_cost)

        # If result contains a diff (from execute squad), show it
        for squad in result.get("squads", []):
            output = squad.get("output") or ""
            if isinstance(output, str) and output.startswith("---"):
                diff_panel = self.query_one("#diff-panel", DiffViewer)
                diff_panel.show_diff(output)

        # Show overall status
        final_status = result.get("status", "unknown")
        chat.add_system(
            f"{'✓' if final_status == 'success' else '✗'} done "
            f"| ${self._total_cost:.5f} | session:{(self._session_id or '')[:8]}"
        )
        self._task_running = False

    # ── Key actions ───────────────────────────────────────────────────

    def action_cycle_persona(self) -> None:
        self._persona_idx = (self._persona_idx + 1) % len(_PERSONAS)
        self._persona = _PERSONAS[self._persona_idx]
        self.query_one("#input-area", HicodeInput).update_persona(self._persona)
        self.query_one("#status-bar", StatusBar).update_persona(self._persona)
        self.notify(f"Persona → {self._persona}", timeout=2)

    def action_new_session(self) -> None:
        self._session_id = None
        self._total_cost = 0.0
        chat = self.query_one("#chat-log", ChatLog)
        chat.clear()
        chat.add_system("New session started")
        self.query_one("#squads-panel", SquadsPanel).clear_squads()
        self.query_one("#diff-panel", DiffViewer).hide()
        self.query_one("#status-bar", StatusBar).update_session("")
        self.query_one("#status-bar", StatusBar).update_cost(0.0)

    def action_toggle_squads(self) -> None:
        panel = self.query_one("#squads-panel", SquadsPanel)
        panel.display = not panel.display

    def action_cancel_task(self) -> None:
        """Ctrl+C / Escape — cancel the running worker (CancelledError chain)."""
        if self._task_running:
            self.workers.cancel_group(self, "default")
            self._task_running = False
            self.query_one("#chat-log", ChatLog).add_system("⊘ task cancelled")
        else:
            # Propagate to default quit-on-escape if desired
            pass

    def action_quit(self) -> None:
        self.exit()


def run_tui() -> None:
    """Entry point for TUI mode."""
    from config.loader import load_config
    from server.assembly import Infra

    Infra.init(load_config())
    HicodeApp().run()
