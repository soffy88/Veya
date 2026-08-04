"""
layer4.session  — Session Manager
layer4.permission — Permission Gate
layer4.config   — Config Loader
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable as _Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 路径注入: layer4/layer4/session/__init__.py → veya/
_HERE = os.path.dirname(os.path.abspath(__file__))  # layer4/layer4/session/
_LAYER4_PKG = os.path.dirname(_HERE)  # layer4/layer4/
_LAYER4_ROOT = os.path.dirname(_LAYER4_PKG)  # layer4/
_VEYA = os.path.dirname(_LAYER4_ROOT)  # veya/


# ===========================================================================
# B. Session Manager
# ===========================================================================


@dataclass
class Session:
    """单个会话的完整业务态（不在引擎骨架里）。"""

    id: str = field(default_factory=lambda: f"sess_{uuid.uuid4().hex[:8]}")
    messages: list[dict] = field(default_factory=list)
    todos: list[dict] = field(default_factory=list)
    mode: str = "build"  # "build" | "plan"
    cost_usd: float = 0.0
    in_tokens: int = 0
    out_tokens: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    title: str = ""
    model: str = "claude-sonnet-4-6"
    cwd: str = ""

    def touch(self) -> None:
        self.updated_at = time.time()

    def summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.title or f"Session {self.id}",
            "messages": len(self.messages),
            "cost_usd": round(self.cost_usd, 4),
            "mode": self.mode,
            "created_at": self.created_at,
        }


class SessionSerializer:
    """Session ↔ JSON 序列化/反序列化。"""

    @staticmethod
    def to_dict(session: Session) -> dict:
        return {
            "id": session.id,
            "messages": session.messages,
            "todos": session.todos,
            "mode": session.mode,
            "cost_usd": session.cost_usd,
            "in_tokens": session.in_tokens,
            "out_tokens": session.out_tokens,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "title": session.title,
            "model": session.model,
            "cwd": session.cwd,
        }

    @staticmethod
    def from_dict(data: dict) -> Session:
        s = Session(id=data.get("id", f"sess_{uuid.uuid4().hex[:8]}"))
        s.messages = data.get("messages", [])
        s.todos = data.get("todos", [])
        s.mode = data.get("mode", "build")
        s.cost_usd = data.get("cost_usd", 0.0)
        s.in_tokens = data.get("in_tokens", 0)
        s.out_tokens = data.get("out_tokens", 0)
        s.created_at = data.get("created_at", time.time())
        s.updated_at = data.get("updated_at", time.time())
        s.title = data.get("title", "")
        s.model = data.get("model", "claude-sonnet-4-6")
        s.cwd = data.get("cwd", "")
        return s

    @staticmethod
    def to_json(session: Session) -> str:
        return json.dumps(SessionSerializer.to_dict(session), ensure_ascii=False)

    @staticmethod
    def from_json(raw: str) -> Session:
        return SessionSerializer.from_dict(json.loads(raw))


class SessionManager:
    """会话生命周期管理（创建/列出/恢复/删除）。

    本地存储到 ~/.veya/sessions/ 目录。
    生产版可替换为 obase.persistence 后端。
    """

    def __init__(self, store_dir: str | Path | None = None) -> None:
        self._dir = Path(store_dir or Path.home() / ".veya" / "sessions")
        self._dir.mkdir(parents=True, exist_ok=True)
        self._active: Session | None = None

    def create(
        self, *, mode: str = "build", model: str = "claude-sonnet-4-6", cwd: str = ""
    ) -> Session:
        sess = Session(mode=mode, model=model, cwd=cwd)
        self._save(sess)
        self._active = sess
        return sess

    def list(self) -> list[dict]:
        sessions = []
        for f in sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                sess = SessionSerializer.from_json(f.read_text())
                sessions.append(sess.summary())
            except Exception:  # pragma: no cover
                pass  # pragma: no cover
        return sessions

    def load(self, session_id: str) -> Session | None:
        p = self._dir / f"{session_id}.json"
        if not p.exists():
            return None
        try:
            sess = SessionSerializer.from_json(p.read_text())
            self._active = sess
            return sess
        except Exception:  # pragma: no cover
            return None  # pragma: no cover

    def save(self, session: Session) -> None:
        session.touch()
        self._save(session)

    def delete(self, session_id: str) -> bool:
        p = self._dir / f"{session_id}.json"
        if p.exists():
            p.unlink()
            return True
        return False  # pragma: no cover

    def _save(self, session: Session) -> None:
        p = self._dir / f"{session.id}.json"
        p.write_text(SessionSerializer.to_json(session))

    @property
    def active(self) -> Session | None:
        return self._active

    def switch(self, session_id: str) -> Session | None:
        return self.load(session_id)


class MultiSessionRouter:
    """/sessions 命令路由，多 session 切换。"""

    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager

    def handle(self, args: list[str]) -> str:
        if not args or args[0] == "list":
            sessions = self.manager.list()
            if not sessions:
                return "No sessions."
            lines = [f"{'ID':20} {'Messages':8} {'Cost':8} {'Mode':6} Title"]  # pragma: no cover
            lines.append("-" * 60)  # pragma: no cover
            for s in sessions:  # pragma: no cover
                lines.append(
                    f"{s['id']:20} {s['messages']:8} "  # pragma: no cover
                    f"${s['cost_usd']:7.4f} {s['mode']:6} {s['title'] or '—'}"
                )
            return "\n".join(lines)  # pragma: no cover

        if args[0] == "new":
            mode = args[1] if len(args) > 1 else "build"
            sess = self.manager.create(mode=mode)
            return f"Created session {sess.id} (mode={mode})"

        if args[0] == "switch" and len(args) > 1:
            sess = self.manager.switch(args[1])
            return f"Switched to {sess.id}" if sess else f"Session {args[1]} not found"

        if args[0] == "delete" and len(args) > 1:
            ok = self.manager.delete(args[1])
            return f"Deleted {args[1]}" if ok else f"Session {args[1]} not found"

        return f"Unknown sessions command: {args[0]}"


# ===========================================================================
# C. Permission Gate
# ===========================================================================


class ApprovalHistory:
    """记录已批准/已拒绝的工具，支持 'always allow this tool'。"""

    def __init__(self) -> None:
        self._always_allow: set[str] = set()
        self._always_deny: set[str] = set()
        self._history: list[dict] = []

    def always_allow(self, tool_name: str) -> None:
        self._always_allow.add(tool_name)

    def always_deny(self, tool_name: str) -> None:
        self._always_deny.add(tool_name)

    def is_always_allowed(self, tool_name: str) -> bool:
        return tool_name in self._always_allow

    def is_always_denied(self, tool_name: str) -> bool:
        return tool_name in self._always_deny

    def record(self, tool_name: str, decision: str) -> None:
        self._history.append({"tool": tool_name, "decision": decision, "ts": time.time()})

    def recent(self, n: int = 10) -> list[dict]:
        return self._history[-n:]


class PermissionPolicy:
    """加载 settings 里的 allow/deny 规则。"""

    def __init__(self, settings: dict | None = None) -> None:
        s = settings or {}
        self.allowed_tools: list[str] = s.get("allowed_tools", [])
        self.denied_tools: list[str] = s.get("denied_tools", [])
        self.mode: str = s.get("mode", "default")  # default/acceptEdits/plan/bypass

    @classmethod
    def from_file(cls, path: str | Path) -> PermissionPolicy:
        try:  # pragma: no cover
            data = json.loads(Path(path).read_text())  # pragma: no cover
            return cls(data.get("permissions", {}))  # pragma: no cover
        except Exception:  # pragma: no cover
            return cls()  # pragma: no cover


class ApprovalPrompt:
    """TUI 层的 y/n/always/deny 交互（可注入 mock 供测试）。

    默认实现：从 stdin 读取。
    测试/headless：注入 auto_approve=True/False。
    """

    def __init__(
        self,
        *,
        auto_approve: bool | None = None,
        callback: _Callable[[str, dict], str] | None = None,
    ) -> None:
        self._auto = auto_approve
        self._callback = callback

    async def ask(self, tool_name: str, tool_input: dict) -> str:
        """返回 'allow' | 'always' | 'deny' | 'deny_always'。"""
        if self._auto is True:
            return "allow"
        if self._auto is False:
            return "deny"
        if self._callback:
            return self._callback(tool_name, tool_input)
        # 默认：interactive stdin
        try:  # pragma: no cover
            print(f"\n⚠  Tool: {tool_name}")  # pragma: no cover
            print(
                f"   Input: {json.dumps(tool_input, ensure_ascii=False)[:200]}"
            )  # pragma: no cover
            resp = input("Allow? [y/n/always/deny-always] ").strip().lower()  # pragma: no cover
            return {  # pragma: no cover
                "y": "allow",
                "yes": "allow",
                "a": "always",
                "always": "always",
                "n": "deny",
                "no": "deny",
                "d": "deny_always",
                "deny-always": "deny_always",
            }.get(resp, "deny")
        except (EOFError, KeyboardInterrupt):  # pragma: no cover
            return "deny"  # pragma: no cover


class PermissionGate:
    """PreToolUse 权限闸门：调 match_permission_rule oskill + human-in-loop。

    作为 agentic_loop 的 pre_tool_gate 注入点。
    callable: (tool_call: dict) -> str  ("allow" | "deny" | "ask")
    """

    def __init__(
        self,
        *,
        policy: PermissionPolicy | None = None,
        history: ApprovalHistory | None = None,
        prompt: ApprovalPrompt | None = None,
    ) -> None:
        self.policy = policy or PermissionPolicy()
        self.history = history or ApprovalHistory()
        self.prompt = prompt or ApprovalPrompt(auto_approve=True)

    def __call__(self, tool_call: dict) -> str:
        """同步权限决策（供 agentic_loop 的 permission_gate 注入点）。"""
        name = tool_call.get("name", "")

        # history 里的 always 规则
        if self.history.is_always_allowed(name):
            return "allow"
        if self.history.is_always_denied(name):
            return "deny"  # pragma: no cover

        # policy 规则（调 compat match_permission_rule）
        try:
            from veya.compat import match_permission_rule

            decision = match_permission_rule(
                tool_call,
                allowed_tools=self.policy.allowed_tools,
                denied_tools=self.policy.denied_tools,
                mode=self.policy.mode,
            )
        except ImportError:  # pragma: no cover
            decision = "ask"  # pragma: no cover

        return decision

    async def ask_human(self, tool_name: str, tool_input: dict) -> str:
        """human-in-loop：调 ApprovalPrompt，更新 history。"""
        resp = await self.prompt.ask(tool_name, tool_input)
        if resp == "always":
            self.history.always_allow(tool_name)
            resp = "allow"
        elif resp == "deny_always":
            self.history.always_deny(tool_name)
            resp = "deny"
        self.history.record(tool_name, resp)
        return resp


# ===========================================================================
# F. Config Loader
# ===========================================================================


@dataclass
class EffectiveConfig:
    """合并后的有效配置。"""

    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-4-6"
    budget_usd: float = 10.0
    mode: str = "build"
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    hooks: list[dict] = field(default_factory=list)
    mcp_servers: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


class GlobalConfig:
    """~/.config/veya/settings.json 加载。"""

    PATH = Path.home() / ".config" / "veya" / "settings.json"

    @classmethod
    def load(cls) -> dict:
        try:
            return json.loads(cls.PATH.read_text())
        except Exception:
            return {}


class ProjectConfig:
    """.veya/settings.json 加载（从 cwd 向上查找）。"""

    @classmethod
    def load(cls, cwd: str | Path = ".") -> dict:
        p = Path(cwd).resolve()
        while p != p.parent:
            candidate = p / ".veya" / "settings.json"
            if candidate.exists():
                try:
                    return json.loads(candidate.read_text())
                except Exception:  # pragma: no cover
                    return {}  # pragma: no cover
            p = p.parent
        return {}


class AgentsMdLoader:
    """AGENTS.md 解析 → 配置 dict（调 resolve_memory_hierarchy oskill）。"""

    @classmethod
    def load(cls, cwd: str | Path = ".") -> dict:
        try:
            from veya.compat import resolve_memory_hierarchy

            agents_md = Path(cwd) / "AGENTS.md"
            result = resolve_memory_hierarchy(project=str(agents_md))
            return {"agents_md_content": result.get("content", "")}
        except Exception:  # pragma: no cover
            return {}  # pragma: no cover


class ModelSelector:
    """解析 llm_provider/llm_model 字段，返回 obase.ProviderRegistry handle。"""

    @classmethod
    def get_caller(cls, config: EffectiveConfig) -> Any:
        """从 obase.ProviderRegistry 取 LLMCaller 实例。"""
        try:  # pragma: no cover
            from obase import ProviderRegistry  # type: ignore  # pragma: no cover

            return ProviderRegistry().get(config.llm_provider, config.llm_model)  # pragma: no cover
        except ImportError:  # pragma: no cover
            # Fallback: 返回 None，由调用方处理
            return None  # pragma: no cover


class ConfigLoader:
    """三层配置合并：global → project → AGENTS.md，调 merge_config oskill。"""

    @classmethod
    def load(cls, cwd: str | Path = ".") -> EffectiveConfig:
        g = GlobalConfig.load()
        p = ProjectConfig.load(cwd)
        a = AgentsMdLoader.load(cwd)

        try:
            from veya.compat import merge_config

            merged = merge_config(g, p, a)
        except ImportError:  # pragma: no cover
            merged = {**g, **p, **a}  # pragma: no cover

        cfg = EffectiveConfig()
        cfg.llm_provider = merged.get("llm_provider", cfg.llm_provider)
        cfg.llm_model = merged.get("llm_model", cfg.llm_model)
        cfg.budget_usd = merged.get("budget_usd", cfg.budget_usd)
        cfg.mode = merged.get("mode", cfg.mode)
        cfg.allowed_tools = merged.get("allowed_tools", [])
        cfg.denied_tools = merged.get("denied_tools", [])
        cfg.hooks = merged.get("hooks", [])
        cfg.mcp_servers = merged.get("mcp_servers", [])
        cfg.extra = {k: v for k, v in merged.items() if k not in cfg.__dataclass_fields__}
        return cfg
