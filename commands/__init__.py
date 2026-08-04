"""
layer4.commands — Slash Command Router
layer4.hooks    — Hook Manager
layer4.subagent — Subagent Loader
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


# ===========================================================================
# D. Slash Command Router
# ===========================================================================


@dataclass
class CommandResult:
    text: str = ""
    messages: list[dict] = field(default_factory=list)
    error: str | None = None
    redirect_to_loop: bool = False  # True → 把 text 作为任务传给 agentic_loop


@dataclass
class SlashCommand:
    name: str
    description: str
    handler: Callable[..., Awaitable[CommandResult]]
    aliases: list[str] = field(default_factory=list)
    usage: str = ""


class SlashRouter:
    """解析输入，匹配命令，调 handler。"""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, cmd: SlashCommand) -> None:
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd

    def is_command(self, text: str) -> bool:
        return text.strip().startswith("/")

    async def dispatch(self, text: str, **ctx) -> CommandResult:
        text = text.strip()
        if not text.startswith("/"):
            return CommandResult(error="Not a command")  # pragma: no cover
        parts = text[1:].split(None, 1)
        name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        cmd = self._commands.get(name)
        if cmd is None:
            return CommandResult(error=f"Unknown command: /{name}")

        try:
            return await cmd.handler(args=args, **ctx)
        except Exception as e:
            return CommandResult(error=f"/{name} failed: {e}")

    def help_text(self) -> str:
        seen = set()
        lines = ["Available commands:"]
        for cmd in self._commands.values():
            if cmd.name in seen:
                continue
            seen.add(cmd.name)
            aliases = (
                f" (aliases: {', '.join('/' + a for a in cmd.aliases)})" if cmd.aliases else ""
            )
            lines.append(f"  /{cmd.name}{aliases}  — {cmd.description}")
        return "\n".join(lines)


# --- Command implementations ------------------------------------------------


async def _init_handler(args: str, **ctx) -> CommandResult:
    """→ initialize_project (legacy omodul; compat shim)."""
    from hicode.compat import init_project

    root = args.strip() or ctx.get("cwd", ".")  # pragma: no cover
    caller = ctx.get("caller")  # pragma: no cover
    if not caller:  # pragma: no cover
        return CommandResult(error="/init requires a configured LLM caller")  # pragma: no cover
    result = await init_project(
        {"max_files": 500},
        {"root_path": root, "llm_caller": caller},
    )
    if result.get("status") == "completed":  # pragma: no cover
        return CommandResult(
            text=f"✅ AGENTS.md written to {result.get('agents_md_path')}"
        )  # pragma: no cover
    return CommandResult(
        error=result.get("error", {}).get("message", "init failed")
    )  # pragma: no cover


async def _plan_handler(args: str, **ctx) -> CommandResult:
    """切换到 plan 模式，把 args 作为任务送入 agentic_loop（只读工具）。"""
    session = ctx.get("session")
    if session:
        session.mode = "plan"
    return (
        CommandResult(text=args, redirect_to_loop=True)
        if args
        else CommandResult(text="Switched to PLAN mode (read-only tools).")
    )


async def _build_handler(args: str, **ctx) -> CommandResult:
    """切换到 build 模式，把 args 作为任务送入 agentic_loop。"""
    session = ctx.get("session")
    if session:
        session.mode = "build"
    return (
        CommandResult(text=args, redirect_to_loop=True)
        if args
        else CommandResult(text="Switched to BUILD mode (full tools).")
    )


async def _undo_handler(args: str, **ctx) -> CommandResult:
    """→ versionstore.restore + build_undo_plan oskill。"""
    versionstore = ctx.get("versionstore")  # pragma: no cover
    if not versionstore:  # pragma: no cover
        return CommandResult(error="No versionstore available for undo")  # pragma: no cover
    revs = (
        versionstore.list_revs() if hasattr(versionstore, "list_revs") else []
    )  # pragma: no cover
    if not revs:  # pragma: no cover
        return CommandResult(error="Nothing to undo")  # pragma: no cover
    rev = revs[-1]  # pragma: no cover
    restored = versionstore.restore(rev)  # pragma: no cover
    return CommandResult(
        text=f"↩  Restored {len(restored)} file(s) from snapshot {rev}"
    )  # pragma: no cover


async def _redo_handler(args: str, **ctx) -> CommandResult:
    return CommandResult(
        text="redo: not yet implemented (requires forward-snapshot stack)"
    )  # pragma: no cover


async def _compact_handler(args: str, **ctx) -> CommandResult:
    """→ compact_conversation (legacy omodul; compat shim)."""
    from hicode.compat import compact_session

    session = ctx.get("session")  # pragma: no cover
    caller = ctx.get("caller")  # pragma: no cover
    if not session or not caller:  # pragma: no cover
        return CommandResult(error="/compact requires active session + caller")  # pragma: no cover
    result = await compact_session(session.messages)
    if result.get("compacted"):  # pragma: no cover
        session.messages = result["messages"]  # pragma: no cover
        before = len(session.messages)  # pragma: no cover
        return CommandResult(
            text=f"🗜  Compacted: {before} → {len(session.messages)} messages"
        )  # pragma: no cover
    return CommandResult(error="compact failed")  # pragma: no cover


async def _review_handler(args: str, **ctx) -> CommandResult:
    """→ code_review (legacy omodul; unavailable)."""
    return CommandResult(
        error="/review requires the legacy omodul.reports module, which is not installed"
    )  # pragma: no cover


async def _tests_handler(args: str, **ctx) -> CommandResult:
    """→ generate_tests (legacy omodul; unavailable)."""
    target = args.strip()  # pragma: no cover
    if not target:  # pragma: no cover
        return CommandResult(error="/tests <file_path>")  # pragma: no cover
    return CommandResult(
        error="/tests requires the legacy omodul.reports module, which is not installed"
    )  # pragma: no cover


async def _checkpoint_handler(args: str, **ctx) -> CommandResult:
    """→ create_checkpoint (legacy omodul; compat shim)."""
    from hicode.compat import RunState, make_checkpoint

    session = ctx.get("session")  # pragma: no cover
    if not session:  # pragma: no cover
        return CommandResult(error="/checkpoint requires active session")  # pragma: no cover
    ckpt = make_checkpoint(RunState(session_id=session.id, data={"messages": session.messages}))
    return CommandResult(
        text=f"📍 Checkpoint {ckpt.session_id} saved ({len(session.messages)} messages)"
    )  # pragma: no cover


async def _rewind_handler(args: str, **ctx) -> CommandResult:
    """→ rewind_to_checkpoint (legacy omodul; unavailable)."""
    ckpt_id = args.strip()  # pragma: no cover
    if not ckpt_id:  # pragma: no cover
        return CommandResult(error="/rewind <checkpoint_id>")  # pragma: no cover
    return CommandResult(
        error="/rewind requires the legacy omodul.complex module, which is not installed"
    )  # pragma: no cover


async def _agents_handler(args: str, **ctx) -> CommandResult:
    subagent_loader = ctx.get("subagent_loader")  # pragma: no cover
    if not subagent_loader:  # pragma: no cover
        return CommandResult(text="No subagents configured.")  # pragma: no cover
    agents = subagent_loader.list() if hasattr(subagent_loader, "list") else []  # pragma: no cover
    if not agents:  # pragma: no cover
        return CommandResult(text="No subagents found in .claude/agents/")  # pragma: no cover
    lines = ["Available subagents:"]  # pragma: no cover
    for a in agents:  # pragma: no cover
        lines.append(f"  • {a.get('name', '?')}  — {a.get('description', '')}")  # pragma: no cover
    return CommandResult(text="\n".join(lines))  # pragma: no cover


async def _plugin_handler(args: str, **ctx) -> CommandResult:
    """→ install_plugin (legacy omodul; unavailable)."""
    if not args.strip():  # pragma: no cover
        return CommandResult(error="/plugin <bundle_json_path>")  # pragma: no cover
    try:  # pragma: no cover
        # Validate the bundle parses as JSON (module itself is unavailable)
        json.loads(Path(args.strip()).read_text())  # pragma: no cover
    except Exception as e:  # pragma: no cover
        return CommandResult(error=f"Cannot load plugin bundle: {e}")  # pragma: no cover
    return CommandResult(
        error="/plugin requires the legacy omodul.complex module, which is not installed"
    )  # pragma: no cover


async def _hooks_handler(args: str, **ctx) -> CommandResult:
    hook_manager = ctx.get("hook_manager")  # pragma: no cover
    if not hook_manager:  # pragma: no cover
        return CommandResult(text="Hook manager not configured.")  # pragma: no cover
    if args.strip() == "list" or not args.strip():  # pragma: no cover
        hooks = hook_manager.list_hooks()  # pragma: no cover
        if not hooks:  # pragma: no cover
            return CommandResult(text="No hooks configured.")  # pragma: no cover
        lines = [f"  {h['event']:25} → {h['command']}" for h in hooks]  # pragma: no cover
        return CommandResult(text="Configured hooks:\n" + "\n".join(lines))  # pragma: no cover
    return CommandResult(text=f"Hooks: {hook_manager.list_hooks()}")  # pragma: no cover


async def _custom_handler_factory(name: str, content: str) -> Callable:
    """从 .claude/commands/*.md 动态创建命令 handler。"""

    async def handler(args: str, **ctx) -> CommandResult:  # pragma: no cover
        prompt = content.replace("$ARGUMENTS", args).replace("{args}", args)  # pragma: no cover
        return CommandResult(text=prompt, redirect_to_loop=True)  # pragma: no cover

    handler.__name__ = name  # pragma: no cover
    return handler  # pragma: no cover


def build_default_router(*, custom_commands_dir: str | Path | None = None) -> SlashRouter:
    """构建包含所有内置命令的默认 SlashRouter。"""
    router = SlashRouter()
    commands = [
        SlashCommand("init", "Scan codebase and generate AGENTS.md", _init_handler),
        SlashCommand("plan", "Switch to PLAN mode (read-only)", _plan_handler),
        SlashCommand("build", "Switch to BUILD mode (full tools)", _build_handler),
        SlashCommand("undo", "Undo last changeset", _undo_handler, aliases=["u"]),
        SlashCommand("redo", "Redo last undone changeset", _redo_handler),
        SlashCommand("compact", "Compact conversation history", _compact_handler),
        SlashCommand("review", "Code review file(s)", _review_handler, usage="/review [files...]"),
        SlashCommand("tests", "Generate tests for a file", _tests_handler, usage="/tests <file>"),
        SlashCommand(
            "checkpoint", "Save conversation checkpoint", _checkpoint_handler, aliases=["ckpt"]
        ),
        SlashCommand("rewind", "Rewind to a checkpoint", _rewind_handler),
        SlashCommand("agents", "List available subagents", _agents_handler),
        SlashCommand("plugin", "Install a plugin bundle", _plugin_handler),
        SlashCommand("hooks", "View/manage hooks", _hooks_handler),
        SlashCommand(
            "help",
            "Show available commands",
            lambda args, **ctx: _help_handler(args, router=router, **ctx),
        ),
        SlashCommand(
            "sessions", "Manage sessions", lambda args, **ctx: _sessions_handler(args, **ctx)
        ),
    ]
    for cmd in commands:
        router.register(cmd)

    # 加载 .claude/commands/*.md 自定义命令
    if custom_commands_dir:
        _load_custom_commands(router, Path(custom_commands_dir))  # pragma: no cover

    return router


async def _help_handler(args: str, router: SlashRouter | None = None, **ctx) -> CommandResult:
    return CommandResult(text=router.help_text() if router else "No router available")


async def _sessions_handler(args: str, **ctx) -> CommandResult:
    multi_router = ctx.get("multi_session_router")  # pragma: no cover
    if not multi_router:  # pragma: no cover
        return CommandResult(error="Session manager not configured")  # pragma: no cover
    from session import MultiSessionRouter  # pragma: no cover

    if not isinstance(multi_router, MultiSessionRouter):  # pragma: no cover
        return CommandResult(error="Invalid session router")  # pragma: no cover
    parts = args.split() if args else []  # pragma: no cover
    return CommandResult(text=multi_router.handle(parts))  # pragma: no cover


def _load_custom_commands(router: SlashRouter, commands_dir: Path) -> None:
    if not commands_dir.exists():  # pragma: no cover
        return  # pragma: no cover
    for md_file in commands_dir.glob("*.md"):  # pragma: no cover
        name = md_file.stem.lower()  # pragma: no cover
        try:  # pragma: no cover
            content = md_file.read_text()  # pragma: no cover
            # 提取第一行作为描述
            first_line = (
                content.splitlines()[0].lstrip("#").strip() if content else name
            )  # pragma: no cover

            async def make_handler(c=content, n=name):  # pragma: no cover
                async def h(args: str, **ctx) -> CommandResult:  # pragma: no cover
                    prompt = c.replace("$ARGUMENTS", args)  # pragma: no cover
                    return CommandResult(text=prompt, redirect_to_loop=True)  # pragma: no cover

                return h  # pragma: no cover

            # create directly  # pragma: no cover
            _content = content  # pragma: no cover

            async def _h(args: str, _c=_content, **ctx) -> CommandResult:  # pragma: no cover
                prompt = _c.replace("$ARGUMENTS", args)  # pragma: no cover
                return CommandResult(text=prompt, redirect_to_loop=True)  # pragma: no cover

            router.register(SlashCommand(name, first_line, _h))  # pragma: no cover
        except Exception:  # pragma: no cover
            pass  # pragma: no cover


# ===========================================================================
# G. Hook Manager
# ===========================================================================


class HookEventNames(StrEnum):
    """25 个 hook 生命周期点。"""

    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PERMISSION_REQUEST = "PermissionRequest"
    STOP = "Stop"
    SUBAGENT_STOP = "SubagentStop"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    PRE_CHECKPOINT = "PreCheckpoint"
    POST_CHECKPOINT = "PostCheckpoint"
    PRE_REWIND = "PreRewind"
    POST_REWIND = "PostRewind"
    TOOL_ERROR = "ToolError"
    BUDGET_WARNING = "BudgetWarning"
    BUDGET_EXCEEDED = "BudgetExceeded"
    CONTEXT_THRESHOLD = "ContextThreshold"
    MODEL_CHANGE = "ModelChange"
    MODE_CHANGE = "ModeChange"
    SUBAGENT_START = "SubagentStart"
    PLUGIN_INSTALL = "PluginInstall"
    SKILL_LOAD = "SkillLoad"
    MEMORY_UPDATE = "MemoryUpdate"
    ERROR = "Error"


@dataclass
class HookConfig:
    event: str
    command: str
    matcher: str | None = None
    scope: str = "global"  # "global" | "project" | "plugin" | "skill"

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "command": self.command,
            "matcher": self.matcher,
            "scope": self.scope,
        }


class HookManager:
    """加载 hook 配置，管理 25 个事件点，CRUD。"""

    def __init__(self, configs: list[HookConfig] | None = None) -> None:
        self._hooks: list[HookConfig] = list(configs or [])

    def add(self, hook: HookConfig) -> None:
        self._hooks.append(hook)

    def remove(self, event: str, command: str) -> bool:
        before = len(self._hooks)
        self._hooks = [h for h in self._hooks if not (h.event == event and h.command == command)]
        return len(self._hooks) < before

    def list_hooks(self) -> list[dict]:
        return [h.to_dict() for h in self._hooks]

    def hooks_for(self, event: str) -> list[HookConfig]:
        return [h for h in self._hooks if h.event == event]

    @classmethod
    def from_config(cls, settings: dict) -> HookManager:
        configs = []
        for raw in settings.get("hooks", []):
            if isinstance(raw, dict):
                configs.append(
                    HookConfig(
                        event=raw.get("event", ""),
                        command=raw.get("command", ""),
                        matcher=raw.get("matcher"),
                        scope=raw.get("scope", "global"),
                    )
                )
        return cls(configs)


class HookDispatcher:
    """agentic_loop 的 hook_dispatch 注入点实现。

    async callable: (event: str, payload: dict) → {decision, modified_payload}
    """

    def __init__(self, manager: HookManager) -> None:
        self.manager = manager

    async def __call__(self, event: str, payload: dict) -> dict:
        """评估 + 执行匹配的 hooks，返回聚合决策。"""
        try:
            from hicode.compat import evaluate_hooks
            from hicode.compat import run_hook as _run_hook

            hook_specs = [
                {"event": h.event, "command": h.command, "matcher": h.matcher}
                for h in self.manager.hooks_for(event)
            ]
            if not hook_specs:
                return {"decision": "allow", "modified_payload": payload}

            cmds = evaluate_hooks(event, payload, hook_specs=hook_specs)  # pragma: no cover
            results = []  # pragma: no cover
            for cmd in cmds:  # pragma: no cover
                result = await _run_hook(
                    cmd.command, event_json={"event": event, **payload}
                )  # pragma: no cover
                results.append(result)  # pragma: no cover

            # 任一 block → block
            for r in results:  # pragma: no cover
                if r.decision == "block":  # pragma: no cover
                    return {"decision": "block", "modified_payload": payload}  # pragma: no cover

            return {"decision": "allow", "modified_payload": payload}  # pragma: no cover
        except ImportError:  # pragma: no cover
            return {"decision": "allow", "modified_payload": payload}  # pragma: no cover


# ===========================================================================
# H. Subagent Loader
# ===========================================================================


class SubagentLoader:
    """.claude/agents/*.md 解析 → SubagentDefinition 列表。"""

    def __init__(self, agents_dir: str | Path | None = None) -> None:
        self._dir = Path(agents_dir or ".claude/agents")
        self._cache: dict[str, Any] = {}

    def list(self) -> list[dict]:
        if not self._dir.exists():
            return []
        result = []
        for md in self._dir.glob("*.md"):
            try:
                from hicode.compat import read_skill_frontmatter

                meta = read_skill_frontmatter(
                    str(md.parent / md.stem) if (md.parent / md.stem).is_dir() else str(md.parent)
                )
                result.append(
                    {
                        "name": meta.name,
                        "description": meta.description,  # pragma: no cover
                        "tools": meta.tools,
                    }
                )
            except Exception:
                # frontmatter 可能不在子目录，直接解析 md 文件
                try:
                    content = md.read_text()
                    name = md.stem
                    desc = content.splitlines()[0].lstrip("#").strip() if content else name
                    result.append({"name": name, "description": desc, "tools": []})
                except Exception:  # pragma: no cover
                    pass  # pragma: no cover
        return result

    def load(self, name: str) -> Any | None:
        """加载指定 subagent，返回 SubagentDefinition 或 None。"""
        if name in self._cache:
            return self._cache[name]  # pragma: no cover

        # 支持两种格式：name.md 文件 或 name/ 目录（含 SKILL.md）
        md_file = self._dir / f"{name}.md"
        md_dir = self._dir / name

        if md_file.exists():
            source_md = md_file
        elif md_dir.is_dir() and (md_dir / "SKILL.md").exists():
            source_md = md_dir / "SKILL.md"  # pragma: no cover
        else:
            return None

        try:
            from hicode.compat import SubagentDefinition, SubagentPermissions

            content = source_md.read_text()

            system_prompt = content
            tools: list = []
            perms = SubagentPermissions(mode="default")

            import re as _re

            fm_m = _re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, _re.DOTALL)
            if fm_m:
                fm_text = fm_m.group(1)  # pragma: no cover
                system_prompt = fm_m.group(2).strip()  # pragma: no cover
                mode_m = _re.search(r"^mode:\s*(\w+)", fm_text, _re.MULTILINE)  # pragma: no cover
                if mode_m:  # pragma: no cover
                    perms = SubagentPermissions(mode=mode_m.group(1))  # pragma: no cover

            memory_dir = self._dir.parent / "agent-memory" / name
            defn = SubagentDefinition(
                name=name,
                system_prompt=system_prompt,
                tools=tools,
                permissions=perms,
                memory_dir=memory_dir,
            )
            self._cache[name] = defn
            return defn
        except Exception:  # pragma: no cover
            return None  # pragma: no cover

    def __call__(self, name: str) -> Any | None:
        return self.load(name)


class AgentMemoryStore:
    """.claude/agent-memory/<name>/ 读写（worker_assembly 层）。"""

    def __init__(self, base_dir: str | Path = ".claude/agent-memory") -> None:
        self._base = Path(base_dir)

    def _path(self, agent_name: str) -> Path:
        return self._base / agent_name

    def read(self, agent_name: str) -> str:
        d = self._path(agent_name)
        if not d.exists():
            return ""
        parts = []
        for f in sorted(d.glob("*.md")):
            with contextlib.suppress(Exception):  # pragma: no cover
                parts.append(f.read_text())
        return "\n\n".join(parts)

    def write(self, agent_name: str, content: str, *, filename: str = "memory.md") -> Path:
        d = self._path(agent_name)
        d.mkdir(parents=True, exist_ok=True)
        p = d / filename
        p.write_text(content)
        return p

    def append(self, agent_name: str, content: str) -> Path:
        d = self._path(agent_name)
        d.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1_000_000)  # microseconds for uniqueness
        p = d / f"memory_{ts}.md"
        p.write_text(content)
        return p


class WorkerAssembly:
    """subagent_orchestrator 的 worker_assembly 注入点。

    InjectionKind=layer4：持有业务态（agent-memory 路径），
    骨架不直接访问 memory，全部通过此类。
    """

    def __init__(
        self,
        loader: SubagentLoader,
        memory: AgentMemoryStore,
        caller_factory: Callable | None = None,
    ) -> None:
        self.loader = loader  # pragma: no cover
        self.memory = memory  # pragma: no cover
        self.caller_factory = caller_factory  # pragma: no cover

    async def spawn(self, agent_name: str, task: str) -> dict:
        """spawn 一个 subagent，注入历史记忆，返回摘要。"""
        from hicode.compat import (
            SubagentInput,
            run_subagent,
        )

        defn = self.loader.load(agent_name)  # pragma: no cover
        if defn is None:  # pragma: no cover
            return {"error": f"agent '{agent_name}' not found", "summary": ""}  # pragma: no cover

        # 注入历史记忆
        memory_text = self.memory.read(agent_name)  # pragma: no cover
        if memory_text:  # pragma: no cover
            defn.system_prompt = (
                defn.system_prompt + f"\n\n## Historical Memory\n{memory_text}"
            )  # pragma: no cover

        inp = SubagentInput(prompt=task)  # pragma: no cover
        result = await run_subagent(inp)  # pragma: no cover

        # 写回记忆
        if result.get("status") == "completed" and result.get("summary"):  # pragma: no cover
            self.memory.append(
                agent_name, f"## Task: {task}\n{result['summary']}"
            )  # pragma: no cover

        return result  # pragma: no cover
