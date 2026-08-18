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
  │ StatusBar: persona|tool progress+ETA|⏱tok$|ctx%|session|provider │
  └─────────────────────────────────────────────────┘

Architecture rule: TUI never calls oprim/omodul/engine directly.
All logic flows through master_coordinator.chat_stream — same brain as Web.
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

_MODES = ["agent", "plan"]


class HicodeApp(App):
    """veya TUI — calls master_coordinator.chat_stream (same brain as Web)."""

    TITLE = "veya"
    SUB_TITLE = "AI coding agent"
    CSS = TCSS

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+x", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+p", "toggle_mode", "Plan/Agent", show=True),
        Binding("ctrl+n", "new_session", "New session", show=True),
        Binding("f1", "toggle_squads", "Squads", show=True),
        Binding("escape", "cancel_task", "Cancel", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mode = "agent"
        self._session_id: str | None = None
        self._total_cost: float = 0.0
        self._task_running: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-pane"):
            with Vertical(id="chat-panel"):
                yield ChatLog(id="chat-log")
            yield SquadsPanel(id="squads-panel")
        yield DiffViewer(id="diff-panel")
        yield HicodeInput(persona=self._mode, id="input-area")
        yield StatusBar(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        chat.add_system("veya ready — enter a coding task")
        chat.add_system("Ctrl+P = plan/agent | Ctrl+N = new session | Ctrl+Q = quit")

    # ── Input submit ──────────────────────────────────────────────────

    def on_hicode_input_submit(self, event: HicodeInput.Submit) -> None:
        """User submitted a command — kick off coordinator via worker."""
        text = event.text.strip()
        display_text = event.display_text.strip() or text
        if not text:
            return
        if self._task_running:
            self.notify("Task already running — press Escape to cancel", severity="warning")
            return
        self._run_command(text, display_text)

    @work(exclusive=True, exit_on_error=False)
    async def _run_command(self, text: str, display_text: str | None = None) -> None:
        """Worker: calls master_coordinator.chat_stream and routes on_step events."""
        from server.coordinator_master import master_coordinator
        from tui.stream import StreamAdapter

        self._task_running = True
        chat = self.query_one("#chat-log", ChatLog)
        squads = self.query_one("#squads-panel", SquadsPanel)
        status_bar = self.query_one("#status-bar", StatusBar)

        # Clear squads from previous request
        squads.clear_squads()
        chat.add_user(display_text or text)  # 长文粘贴用占位符回显, 完整内容仍发给后端
        status_bar.start_session()

        adapter = StreamAdapter(self)
        on_step = adapter.make_on_step()

        try:
            result = await master_coordinator.chat_stream(
                text,
                session_id=self._session_id,
                on_step=on_step,
                mode=self._mode,
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

        answer = result.get("final_answer") or ""
        if answer:
            chat.add_assistant(answer)

        # Show overall status
        final_status = result.get("status", "unknown")
        chat.add_system(
            f"{'✓' if final_status == 'success' else '✗'} done "
            f"| ${self._total_cost:.5f} | session:{(self._session_id or '')[:8]}"
        )
        self._task_running = False

    # ── Key actions ───────────────────────────────────────────────────

    def action_toggle_mode(self) -> None:
        self._mode = "plan" if self._mode == "agent" else "agent"
        self.query_one("#input-area", HicodeInput).update_persona(self._mode)
        self.query_one("#status-bar", StatusBar).update_persona(self._mode)
        self.notify(f"Mode → {self._mode}", timeout=2)

    def action_new_session(self) -> None:
        self._session_id = None
        self._total_cost = 0.0
        chat = self.query_one("#chat-log", ChatLog)
        chat.clear()
        chat.add_system("New session started")
        self.query_one("#squads-panel", SquadsPanel).clear_squads()
        self.query_one("#diff-panel", DiffViewer).hide()
        self.query_one("#status-bar", StatusBar).reset()

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
