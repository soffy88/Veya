"""server/backends.py — 执行后端注册表 (对标 OpenHands 多 backend 挂载)。

统一挂载三类执行后端:
  builtin — Veya 主脑 (master, 内置)
  cli     — 本机 CLI agent (claude / codex / pi, 走 engine_runner)
  acp     — 外部 ACP 兼容 agent (走 acp_client, JSON-RPC over stdio)

能力: 注册 / 探测 / 统一 run / 状态聚合 (Canvas 视角: 可用/忙碌/任务数)。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from server.acp_client import ACPBackend, ACPError

BACKEND_KINDS = ("builtin", "cli", "acp")

CLI_BACKENDS = {"claude": "claude", "codex": "codex", "pi": "pi"}


@dataclass
class BackendSpec:
    """一个执行后端的注册信息。"""

    name: str
    kind: str                    # builtin | cli | acp
    command: list[str] = field(default_factory=list)  # cli/acp: 可执行命令
    agent: str = "general"       # acp: agent 名
    model: str = ""
    enabled: bool = True
    created_at: float = field(default_factory=time.time)

    def available(self) -> bool:
        if self.kind == "builtin":
            return True
        if not self.command:
            return False
        return shutil.which(self.command[0]) is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind,
            "command": self.command, "agent": self.agent,
            "model": self.model, "enabled": self.enabled,
            "available": self.available(),
        }


class BackendRegistry:
    """多 backend 注册表: 发现内置 CLI + 手动注册 ACP + 统一执行。"""

    def __init__(self) -> None:
        self._backends: dict[str, BackendSpec] = {}
        self._running: dict[str, int] = {}   # backend name → 运行中任务数
        self._tasks: set[asyncio.Task] = set()

    # ── 注册/发现 ──────────────────────────────────────────────────────
    def register(self, name: str, kind: str, *, command: list[str] | None = None,
                 agent: str = "general", model: str = "",
                 enabled: bool = True) -> BackendSpec:
        if kind not in BACKEND_KINDS:
            raise ValueError(f"未知 backend kind: {kind}; 可选 {BACKEND_KINDS}")
        spec = BackendSpec(name=name, kind=kind, command=list(command or []),
                           agent=agent, model=model, enabled=enabled)
        self._backends[name] = spec
        return spec

    def discover(self) -> list[BackendSpec]:
        """内置发现: master + 本机 CLI (容器环境只保留 master)。"""
        out: list[BackendSpec] = [
            BackendSpec(name="master", kind="builtin"),
        ]
        if not self._container_env():
            for eng, bin_name in CLI_BACKENDS.items():
                if shutil.which(bin_name):
                    out.append(BackendSpec(
                        name=eng, kind="cli", command=[bin_name], model=eng))
        return out

    def list(self) -> list[dict[str, Any]]:
        specs: dict[str, BackendSpec] = {}
        for s in self.discover():
            specs[s.name] = s
        for name, s in self._backends.items():
            specs[name] = s
        return [s.to_dict() for s in specs.values()]

    @staticmethod
    def _container_env() -> bool:
        import os

        return bool(os.environ.get("VEYA_WORKSPACE")) or os.path.exists("/.dockerenv")

    def get(self, name: str) -> BackendSpec | None:
        for s in self.list():
            if s["name"] == name:
                return self._backends.get(name) or BackendSpec(
                    name=s["name"], kind=s["kind"], command=s["command"],
                    agent=s["agent"], model=s["model"], enabled=s["enabled"])
        return None

    # ── 执行 ───────────────────────────────────────────────────────────
    async def run(self, name: str, prompt: str, *, cwd: str | None = None,
                  model: str = "", timeout_s: float = 600.0) -> dict[str, Any]:
        """统一执行: builtin → 主脑; cli → engine_runner; acp → ACP 客户端。"""
        spec = self._find(name)
        if spec is None:
            raise KeyError(f"backend 不存在: {name}")
        if not spec.enabled:
            return {"ok": False, "backend": name, "error": "backend 已禁用"}
        if not spec.available():
            return {"ok": False, "backend": name,
                    "error": f"backend {name} 不可用 (CLI 未安装或命令无效)"}

        self._running[name] = self._running.get(name, 0) + 1
        try:
            if spec.kind == "builtin":
                return await self._run_builtin(prompt, model or spec.model, timeout_s)
            if spec.kind == "cli":
                return await self._run_cli(spec, prompt, cwd, model or spec.model, timeout_s)
            return await self._run_acp(spec, prompt, cwd, timeout_s)
        finally:
            self._running[name] = max(0, self._running.get(name, 0) - 1)

    async def _run_builtin(self, prompt: str, model: str,
                           timeout_s: float) -> dict[str, Any]:
        from server.coordinator import coordinator

        result = await asyncio.wait_for(
            coordinator.handle({"text": prompt, "persona": "build"}),
            timeout=timeout_s,
        )
        output = result.get("output") or result.get("squads") or ""
        ok = result.get("status") == "success"
        return {"ok": ok, "backend": "master",
                "output": str(output)[:4000] if output else "",
                "error": "" if ok else str(result.get("error", "执行失败"))[:2000],
                "duration_s": 0.0}

    async def _run_cli(self, spec: BackendSpec, prompt: str, cwd: str | None,
                       model: str, timeout_s: float) -> dict[str, Any]:
        from server.engine_runner import run_engine

        result = await run_engine(spec.name, prompt, model=model or None,
                                  cwd=cwd, timeout_s=timeout_s)
        return {"ok": bool(result.get("ok")), "backend": spec.name,
                "output": str(result.get("output", ""))[:4000],
                "error": str(result.get("error", ""))[:2000],
                "duration_s": float(result.get("duration_s", 0.0))}

    async def _run_acp(self, spec: BackendSpec, prompt: str, cwd: str | None,
                       timeout_s: float) -> dict[str, Any]:
        backend = ACPBackend(spec.command, agent=spec.agent, cwd=cwd)
        try:
            result = await backend.run(prompt, timeout_s=timeout_s)
            return {"ok": True, "backend": spec.name,
                    "output": str(result.get("output", ""))[:4000],
                    "error": "", "duration_s": 0.0}
        except (ACPError, OSError) as e:
            return {"ok": False, "backend": spec.name, "error": str(e)[:2000],
                    "output": "", "duration_s": 0.0}
        finally:
            await backend.close()

    # ── 状态聚合 (Canvas 视角) ─────────────────────────────────────────
    def status(self) -> list[dict[str, Any]]:
        return [{
            **s,
            "busy": self._running.get(s["name"], 0) > 0,
            "running_tasks": self._running.get(s["name"], 0),
        } for s in self.list()]

    def _find(self, name: str) -> BackendSpec | None:
        for s in self.list():
            if s["name"] == name:
                return BackendSpec(
                    name=s["name"], kind=s["kind"], command=s["command"],
                    agent=s["agent"], model=s["model"], enabled=s["enabled"])
        return None


_default_registry = BackendRegistry()


def get_backend_registry() -> BackendRegistry:
    return _default_registry
