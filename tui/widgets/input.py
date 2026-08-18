"""tui/widgets/input.py — Input widget with submit / persona switch support."""

from __future__ import annotations

from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static

_PASTE_LINE_THRESHOLD = 3
_PASTE_CHAR_THRESHOLD = 400


class _PasteAwareInput(Input):
    """单行 Input 的默认 `_on_paste` 只取粘贴内容的第一行, 长文本/多行粘贴会静默丢失
    剩余部分。这里改成整段暂存 + 插入占位符 `[Pasted text #N +M lines]`, 提交时再
    展开成完整文本发给后端, 展示层保留占位符 (对齐 Claude Code 长文粘贴收缩体验)。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pastes: dict[str, str] = {}
        self._paste_seq = 0

    def _on_paste(self, event: events.Paste) -> None:
        text = event.text or ""
        lines = text.splitlines()
        if len(lines) <= _PASTE_LINE_THRESHOLD and len(text) <= _PASTE_CHAR_THRESHOLD:
            super()._on_paste(event)
            return
        self._paste_seq += 1
        placeholder = f"[Pasted text #{self._paste_seq} +{len(lines)} lines]"
        self.pastes[placeholder] = text
        selection = self.selection
        if selection.is_empty:
            self.insert_text_at_cursor(placeholder)
        else:
            self.replace(placeholder, *selection)
        event.stop()

    def expand_pastes(self, text: str) -> str:
        """占位符 → 完整粘贴文本 (提交时调用, 发给后端的是完整内容)。"""
        for placeholder, full in self.pastes.items():
            text = text.replace(placeholder, full)
        return text

    def reset_pastes(self) -> None:
        self.pastes.clear()
        self._paste_seq = 0


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
        """Fired when the user submits a command.

        text: 完整内容(占位符已展开), 发给后端。
        display_text: 占位符原样保留的版本, 用于聊天记录回显(长文粘贴收缩显示)。
        """

        def __init__(self, text: str, display_text: str | None = None) -> None:
            super().__init__()
            self.text = text
            self.display_text = display_text if display_text is not None else text

    def __init__(self, persona: str = "build", **kwargs) -> None:
        super().__init__(**kwargs)
        self._persona = persona

    def compose(self) -> ComposeResult:
        yield Static(f"[{self._persona}]", id="persona-badge", markup=True)
        yield _PasteAwareInput(placeholder="Enter a coding task…", id="prompt-input")

    def update_persona(self, persona: str) -> None:
        self._persona = persona
        try:
            badge = self.query_one("#persona-badge", Static)
            badge.update(f"[{persona}]")
        except Exception:
            pass

    def action_clear_input(self) -> None:
        try:
            inp = self.query_one("#prompt-input", _PasteAwareInput)
            inp.value = ""
            inp.reset_pastes()
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        display_text = event.value.strip()
        if display_text:
            inp = event.input
            full_text = (
                inp.expand_pastes(display_text)
                if isinstance(inp, _PasteAwareInput)
                else display_text
            )
            self.post_message(self.Submit(full_text, display_text))
            event.input.value = ""
            if isinstance(inp, _PasteAwareInput):
                inp.reset_pastes()
