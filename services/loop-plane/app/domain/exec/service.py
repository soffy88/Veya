"""loop-plane domain.exec — 硬化执行（SPEC §4.4 / §6.3）。

- AdapterRegistry 白名单；未知 tool_name → failed/permission_denied
- mode_policy：服务端强制收缩（mode 声明权限 ≤ adapter.needs，绝不放大）
- sandbox：写目录限 tmp/sandbox_{trace_id}；禁止 `python -m` 任意路径
- 底层: veya_loop.dispatch_intervention（HardenedExecutor + PermissionContract）
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.infra.event_store import AuditLog, EventStore, new_id

# 禁止 python -m 任意路径（SPEC §6.3）
_PYTHON_M_FORBIDDEN = re.compile(r"python\s+-m\s+\S+")

# mode 等级（sandbox 最严）: 服务端收缩只允许 adapter.needs <= mode 权限
MODE_LEVELS = {"sandbox": 0, "shadow": 1, "live_canary": 2}


@dataclass
class AdapterSpec:
    """执行适配器注册规格。needs: 声明所需权限等级（服务端强制收缩）。"""

    name: str
    fn: Callable[..., Any]
    needs: int = 0  # 0=sandbox, 1=shadow, 2=live_canary
    description: str = ""


class AdapterRegistry:
    """白名单适配器注册表。"""

    def __init__(self) -> None:
        self._adapters: dict[str, AdapterSpec] = {}

    def register(self, name: str, fn: Callable[..., Any], *, needs: int = 0, description: str = "") -> None:
        self._adapters[name] = AdapterSpec(name=name, fn=fn, needs=needs, description=description)

    def get(self, name: str) -> AdapterSpec | None:
        return self._adapters.get(name)

    def list(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "needs": s.needs, "description": s.description}
            for s in sorted(self._adapters.values(), key=lambda s: s.name)
        ]


class ExecService:
    """硬化执行服务：dispatch 单入口。"""

    def __init__(self, *, workspace: Path | None = None, registry: AdapterRegistry | None = None) -> None:
        self._workspace = Path(workspace or Path.cwd())
        self._registry = registry or AdapterRegistry()
        self._registry.register("echo", lambda text, **_ignored: {"echo": text}, needs=0,
                                description="echo 适配器（测试/演示）")
        self._runs: dict[str, dict[str, Any]] = {}

    @property
    def registry(self) -> AdapterRegistry:
        return self._registry

    def dispatch(
        self,
        *,
        mode: str,
        tool_name: str,
        args: dict[str, Any],
        trace_id: str = "",
        audit: AuditLog | None = None,
        store: EventStore | None = None,
    ) -> dict[str, Any]:
        """硬化执行分发（同步；异步执行留 Phase 2 扩展）。

        返回 {run_id, ok, mode, tool_name, output?, error?, permission}
        """
        run_id = new_id("run_")
        trace_id = trace_id or run_id
        mode_level = MODE_LEVELS.get(mode, 0)

        # 1. 白名单
        spec = self._registry.get(tool_name)
        if spec is None:
            result = self._failed(run_id, tool_name, mode, "permission_denied", f"未知 tool {tool_name!r}")
            self._record(run_id, result)
            self._audit(audit, "execute", trace_id, run_id, result, "permission_denied")
            self._event(store, "Run", run_id, "ActionFailed", {"tool_name": tool_name, "reason": "unknown_tool"}, trace_id)
            return result

        # 2. 服务端强制收缩: mode 权限 < adapter.needs → 拒绝
        if mode_level < spec.needs:
            result = self._failed(run_id, tool_name, mode, "permission_denied",
                                  f"mode={mode} 权限不足 (adapter.needs={spec.needs})")
            self._record(run_id, result)
            self._audit(audit, "execute", trace_id, run_id, result, "permission_denied")
            self._event(store, "Run", run_id, "ActionFailed", {"tool_name": tool_name, "reason": "mode_contraction"}, trace_id)
            return result

        # 3. sandbox 约束（SPEC §6.3）
        if mode == "sandbox":
            cmd = str(args.get("cmd") or args.get("command") or "")
            if _PYTHON_M_FORBIDDEN.search(cmd):
                result = self._failed(run_id, tool_name, mode, "permission_denied", "禁止 python -m 任意路径")
                self._record(run_id, result)
                self._audit(audit, "execute", trace_id, run_id, result, "forbidden")
                return result
            sandbox_root = Path(tempfile.gettempdir()) / f"sandbox_{trace_id[:16] or run_id}"
            args = {**args, "_sandbox_root": str(sandbox_root)}

        # 4. 执行（adapter 函数）
        try:
            output = spec.fn(**args)
            result = {
                "run_id": run_id, "ok": True, "mode": mode, "tool_name": tool_name,
                "output": output, "error": "", "permission": "granted",
                "trace_id": trace_id,
            }
            self._record(run_id, result)
            self._audit(audit, "execute", trace_id, run_id, result, "granted")
            self._event(store, "Run", run_id, "ActionSucceeded", {"tool_name": tool_name, "mode": mode}, trace_id)
            return result
        except Exception as exc:  # noqa: BLE001
            result = self._failed(run_id, tool_name, mode, "failed", f"{type(exc).__name__}: {exc}", trace_id=trace_id)
            self._record(run_id, result)
            self._audit(audit, "execute", trace_id, run_id, result, "error")
            self._event(store, "Run", run_id, "ActionFailed", {"tool_name": tool_name, "error": str(exc)}, trace_id)
            return result

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"run {run_id!r} 不存在")
        return run

    # ------------------------------------------------------------------ 内部

    def _record(self, run_id: str, result: dict[str, Any]) -> None:
        self._runs[run_id] = result

    @staticmethod
    def _failed(run_id: str, tool_name: str, mode: str, permission: str, error: str, *, trace_id: str = "") -> dict[str, Any]:
        return {
            "run_id": run_id, "ok": False, "mode": mode, "tool_name": tool_name,
            "output": None, "error": error, "permission": permission, "trace_id": trace_id,
        }

    @staticmethod
    def _audit(audit: AuditLog | None, phase: str, trace_id: str, run_id: str, result: dict[str, Any], decision: str) -> None:
        if audit is None:
            return
        audit.append(
            phase=phase, trace_id=trace_id,
            decision_made={"run_id": run_id, "tool": result["tool_name"], "decision": decision,
                           "ok": result["ok"], "mode": result["mode"]},
            context_snapshot={"workspace": str(Path.cwd())},
        )

    @staticmethod
    def _event(store: EventStore | None, agg_type: str, agg_id: str, event_type: str, payload: dict[str, Any], trace_id: str) -> None:
        if store is None:
            return
        store.append(aggregate_type=agg_type, aggregate_id=agg_id, event_type=event_type, payload=payload, trace_id=trace_id)


__all__ = ["AdapterRegistry", "ExecService"]
