"""
layer4.streaming — Event Pipe & Renderers
layer4.auth      — Provider Login UI
layer4.mode      — Mode Controller
"""
from __future__ import annotations

import json
import sys
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, AsyncIterator
from layer4.tools import ToolRegistry, ToolAdapter


# ===========================================================================
# E. Streaming & Event Pipe
# ===========================================================================

@dataclass
class HicodeEvent:
    """统一的内部事件结构。"""
    event: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    session_id: str = ""


class EventBus:
    """内部事件总线：聚合 on_step 回调，支持多订阅者。"""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[HicodeEvent], None]] = []
        self._async_subscribers: list[Callable[[HicodeEvent], Any]] = []

    def subscribe(self, handler: Callable[[HicodeEvent], None]) -> None:
        self._subscribers.append(handler)

    def subscribe_async(self, handler: Callable[[HicodeEvent], Any]) -> None:
        self._async_subscribers.append(handler)  # pragma: no cover

    def emit(self, event: str, data: dict | None = None, session_id: str = "") -> None:
        e = HicodeEvent(event=event, data=data or {}, session_id=session_id)
        for sub in self._subscribers:
            try:
                sub(e)
            except Exception:
                pass

    async def emit_async(self, event: str, data: dict | None = None) -> None:
        self.emit(event, data)
        e = HicodeEvent(event=event, data=data or {})
        for sub in self._async_subscribers:
            try:  # pragma: no cover
                await sub(e)  # pragma: no cover
            except Exception:  # pragma: no cover
                pass  # pragma: no cover

    def as_on_step(self) -> Callable[[dict], None]:
        """返回兼容 omodul on_step 签名的回调。"""
        def on_step(event_dict: dict) -> None:
            self.emit(event_dict.get("event", "unknown"), event_dict)
        return on_step


class StreamRenderer:
    """token-by-token 渲染（llm_stream → 终端）。"""

    def __init__(self, *, prefix: str = "", file=None) -> None:
        self._prefix = prefix  # pragma: no cover
        self._file = file or sys.stdout  # pragma: no cover
        self._buffer = ""  # pragma: no cover

    async def render(self, stream: AsyncIterator) -> str:
        """消费 llm_stream 的 AsyncIterator[StreamDelta]，渲染到终端。"""
        full_text = ""  # pragma: no cover
        try:  # pragma: no cover
            if self._prefix:  # pragma: no cover
                print(self._prefix, end="", file=self._file, flush=True)  # pragma: no cover
            async for delta in stream:  # pragma: no cover
                if hasattr(delta, "type"):  # pragma: no cover
                    if delta.type == "text" and delta.text:  # pragma: no cover
                        print(delta.text, end="", file=self._file, flush=True)  # pragma: no cover
                        full_text += delta.text  # pragma: no cover
                    elif delta.type == "stop":  # pragma: no cover
                        print("", file=self._file)  # newline at end  # pragma: no cover
                elif isinstance(delta, dict):  # pragma: no cover
                    text = delta.get("text", "")  # pragma: no cover
                    if text:  # pragma: no cover
                        print(text, end="", file=self._file, flush=True)  # pragma: no cover
                        full_text += text  # pragma: no cover
        except Exception:  # pragma: no cover
            pass  # pragma: no cover
        return full_text  # pragma: no cover


class SSEEmitter:
    """Server-Sent Events 输出（headless/Agent SDK 模式）。

    格式：data: <json>\n\n
    """

    def __init__(self, file=None) -> None:
        self._file = file or sys.stdout

    def emit(self, event: str, data: dict) -> None:
        payload = json.dumps({"event": event, **data}, ensure_ascii=False)
        print(f"data: {payload}\n", file=self._file, flush=True)

    def as_on_step(self) -> Callable[[dict], None]:
        def on_step(event_dict: dict) -> None:
            self.emit(event_dict.get("event", "step"), event_dict)
        return on_step


class StdoutRenderer:
    """-p/print 模式纯文本输出（无 ANSI，管道友好）。"""

    def __init__(self, file=None) -> None:
        self._file = file or sys.stdout

    def on_step(self, event_dict: dict) -> None:
        event = event_dict.get("event", "")
        if event == "tool_call":
            print(f"[tool] {event_dict.get('tool', '')}", file=self._file)
        elif event == "tool_result":
            preview = str(event_dict.get("result_preview", ""))[:100]  # pragma: no cover
            print(f"[result] {preview}", file=self._file)  # pragma: no cover
        elif event in ("session_done", "completed"):
            cost = event_dict.get("cost_usd", 0)
            print(f"[done] status={event_dict.get('status','')} cost=${cost:.4f}",
                  file=self._file)

    def print_result(self, text: str) -> None:
        print(text, file=self._file)  # pragma: no cover


class DiffRenderer:
    """generate_patch_preview oskill 输出着色显示。"""

    RESET = "\033[0m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"

    @classmethod
    def render(cls, diff: str, *, color: bool = True, file=None) -> None:
        out = file or sys.stdout
        if not color or not diff:
            print(diff, file=out)
            return
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                print(f"{cls.GREEN}{line}{cls.RESET}", file=out)
            elif line.startswith("-") and not line.startswith("---"):
                print(f"{cls.RED}{line}{cls.RESET}", file=out)
            elif line.startswith("@@"):
                print(f"{cls.CYAN}{line}{cls.RESET}", file=out)
            elif line.startswith("+++") or line.startswith("---"):
                print(f"{cls.BOLD}{line}{cls.RESET}", file=out)
            else:
                print(line, file=out)  # pragma: no cover


class TodoRenderer:
    """TodoItem 列表渲染（进度条样式）。"""

    STATUS_ICONS = {
        "pending": "○",
        "in_progress": "●",
        "done": "✅",
        "cancelled": "✗",
    }
    PRIORITY_COLORS = {
        "high": "\033[31m",
        "medium": "\033[33m",
        "low": "\033[32m",
    }
    RESET = "\033[0m"

    @classmethod
    def render(cls, todos: list, *, color: bool = True, file=None) -> str:
        if not todos:
            return "No todos."
        _out = file or sys.stdout  # used later if file provided
        lines = []
        done = sum(1 for t in todos
                   if (t.status if hasattr(t, "status") else t.get("status")) == "done")
        total = len(todos)
        pct = int(done / total * 100) if total else 0
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        header = f"Tasks [{bar}] {pct}% ({done}/{total})"
        lines.append(header)
        for t in todos:
            status = t.status if hasattr(t, "status") else t.get("status", "pending")
            priority = t.priority if hasattr(t, "priority") else t.get("priority", "medium")
            content = t.content if hasattr(t, "content") else t.get("content", "")
            icon = cls.STATUS_ICONS.get(status, "?")
            if color:
                pc = cls.PRIORITY_COLORS.get(priority, "")
                line = f"  {icon} {pc}{content}{cls.RESET}"
            else:
                line = f"  {icon} [{priority}] {content}"
            lines.append(line)
        result = "\n".join(lines)
        if file:
            print(result, file=file)  # pragma: no cover
        return result


class CostStatusline:
    """状态栏：实时 cost/tokens/模型显示。"""

    def __init__(self, *, model: str = "claude-sonnet-4-6", file=None) -> None:
        self.model = model
        self._file = file or sys.stderr
        self.total_cost = 0.0
        self.in_tokens = 0
        self.out_tokens = 0

    def update(self, cost_usd: float = 0.0, in_tok: int = 0, out_tok: int = 0) -> None:
        self.total_cost += cost_usd
        self.in_tokens += in_tok
        self.out_tokens += out_tok

    def render(self) -> str:
        return (f"[{self.model}] "
                f"in={self.in_tokens:,} out={self.out_tokens:,} "
                f"cost=${self.total_cost:.4f}")

    def print(self) -> None:
        print(f"\r{self.render()}", end="", file=self._file, flush=True)

    def on_step(self, event_dict: dict) -> None:
        cost = event_dict.get("cost_usd", 0.0)
        if isinstance(cost, (int, float)) and cost > 0:
            self.total_cost = cost  # 累计值
            self.print()


class ThinkingRenderer:
    """interleaved thinking 折叠显示。"""

    GRAY = "\033[90m"
    RESET = "\033[0m"

    @classmethod
    def render(cls, thinking: str, *, collapsed: bool = True,
               color: bool = True, file=None) -> None:
        out = file or sys.stdout
        if not thinking:
            return
        if collapsed:
            preview = thinking[:80].replace("\n", " ")
            if color:
                print(f"{cls.GRAY}💭 {preview}…{cls.RESET}", file=out)
            else:
                print(f"[thinking] {preview}…", file=out)  # pragma: no cover
        else:
            if color:  # pragma: no cover
                print(f"{cls.GRAY}💭 Thinking:\n{thinking}\n{cls.RESET}", file=out)  # pragma: no cover
            else:
                print(f"[thinking]\n{thinking}", file=out)  # pragma: no cover


# ===========================================================================
# I. Provider Login UI
# ===========================================================================

class CredentialStore:
    """安全存储 API key 到 env 文件 / keychain（调 obase.secrets）。"""

    ENV_FILE = Path.home() / ".hicode" / ".env"

    @classmethod
    def save(cls, key: str, value: str) -> None:
        """写入 ~/.hicode/.env（简单实现；生产版接 obase.secrets / keychain）。"""
        cls.ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        found = False
        if cls.ENV_FILE.exists():
            for line in cls.ENV_FILE.read_text().splitlines():
                if line.startswith(f"{key}="):
                    lines.append(f"{key}={value}")
                    found = True
                else:
                    lines.append(line)  # pragma: no cover
        if not found:
            lines.append(f"{key}={value}")
        cls.ENV_FILE.write_text("\n".join(lines) + "\n")

    @classmethod
    def load(cls, key: str) -> str | None:
        """读取，优先 env var，再读文件。"""
        val = os.environ.get(key)
        if val:
            return val
        if cls.ENV_FILE.exists():
            for line in cls.ENV_FILE.read_text().splitlines():
                if line.startswith(f"{key}="):
                    return line[len(key) + 1:]
        return None


class ApiKeyPrompt:
    """终端交互式录入 API key（可注入 mock）。"""

    def __init__(self, *, auto_value: str | None = None) -> None:
        self._auto = auto_value

    async def prompt(self, provider: str) -> str:
        if self._auto is not None:
            return self._auto
        try:  # pragma: no cover
            import getpass  # pragma: no cover
            key = getpass.getpass(f"Enter API key for {provider}: ")  # pragma: no cover
            return key.strip()  # pragma: no cover
        except (EOFError, KeyboardInterrupt):  # pragma: no cover
            return ""  # pragma: no cover

    async def setup(self, provider: str) -> bool:
        """交互式录入并保存。返回 True 成功。"""
        key_name = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "gemini": "GOOGLE_API_KEY",
        }.get(provider.lower(), f"{provider.upper()}_API_KEY")

        value = await self.prompt(provider)
        if not value:
            return False  # pragma: no cover
        CredentialStore.save(key_name, value)
        return True


class OAuthFlow:
    """Device-flow OAuth 登录（stub；生产版接 obase.auth）。"""

    async def start(self, provider: str) -> dict:
        """启动 device-flow，返回 {device_code, user_code, verification_uri}。"""
        try:  # pragma: no cover
            from obase import auth  # type: ignore  # pragma: no cover
            return await auth.device_flow_start(provider)  # pragma: no cover
        except ImportError:  # pragma: no cover
            return {  # pragma: no cover
                "error": "obase.auth not available",
                "verification_uri": f"https://{provider}.com/oauth/device",
                "user_code": "HICODE-XXXX",
            }

    async def poll(self, provider: str, device_code: str) -> str | None:
        """轮询获取 access token。返回 token 或 None（待续）。"""
        try:  # pragma: no cover
            from obase import auth  # type: ignore  # pragma: no cover
            return await auth.device_flow_poll(provider, device_code)  # pragma: no cover
        except ImportError:  # pragma: no cover
            return None  # pragma: no cover


# ===========================================================================
# J. Mode Controller
# ===========================================================================


class ModeController:
    """当前模式状态（build/plan/bypass）。"""

    def __init__(self, initial: str = "build") -> None:
        self.current = initial
        self._history: list[str] = [initial]

    def set(self, mode: str) -> None:
        if mode not in ("build", "plan", "bypass"):
            raise ValueError(f"Invalid mode: {mode}")
        self._history.append(mode)
        self.current = mode

    @property
    def is_build(self) -> bool:
        return self.current == "build"

    @property
    def is_plan(self) -> bool:
        return self.current == "plan"

    @property
    def is_bypass(self) -> bool:
        return self.current == "bypass"


class PlanToolSet:
    """Plan 模式只读工具集装配。"""

    @staticmethod
    def filter(registry: ToolRegistry) -> list[ToolAdapter]:
        return registry.readonly_only()


class BuildToolSet:
    """Build 模式全量工具集装配。"""

    @staticmethod
    def filter(registry: ToolRegistry) -> list[ToolAdapter]:
        return registry.all()


class ModeToggle:
    """模式切换时重新装配 agentic_loop 的工具集。"""

    def __init__(self, registry: ToolRegistry,
                 controller: ModeController) -> None:
        self.registry = registry
        self.controller = controller

    def active_tools(self) -> list[ToolAdapter]:
        if self.controller.is_plan:
            return PlanToolSet.filter(self.registry)
        return BuildToolSet.filter(self.registry)

    def switch(self, mode: str) -> list[ToolAdapter]:
        self.controller.set(mode)
        return self.active_tools()
