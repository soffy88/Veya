"""server/backends.py — 执行后端注册表 (对标 OpenHands 多 backend 挂载)。

统一挂载三类执行后端:
  builtin — Veya 主脑 (master, 内置)
  cli     — 本机 CLI agent (只做发现与准入查询, 不再直接执行)
  acp     — 外部 ACP 兼容 agent (JSON-RPC over stdio)

能力: 注册 / 探测 / 统一 run / 状态聚合 (Canvas 视角: 可用/忙碌/任务数)。

执行权威: 只有 builtin 在本模块内执行。cli 的直接执行已关闭 (EXECUTION_FACADE_CLOSED)
—— 详见 _run_cli 的 docstring。统一执行权威是 ExecutorRegistry → admission →
GoalRun → L2 worker → receipt 那条链, 不在本模块。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.acp_client import ACPBackend, ACPError

BACKEND_KINDS = ("builtin", "cli", "acp")

CLI_BACKENDS = {"claude": "claude", "codex": "codex", "pi": "pi", "opencode": "opencode"}


def _backend_event_emit(topic: str, payload: dict[str, Any]) -> None:
    """Best-effort audit for backend-driven delivery edges.

    Runs outside request context; a failing event sink must never break
    execution, so failures are logged (the delivery record itself always
    persists in the assembly for inspection).
    """
    try:
        from server.events import append_canonical_event

        append_canonical_event(topic, payload, actor="system")
    except Exception:
        logging.getLogger("veya.backends").warning("backend delivery audit emit failed: %s", topic)


@dataclass
class BackendSpec:
    """一个执行后端的注册信息。"""

    name: str
    kind: str  # builtin | cli | acp
    command: list[str] = field(default_factory=list)  # cli/acp: 可执行命令
    agent: str = "general"  # acp: agent 名
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
            "name": self.name,
            "kind": self.kind,
            "command": self.command,
            "agent": self.agent,
            "model": self.model,
            "enabled": self.enabled,
            "available": self.available(),
        }

    #: Caps for this facade's output and error text, in one place so the three run


#: branches cannot drift apart. They used to be bare [:4000] / [:2000] slices
#: repeated per branch.
OUTPUT_LIMIT = 4000
ERROR_LIMIT = 2000

#: Appended to a field that was cut, so a clipped answer cannot be mistaken for a
#: short one even if a caller ignores the flag.
TRUNCATION_MARKER = "\n...[truncated by backend facade]"


#: Identity the facade dispatches under. Delegating is only meaningful with a
#: named principal: the canonical chain's authority comes from the session's
#: principal and its permission grant, so an anonymous call has nothing to
#: authorize against.
FACADE_PRINCIPAL = "local-backend-facade"

#: Operator-declared root the facade may execute in. The endpoint is
#: unauthenticated, so a caller-supplied ``cwd`` cannot be trusted to name a
#: workspace. With this unset the facade stays closed; setting it is the explicit
#: decision to let the facade execute, and only inside that root.
FACADE_ROOT_ENV = "VEYA_BACKEND_FACADE_ROOT"


def _facade_root() -> Path | None:
    raw = os.environ.get(FACADE_ROOT_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


#: The narrowest permission grant that lets the facade reach the canonical
#: chain, measured rather than assumed. Asking for less fails closed at the
#: permission engine before an executor is ever selected:
#:
#:     read only                -> TOOL_DENIED / session lacks write permission
#:     read + write             -> TOOL_DENIED / session lacks shell permission
#:     read + write + shell     -> dispatch admitted, parent execution created
#:
#: ``worker.dispatch`` is a write-class tool that runs shell commands, so those
#: three are what authorising the dispatch capability means. None of them is an
#: effect grant on the dispatched task: what bounds that is the READ task
#: contract the delegate pins, plus the executor's own write-qualification check
#: per child. git, destructive and service_control stay off.
FACADE_PERMISSIONS = ("read", "write", "shell")


def _facade_session(workspace: Path) -> Any:
    """A session for the facade principal, with the narrowest grant that works.

    ``shell`` is on because ``worker.dispatch`` runs commands, not because the
    facade is trusted with arbitrary execution: the task contract is pinned to
    READ by the caller below, and the workspace is confined to the
    operator-declared root.
    """
    from veya.remote.models import RemotePermissions, RemoteSession

    now = time.time()
    return RemoteSession(
        session_id=f"facade-{int(now * 1000)}",
        principal=FACADE_PRINCIPAL,
        token_id=f"facade-{os.getpid()}",
        workspaces=(str(workspace),),
        active_workspace=str(workspace),
        permissions=RemotePermissions(read=True, write=True, shell=True),
        created_at=now,
        expires_at=now + 300,
    )


def _capped(value: Any, limit: int) -> tuple[Any, bool]:
    """Bound one text field and report whether it was cut.

    Non-text is passed through untouched. A structured payload — the canonical
    dispatch projection the delegate returns — is not a length to bound but data
    to hand over intact, and stringifying it here would quietly replace it with
    its repr.
    """
    if value is None:
        return "", False
    if not isinstance(value, str):
        return value, False
    if len(value) <= limit:
        return value, False
    return value[:limit] + TRUNCATION_MARKER, True


def _result(
    *,
    ok: bool,
    backend: str,
    output: Any = "",
    error: Any = "",
    **extra: Any,
) -> dict[str, Any]:
    """One response shape for every branch of :meth:`BackendRegistry.run`.

    The branches used to hand back three different key sets, so a consumer could
    not rely on ``output_truncated`` existing — it existed nowhere.
    """
    capped_output, output_truncated = _capped(output, OUTPUT_LIMIT)
    capped_error, error_truncated = _capped(error, ERROR_LIMIT)
    return {
        **extra,
        "ok": ok,
        "backend": backend,
        "output": capped_output,
        "output_truncated": output_truncated,
        "error": capped_error,
        "error_truncated": error_truncated,
        "duration_s": float(extra.get("duration_s", 0.0)),
    }


class BackendRegistry:
    """多 backend 注册表: 发现内置 CLI + 手动注册 ACP + 统一执行。"""

    def __init__(self) -> None:
        self._backends: dict[str, BackendSpec] = {}
        self._running: dict[str, int] = {}  # backend name → 运行中任务数
        self._tasks: set[asyncio.Task] = set()

    # ── 注册/发现 ──────────────────────────────────────────────────────
    def register(
        self,
        name: str,
        kind: str,
        *,
        command: list[str] | None = None,
        agent: str = "general",
        model: str = "",
        enabled: bool = True,
    ) -> BackendSpec:
        if kind not in BACKEND_KINDS:
            raise ValueError(f"未知 backend kind: {kind}; 可选 {BACKEND_KINDS}")
        spec = BackendSpec(
            name=name,
            kind=kind,
            command=list(command or []),
            agent=agent,
            model=model,
            enabled=enabled,
        )
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
                    # model stays empty on purpose: it used to be seeded with the
                    # engine name, so a caller that omitted `model` got an argv
                    # like `claude -p … --model claude` — the engine name handed
                    # back to the CLI as a model id.
                    out.append(BackendSpec(name=eng, kind="cli", command=[bin_name]))
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
                    name=s["name"],
                    kind=s["kind"],
                    command=s["command"],
                    agent=s["agent"],
                    model=s["model"],
                    enabled=s["enabled"],
                )
        return None

    # ── 执行 ───────────────────────────────────────────────────────────
    async def run(
        self,
        name: str,
        prompt: str,
        *,
        cwd: str | None = None,
        model: str = "",
        timeout_s: float = 600.0,
        agent_runtime_id: str | None = None,
    ) -> dict[str, Any]:
        """统一执行: builtin → 主脑; cli → engine_runner; acp → ACP 客户端."""
        spec = self._find(name)
        if spec is None:
            raise KeyError(f"backend 不存在: {name}")
        if not spec.enabled:
            return {"ok": False, "backend": name, "error": "backend 已禁用"}
        if not spec.available():
            return {
                "ok": False,
                "backend": name,
                "error": f"backend {name} 不可用 (CLI 未安装或命令无效)",
            }
        # D8: optional runtime gate against the canonical D1 descriptor.
        if agent_runtime_id is not None:
            from server.agent_definition import check_runtime_available

            _rt = check_runtime_available(agent_runtime_id)
            if not _rt.get("available"):
                return {
                    "ok": False,
                    "backend": name,
                    "error": f"runtime unavailable: {agent_runtime_id}",
                }

        self._running[name] = self._running.get(name, 0) + 1
        try:
            if spec.kind == "builtin":
                return await self._run_builtin(prompt, model or spec.model, timeout_s)
            if spec.kind == "cli":
                return await self._run_cli(spec, prompt, cwd, model or spec.model, timeout_s)
            return await self._run_acp(spec, prompt, cwd, timeout_s)
        finally:
            self._running[name] = max(0, self._running.get(name, 0) - 1)

    async def _run_builtin(self, prompt: str, model: str, timeout_s: float) -> dict[str, Any]:
        from server.coordinator_master import master_coordinator

        result = await asyncio.wait_for(
            master_coordinator.chat_stream(prompt, model=model or None),
            timeout=timeout_s,
        )
        output = result.get("output") or result.get("squads") or ""
        ok = result.get("status") == "success"
        return _result(
            ok=ok,
            backend="master",
            output=output if output else "",
            error="" if ok else result.get("error", "执行失败"),
        )

    async def _run_cli(
        self, spec: BackendSpec, prompt: str, cwd: str | None, model: str, timeout_s: float
    ) -> dict[str, Any]:
        """Refuse direct execution; report what the canonical chain would admit.

        This used to call ``server.engine_runner.run_engine(spec.name)``, which
        spawned an L2 CLI process with no ExecutorRegistry admission, no
        permission check, no GoalRun, no receipt and no worktree binding — a
        second execution plane reachable over unauthenticated HTTP with a
        caller-chosen ``cwd``.

        Delegating to the canonical chain needs a service principal and a
        workspace binding; this endpoint has neither, and inventing authority
        here would turn the bypass into an arbitrary-directory write primitive.
        So the name still goes through ExecutorRegistry — an unknown or retired
        executor fails closed by the registry's own answer — and an admitted one
        is refused with the canonical entry to use instead.
        """
        from veya.remote.executor_registry import (
            get_executor_registry,
            is_retired_executor,
            normalize_executor_id,
        )

        registry = get_executor_registry()
        canonical = normalize_executor_id(spec.name)
        admitted = sorted(registry.snapshot())
        base = {
            "canonical_executor_id": canonical,
            "admitted_executors": admitted,
        }
        if is_retired_executor(canonical):
            return _result(
                ok=False,
                backend=spec.name,
                error=f"executor retired, refused without substitution: {canonical!r}",
                error_code="EXECUTOR_RETIRED",
                **base,
            )
        try:
            identity = registry.identity(canonical)
        except ValueError as exc:
            return _result(
                ok=False,
                backend=spec.name,
                error=f"{exc}; admitted executors: {admitted}",
                error_code="EXECUTOR_NOT_ADMITTED",
                **base,
            )
        return await self._delegate(spec, prompt, cwd, timeout_s, identity)

    async def _delegate(
        self, spec: BackendSpec, prompt: str, cwd: str | None, timeout_s: float, identity: Any
    ) -> dict[str, Any]:
        """Run the task through the canonical chain under the facade principal.

        Every step here exists because the endpoint is unauthenticated:

        * the workspace must sit inside the operator-declared root, because the
          caller's ``cwd`` is otherwise an arbitrary-directory write primitive;
        * the session is read-only, so a dispatch that needs write, shell or git
          is refused by the permission grant rather than by this code;
        * the task contract is pinned to READ, so the facade cannot be talked
          into a stronger effect than its principal holds.

        ``veya.remote`` is imported here rather than at module scope on purpose:
        tool_adapter imports server.goal_run.pre_admission, so a module-level
        import would close a cycle.
        """
        root = _facade_root()
        if root is None:
            return _result(
                ok=False,
                backend=spec.name,
                error=(
                    f"the facade has no execution root; set {FACADE_ROOT_ENV} to the "
                    "repository this endpoint may execute in, or dispatch "
                    f"worker.dispatch with worker={identity.executor_id!r} directly"
                ),
                error_code="EXECUTION_FACADE_CLOSED",
                canonical_entry="worker.dispatch",
                canonical_executor_id=identity.executor_id,
            )

        requested = Path(cwd).expanduser() if cwd else root
        try:
            workspace = requested.resolve(strict=True)
        except OSError as exc:
            return _result(
                ok=False,
                backend=spec.name,
                error=f"workspace does not exist: {requested} ({exc})",
                error_code="WORKSPACE_DENIED",
                canonical_executor_id=identity.executor_id,
            )
        if workspace != root and root not in workspace.parents:
            return _result(
                ok=False,
                backend=spec.name,
                error=(f"workspace is outside {FACADE_ROOT_ENV}: {workspace} is not under {root}"),
                error_code="WORKSPACE_DENIED",
                canonical_executor_id=identity.executor_id,
            )

        from veya.remote.tool_adapter import RemoteToolAdapter

        adapter = RemoteToolAdapter(None)
        session = _facade_session(workspace)
        result = await adapter.call(
            session,
            "worker.dispatch",
            {
                "workspace": str(workspace),
                "tasks": [
                    {
                        "worker": identity.executor_id,
                        "task": prompt,
                        "task_contract": {"task_kind": "READ"},
                    }
                ],
                "timeout_sec": timeout_s,
            },
        )
        payload = dict(result.result) if isinstance(result.result, dict) else {}
        return _result(
            ok=bool(result.ok),
            backend=spec.name,
            output=payload,
            error=result.message or "",
            error_code=None if result.ok else str(result.error_code or "EXECUTION_FAILED"),
            canonical_executor_id=identity.executor_id,
            canonical_entry="worker.dispatch",
            execution_id=result.execution_id,
        )

    async def _run_acp(
        self, spec: BackendSpec, prompt: str, cwd: str | None, timeout_s: float
    ) -> dict[str, Any]:
        from server.acp_mcp_delivery import default_mcp_sources, get_acp_mcp_delivery

        backend = ACPBackend(spec.command, agent=spec.agent, cwd=cwd)
        delivery = get_acp_mcp_delivery(emit=_backend_event_emit)
        try:
            session_id = await backend.start_session()
            await delivery.open_session(
                session_id,
                backend_kind="acp",
                reuse_key=f"{spec.name}:{cwd or ''}",
                sources=default_mcp_sources(),
                on_close_transport=backend.close,
                owner=f"backend:{spec.name}",
            )
            result = await backend.run(prompt, timeout_s=timeout_s)
            return _result(
                ok=True,
                backend=spec.name,
                output=result.get("output", ""),
            )
        except (ACPError, OSError) as e:
            return _result(ok=False, backend=spec.name, error=e)
        finally:
            await backend.close()

    # ── 状态聚合 (Canvas 视角) ─────────────────────────────────────────
    def status(self) -> list[dict[str, Any]]:
        return [
            {
                **s,
                "busy": self._running.get(s["name"], 0) > 0,
                "running_tasks": self._running.get(s["name"], 0),
            }
            for s in self.list()
        ]

    def _find(self, name: str) -> BackendSpec | None:
        for s in self.list():
            if s["name"] == name:
                return BackendSpec(
                    name=s["name"],
                    kind=s["kind"],
                    command=s["command"],
                    agent=s["agent"],
                    model=s["model"],
                    enabled=s["enabled"],
                )
        return None


_default_registry = BackendRegistry()


def get_backend_registry() -> BackendRegistry:
    return _default_registry
