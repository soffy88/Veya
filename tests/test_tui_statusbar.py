"""tui/widgets/statusbar.py — 工具进度条+ETA / token·耗时·费用统计 / 上下文用量预警
(working-activity 内化)。"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from tui.widgets.statusbar import StatusBar, _format_tok, _render_bar


def test_render_bar_bounds():
    assert _render_bar(0.0) == "[□□□□□□□□□□]"
    assert _render_bar(1.0) == "[■■■■■■■■■■]"
    assert _render_bar(1.5) == "[■■■■■■■■■■]"  # 超过 100% 不越界


def test_format_tok():
    assert _format_tok(340) == "340"
    assert _format_tok(1200) == "1.2k"


class _StatusBarApp(App):
    def compose(self) -> ComposeResult:
        yield StatusBar(id="status-bar")


@pytest.mark.asyncio
async def test_start_tool_shows_indefinite_progress_without_history():
    app = _StatusBarApp()
    async with app.run_test():
        sb = app.query_one(StatusBar)
        sb.start_tool("read_file")
        rendered = sb._render_progress()
        assert "read_file" in rendered
        assert "~" not in rendered  # 无历史样本 → 不编造 ETA


@pytest.mark.asyncio
async def test_finish_tool_then_restart_shows_eta_from_history():
    app = _StatusBarApp()
    async with app.run_test():
        sb = app.query_one(StatusBar)
        sb.start_tool("read_file")
        sb.finish_tool("read_file", 500)
        assert sb._tool_current is None  # 完成后清空当前态

        sb.start_tool("read_file")
        rendered = sb._render_progress()
        assert "~0.5s" in rendered


@pytest.mark.asyncio
async def test_update_context_compacting_warns():
    app = _StatusBarApp()
    async with app.run_test():
        sb = app.query_one(StatusBar)
        sb.update_context(85.0, True)
        rendered = sb._render_context()
        assert "85%" in rendered
        assert "⚠" in rendered

        sb.update_context(30.0, False)
        rendered = sb._render_context()
        assert "⚠" not in rendered


@pytest.mark.asyncio
async def test_stats_reflect_tokens_and_cost():
    app = _StatusBarApp()
    async with app.run_test():
        sb = app.query_one(StatusBar)
        sb.update_tokens(1200, 340)
        sb.update_cost(0.00512)
        rendered = sb._render_stats()
        assert "1.2k/340" in rendered
        assert "$0.00512" in rendered


@pytest.mark.asyncio
async def test_reset_clears_progress_context_and_stats():
    app = _StatusBarApp()
    async with app.run_test():
        sb = app.query_one(StatusBar)
        sb.start_tool("read_file")
        sb.update_context(90.0, True)
        sb.update_tokens(500, 200)
        sb.update_cost(0.01)

        sb.reset()
        assert sb._render_progress() == ""
        assert sb._render_context() == ""
        assert "0/0" in sb._render_stats()
        assert "$0.00000" in sb._render_stats()
