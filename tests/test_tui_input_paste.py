"""tui/widgets/input.py — 长文/多行粘贴自动收缩为占位符, 提交时展开完整内容
(working-activity 系列调研后对齐 Claude Code 粘贴体验)。"""

from __future__ import annotations

import pytest
from textual import events
from textual.app import App, ComposeResult

from tui.widgets.input import HicodeInput, _PasteAwareInput


class _InputApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.captured: list[HicodeInput.Submit] = []

    def compose(self) -> ComposeResult:
        yield HicodeInput(id="hicode-input")

    def on_hicode_input_submit(self, event: HicodeInput.Submit) -> None:
        self.captured.append(event)


@pytest.mark.asyncio
async def test_short_paste_passes_through_unmodified():
    app = _InputApp()
    async with app.run_test():
        inp = app.query_one("#prompt-input", _PasteAwareInput)
        inp.focus()
        inp._on_paste(events.Paste(text="hello world"))
        assert inp.value == "hello world"
        assert inp.pastes == {}


@pytest.mark.asyncio
async def test_long_paste_collapses_to_placeholder():
    app = _InputApp()
    async with app.run_test():
        inp = app.query_one("#prompt-input", _PasteAwareInput)
        inp.focus()
        long_text = "\n".join(f"line {i}" for i in range(50))
        inp._on_paste(events.Paste(text=long_text))
        assert inp.value == "[Pasted text #1 +50 lines]"
        assert inp.pastes["[Pasted text #1 +50 lines]"] == long_text


@pytest.mark.asyncio
async def test_expand_pastes_recovers_full_text():
    app = _InputApp()
    async with app.run_test():
        inp = app.query_one("#prompt-input", _PasteAwareInput)
        inp.focus()
        long_text = "def f():\n    pass\n" * 20
        inp._on_paste(events.Paste(text=long_text))
        placeholder = inp.value
        inp.insert_text_at_cursor(" explain this")
        expanded = inp.expand_pastes(inp.value)
        assert expanded == f"{long_text} explain this"
        assert placeholder not in expanded


@pytest.mark.asyncio
async def test_submit_sends_full_text_but_displays_placeholder():
    app = _InputApp()
    async with app.run_test() as pilot:
        inp = app.query_one("#prompt-input", _PasteAwareInput)
        inp.focus()

        long_text = "\n".join(f"log line {i}" for i in range(10))
        inp._on_paste(events.Paste(text=long_text))
        assert inp.value == "[Pasted text #1 +10 lines]"

        await inp.action_submit()
        await pilot.pause()

        assert len(app.captured) == 1
        submit = app.captured[0]
        assert submit.display_text == "[Pasted text #1 +10 lines]"
        assert submit.text == long_text
        assert inp.value == ""  # 提交后清空
        assert inp.pastes == {}  # reset after submit


@pytest.mark.asyncio
async def test_clear_input_resets_pastes():
    app = _InputApp()
    async with app.run_test():
        widget = app.query_one("#hicode-input", HicodeInput)
        inp = app.query_one("#prompt-input", _PasteAwareInput)
        inp.focus()
        inp._on_paste(events.Paste(text="\n".join(str(i) for i in range(10))))
        assert inp.pastes

        widget.action_clear_input()
        assert inp.value == ""
        assert inp.pastes == {}
