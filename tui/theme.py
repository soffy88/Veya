"""hicode TUI theme — TCSS and color constants."""

TCSS = """
/* ── Global ─────────────────────────────────────── */
Screen {
    background: $surface;
}

/* ── Main layout ─────────────────────────────────── */
#main-pane {
    layout: horizontal;
    height: 1fr;
}

/* ── Chat panel (left 2/3) ───────────────────────── */
#chat-panel {
    width: 2fr;
    border: round $primary;
    padding: 0 1;
}

#chat-log {
    height: 1fr;
    scrollbar-gutter: stable;
}

/* ── Squads panel (right 1/3) ────────────────────── */
#squads-panel {
    width: 1fr;
    border: round $accent;
    padding: 0 1;
    overflow-y: auto;
}

#squads-title {
    text-style: bold;
    color: $accent;
    padding: 0 0 1 0;
}

.squad-block {
    border: solid $surface-lighten-1;
    padding: 0 1;
    margin-bottom: 1;
}

.squad-running {
    border: solid $warning;
}

.squad-success {
    border: solid $success;
}

.squad-failed {
    border: solid $error;
}

/* ── Diff panel ──────────────────────────────────── */
#diff-panel {
    height: 0;
    border: round $warning;
    padding: 0 1;
    display: none;
}

#diff-panel.visible {
    height: 8;
    display: block;
}

/* ── Input area ──────────────────────────────────── */
#input-area {
    height: 3;
    border: round $primary-lighten-2;
    padding: 0 1;
}

#prompt-input {
    height: 1fr;
    border: none;
}

/* ── Status bar ──────────────────────────────────── */
#status-bar {
    height: 1;
    background: $primary-darken-2;
    color: $text;
    padding: 0 1;
    layout: horizontal;
}

.status-item {
    width: auto;
    margin-right: 2;
    color: $text-muted;
}

.status-item-highlight {
    color: $accent;
}
"""

# Rich markup colors for chat messages
COLORS = {
    "user": "bold cyan",
    "assistant": "white",
    "tool_call": "bold yellow",
    "tool_result": "dim",
    "system": "dim italic",
    "error": "bold red",
    "success": "bold green",
    "cost": "bold magenta",
}
