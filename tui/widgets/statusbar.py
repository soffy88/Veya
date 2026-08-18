"""tui/widgets/statusbar.py — Status bar: persona / tool progress / stats / context / session / provider."""

from __future__ import annotations

import os
import time
from contextlib import suppress

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

_BAR_WIDTH = 10
_HISTORY_CAP = 5


def _render_bar(frac: float, width: int = _BAR_WIDTH) -> str:
    frac = max(0.0, min(frac, 1.0))
    filled = int(frac * width)
    return "[" + "■" * filled + "□" * (width - filled) + "]"


def _format_tok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


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
    .sb-progress { color: cyan; }
    .sb-context-warn { color: yellow; }
    .sb-session { color: $accent; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._persona = "build"
        self._cost = 0.0
        self._session_id = ""
        self._provider = os.environ.get("VEYA_LLM_PROVIDER", "dashscope")
        self._elapsed_start: float | None = None
        self._input_tokens = 0
        self._output_tokens = 0
        self._tool_current: str | None = None
        self._tool_start: float | None = None
        self._tool_history: dict[str, list[float]] = {}
        self._context_pct = 0.0
        self._context_compacting = False

    def compose(self) -> ComposeResult:
        yield Static(self._render_persona(), id="sb-persona", markup=True, classes="sb-item")
        yield Static(
            self._render_progress(), id="sb-progress", markup=True, classes="sb-item sb-progress"
        )
        yield Static(self._render_stats(), id="sb-stats", markup=True, classes="sb-item")
        yield Static(self._render_context(), id="sb-context", markup=True, classes="sb-item")
        yield Static(
            self._render_session(), id="sb-session", markup=True, classes="sb-item sb-session"
        )
        yield Static(f"provider:{self._provider}", id="sb-provider", markup=True, classes="sb-item")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        with suppress(Exception):
            self.query_one("#sb-progress", Static).update(self._render_progress())
        with suppress(Exception):
            self.query_one("#sb-stats", Static).update(self._render_stats())

    # ── render ────────────────────────────────────────────────────────

    def _render_persona(self) -> str:
        return f"[bold]persona:[/bold]{self._persona}"

    def _render_progress(self) -> str:
        if not self._tool_current or self._tool_start is None:
            return ""
        elapsed = time.monotonic() - self._tool_start
        hist = self._tool_history.get(self._tool_current) or []
        if hist:
            eta = sum(hist) / len(hist)
            frac = elapsed / eta if eta > 0 else 0.0
            return f"{_render_bar(frac)} {self._tool_current} {elapsed:.1f}s/~{eta:.1f}s"
        return f"⠋ {self._tool_current} {elapsed:.1f}s"

    def _render_stats(self) -> str:
        elapsed = int(time.monotonic() - self._elapsed_start) if self._elapsed_start else 0
        tok = f"{_format_tok(self._input_tokens)}/{_format_tok(self._output_tokens)}"
        return f"⏱{elapsed}s tok:{tok} cost:${self._cost:.5f}"

    def _render_context(self) -> str:
        if self._context_pct <= 0:
            return ""
        text = f"ctx:{self._context_pct:.0f}%"
        return f"[yellow]⚠ {text}[/yellow]" if self._context_compacting else text

    def _render_session(self) -> str:
        sid = self._session_id[:8] + "…" if len(self._session_id) > 8 else self._session_id
        return f"session:{sid}" if sid else "session:—"

    # ── update ────────────────────────────────────────────────────────

    def update_persona(self, persona: str) -> None:
        self._persona = persona
        with suppress(Exception):
            self.query_one("#sb-persona", Static).update(self._render_persona())

    def update_cost(self, cost: float) -> None:
        self._cost = cost
        with suppress(Exception):
            self.query_one("#sb-stats", Static).update(self._render_stats())

    def update_session(self, session_id: str) -> None:
        self._session_id = session_id
        with suppress(Exception):
            self.query_one("#sb-session", Static).update(self._render_session())

    def start_session(self) -> None:
        """开始计时当前请求的累计耗时 (working-activity 耗时统计的内化)。"""
        self._elapsed_start = time.monotonic()
        with suppress(Exception):
            self.query_one("#sb-stats", Static).update(self._render_stats())

    def update_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        with suppress(Exception):
            self.query_one("#sb-stats", Static).update(self._render_stats())

    def start_tool(self, tool_name: str) -> None:
        """工具调用开始: 记录起点, 供进度条按同名工具历史均值估 ETA
        (working-activity 进度条+ETA 的内化)。"""
        self._tool_current = tool_name
        self._tool_start = time.monotonic()
        with suppress(Exception):
            self.query_one("#sb-progress", Static).update(self._render_progress())

    def finish_tool(self, tool_name: str, elapsed_ms: int) -> None:
        hist = self._tool_history.setdefault(tool_name, [])
        hist.append(elapsed_ms / 1000)
        del hist[:-_HISTORY_CAP]
        if self._tool_current == tool_name:
            self._tool_current = None
            self._tool_start = None
        with suppress(Exception):
            self.query_one("#sb-progress", Static).update(self._render_progress())

    def update_context(self, usage_percent: float, compacting: bool) -> None:
        """上下文用量/压缩预警 (working-activity 的内化)。"""
        self._context_pct = usage_percent
        self._context_compacting = compacting
        with suppress(Exception):
            self.query_one("#sb-context", Static).update(self._render_context())

    def reset(self) -> None:
        """新会话: 清掉上一轮的耗时/token/进度/上下文状态, persona/provider 不变。"""
        self._cost = 0.0
        self._session_id = ""
        self._elapsed_start = None
        self._input_tokens = 0
        self._output_tokens = 0
        self._tool_current = None
        self._tool_start = None
        self._tool_history.clear()
        self._context_pct = 0.0
        self._context_compacting = False
        with suppress(Exception):
            self.query_one("#sb-stats", Static).update(self._render_stats())
        with suppress(Exception):
            self.query_one("#sb-session", Static).update(self._render_session())
        with suppress(Exception):
            self.query_one("#sb-progress", Static).update(self._render_progress())
        with suppress(Exception):
            self.query_one("#sb-context", Static).update(self._render_context())
