"""tui/widgets/squads.py — Squad status panel (veya 招牌多分队可视化)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

ROLE_ICONS = {
    "research": "🔍",
    "plan": "📋",
    "execute": "⚡",
    "build": "🔨",
}

STATUS_COLORS = {
    "running": "yellow",
    "success": "green",
    "failed": "red",
    "unknown": "dim",
}


class SquadBlock(Widget):
    """Single squad status block: role / current action / cost."""

    DEFAULT_CSS = """
    SquadBlock {
        height: auto;
        border: solid $surface-lighten-2;
        padding: 0 1;
        margin-bottom: 1;
    }
    SquadBlock.running { border: solid yellow; }
    SquadBlock.success { border: solid green; }
    SquadBlock.failed  { border: solid red; }
    """

    status: reactive[str] = reactive("running")
    current_action: reactive[str] = reactive("starting…")
    cost_usd: reactive[float] = reactive(0.0)

    def __init__(self, squad_id: str, role: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.squad_id = squad_id
        self.role = role

    def compose(self) -> ComposeResult:
        icon = ROLE_ICONS.get(self.role, "●")
        yield Static(
            f"{icon} [{self.role}] {self.squad_id}",
            id=f"squad-title-{self.squad_id}",
            markup=True,
        )
        yield Static("", id=f"squad-action-{self.squad_id}", markup=True)
        yield Static("", id=f"squad-cost-{self.squad_id}", markup=True)

    def watch_status(self, status: str) -> None:
        self.remove_class("running", "success", "failed")
        if status in ("running", "success", "failed"):
            self.add_class(status)

    def watch_current_action(self, action: str) -> None:
        try:
            label = self.query_one(f"#squad-action-{self.squad_id}", Static)
            color = STATUS_COLORS.get(self.status, "dim")
            label.update(f"[{color}]{action}[/]")
        except Exception:
            pass

    def watch_cost_usd(self, cost: float) -> None:
        try:
            label = self.query_one(f"#squad-cost-{self.squad_id}", Static)
            label.update(f"[magenta]${cost:.5f}[/]")
        except Exception:
            pass

    def set_action(self, action: str) -> None:
        self.current_action = action

    def complete(self, status: str, cost: float) -> None:
        self.status = status
        self.cost_usd = cost
        sym = "✓" if status == "success" else "✗"
        self.current_action = f"{sym} {status}"


class SquadsPanel(VerticalScroll):
    """Panel showing all active squads for the current request."""

    DEFAULT_CSS = """
    SquadsPanel {
        width: 1fr;
        border: round $accent;
        padding: 0 1;
    }
    #squads-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("🚀 Squads", id="squads-title", markup=True)

    def clear_squads(self) -> None:
        """Remove all squad blocks (call before a new request)."""
        for block in self.query(SquadBlock):
            block.remove()

    def add_squad(self, squad_id: str, role: str) -> None:
        block_id = f"squad-{squad_id}"
        if not self.query(f"#{block_id}"):
            block = SquadBlock(squad_id, role, id=block_id)
            self.mount(block)

    def update_squad(self, squad_id: str, action: str) -> None:
        try:
            block = self.query_one(f"#squad-{squad_id}", SquadBlock)
            block.set_action(action)
        except Exception:
            pass

    def complete_squad(self, squad_id: str, status: str, cost: float = 0.0) -> None:
        try:
            block = self.query_one(f"#squad-{squad_id}", SquadBlock)
            block.complete(status, cost)
        except Exception:
            pass
