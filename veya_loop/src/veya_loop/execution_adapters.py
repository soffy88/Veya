"""veya_loop.execution_adapters — 真实执行适配器模板 + 参考实现 (重启)。

P0 落地: 给 dispatch_intervention 提供"业务适配器"层。

模板 (ExecutionAdapter):
  动作 → argv + 权限动作名 + 审计描述 的契约;
  具体业务 (重启/限流/发布...) 实现 build_argv / permission_action / describe,
  经 dispatch_via_adapter 走 授权 → 硬化执行 → 审计 三合一链路。

参考实现 (RestartAdapter):
  - systemctl / docker 两种模式;
  - probe() 提供"重启前健康检查" (可选, is-active / inspect);
  - 与 PermissionContract 规则 "restart:*" 对齐。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ._assembly import oprim as _load_oprim
from .hardened import DispatchResult, HardenedExecutor, PermissionContract

_oprim = _load_oprim()


class ExecutionAdapter(ABC):
    """执行适配器模板: 业务动作 → 可审计的硬化派发。

    实现方只需定义:
      action_prefix  权限规则前缀 (如 "restart")
      build_argv     目标 → 具体命令 argv
      describe       目标 → 审计备注
    派发契约 (dispatch_via_adapter):
      permission_action(target) = f"{prefix}:{target}" — 规则 "restart:*" 命中。
    """

    action_prefix: str = "action"

    @abstractmethod
    def build_argv(self, target: str, **params: Any) -> list[str]:
        """目标 → 可执行 argv (交给 HardenedExecutor)。"""

    def permission_action(self, target: str) -> str:
        return f"{self.action_prefix}:{target}"

    def describe(self, target: str) -> str:
        return f"{self.action_prefix} 动作: {target}"


class RestartAdapter(ExecutionAdapter):
    """重启适配器参考实现 (systemctl / docker)。

    Example:
        adapter = RestartAdapter(mode="systemctl", grace_period_s=10.0)
        result = dispatch_via_adapter(
            adapter, "api_service",
            contract=contract, executor=executor, emitter=emitter,
        )
    """

    action_prefix = "restart"

    def __init__(self, *, mode: str = "systemctl", grace_period_s: float = 5.0):
        if mode not in ("systemctl", "docker"):
            raise ValueError(f"mode 必须是 systemctl|docker, 收到 {mode!r}")
        self.mode = mode
        self.grace_period_s = float(grace_period_s)

    def build_argv(self, target: str, **params: Any) -> list[str]:
        if self.mode == "docker":
            return ["docker", "restart", target]
        return ["systemctl", "restart", target]

    def describe(self, target: str) -> str:
        return (f"重启 {target} ({self.mode}, 宽限期 {self.grace_period_s}s)")

    # ── 参考实现附带: 重启前健康检查 (可选, probe 失败可阻断派发) ──────
    def probe_argv(self, target: str) -> list[str]:
        """健康检查命令 (is-active / inspect), 供重启前验证。"""
        if self.mode == "docker":
            return ["docker", "inspect", "-f", "{{.State.Running}}", target]
        return ["systemctl", "is-active", target]

    def probe(self, target: str, executor: HardenedExecutor,
              timeout_s: float = 10.0) -> bool:
        """重启前探测: 服务是否存活。返回 bool (超时/非零 → False)。"""
        out = executor.execute(self.probe_argv(target), timeout_s=timeout_s)
        return out.ok and out.stdout.strip().lower() in ("active", "true")


def dispatch_via_adapter(
    adapter: ExecutionAdapter,
    target: str,
    *,
    contract: PermissionContract,
    executor: HardenedExecutor | None = None,
    emitter: Any | None = None,          # oprim.AuditEmitter
    actor: str = "",
    resource: str = "",
    probe_first: bool = False,
    probe_timeout_s: float = 10.0,
    **params: Any,
) -> DispatchResult:
    """适配器派发: probe(可选) → 授权 → 硬化执行 → 审计。

    与 dispatch_intervention 同一链路, 只是动作名/argv/审计描述由适配器提供。
    """
    action = adapter.permission_action(target)
    argv = adapter.build_argv(target, **params)
    notes = adapter.describe(target)

    if (probe_first and executor is not None
            and hasattr(adapter, "probe") and hasattr(adapter, "probe_argv")
            and not adapter.probe(target, executor, timeout_s=probe_timeout_s)):
        if emitter is not None:
            emitter.decide(decision={"chosen_strategy": action,
                                     "denied": True,
                                     "reason": "probe 失败: 服务已不可用"},
                           context={"notes": notes})
        return DispatchResult("denied", action, reason="probe failed: service unavailable")

    from .hardened import dispatch_intervention

    return dispatch_intervention(
        action, argv,
        contract=contract, executor=executor, emitter=emitter,
        resource=resource, actor=actor, notes=notes,
    )


__all__ = ["ExecutionAdapter", "RestartAdapter", "dispatch_via_adapter"]
