"""veya_loop.hardened — 硬化执行器 / 授权契约 / 干预派发 (Veya Loop 自有组件)。

主库元素只做机制; 本模块是 Veya Loop 的业务装配层:

  HardenedExecutor    把 oprim._sandbox 的隔离沙箱 + 环境冻结 + 超时,
                      包成带审计的「硬化执行器」。
  PermissionContract  干预授权契约: 规则匹配 + nonce 签发/校验 + 决策留痕。
                      (obase.auth 只管身份 JWT; 动作级授权是本包的职责。)
  dispatch_intervention
                      「授权 → 硬化执行 → 审计落笔」一条调用完成 ——
                      Veya Loop 对外的最小派发入口。
"""

from __future__ import annotations

import fnmatch
import hashlib
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ._assembly import oprim as _load_oprim

_oprim = _load_oprim()


# =========================================================================
# HardenedExecutor — 硬化执行器
# =========================================================================


@dataclass
class ExecOutcome:
    argv: list[str]
    exit_code: int
    ok: bool
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    def summary(self, limit: int = 300) -> str:
        return (self.stdout or self.stderr)[:limit]


class HardenedExecutor:
    """隔离沙箱执行器: unshare 命名空间 + 环境变量冻结 + 超时 + 预热池。

    安全边界 (继承 oprim._sandbox):
      - isolation="netns": `unshare -Urn` (seccomp 禁用时自动回落 none, 不假装隔离);
      - 环境变量全清空再按需注入 (API key/代理不泄入沙箱);
      - PYTHONHASHSEED=0 / TZ=UTC 冻结不确定性;
      - 超时强制杀进程。
    """

    def __init__(
        self,
        *,
        isolation: str = "netns",
        timeout_s: float = 30.0,
        pool_size: int = 4,
        base_dir: str | None = None,
        env: dict | None = None,
    ):
        self.isolation = isolation
        self.timeout_s = timeout_s
        # env 提供时: 用自定义环境的沙箱工厂 (清空宿主环境 + 按需注入 env);
        # 未提供时用池默认工厂 (冻结的确定性默认环境, 见 oprim._sandbox)。
        self.env = dict(env) if env is not None else None
        factory = None
        if self.env is not None:
            _env = self.env
            factory = lambda: _oprim.LocalSandbox(base_dir, isolation, env=_env)  # noqa: E731
        self._pool = _oprim.SandboxPool(
            size=max(1, pool_size), base_dir=base_dir, isolation=isolation, factory=factory
        )

    def __enter__(self) -> HardenedExecutor:
        self._pool.prewarm()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self._pool.shutdown()
        return False

    def execute(self, argv: Sequence[str], *, timeout_s: float | None = None) -> ExecOutcome:
        """在预热池沙箱中执行命令, 返回结构化结果。"""
        sb = self._pool.acquire()
        try:
            r = sb.run(list(argv), timeout_s=timeout_s or self.timeout_s)
            return ExecOutcome(
                list(argv), r.exit_code, r.ok, r.stdout, r.stderr, r.duration_ms, r.timed_out
            )
        finally:
            self._pool.release(sb)

    def shutdown(self) -> None:
        self._pool.shutdown()

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "created": self._pool.stats.created,
            "acquires": self._pool.stats.acquires,
            "avg_acquire_ms": round(self._pool.stats.avg_wait_ms, 2),
            "isolation": self.isolation,
        }


# =========================================================================
# PermissionContract — 授权契约
# =========================================================================


@dataclass
class PermissionDecision:
    allowed: bool
    reason: str
    nonce: str | None = None
    action: str = ""
    resource: str = ""
    actor: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "nonce": self.nonce,
            "action": self.action,
            "resource": self.resource,
            "actor": self.actor,
        }


def _rule_matches(pattern: str, action: str) -> bool:
    """glob 匹配动作段: "do*" 匹配 "do(mode=healthy)"; "danger*" 匹配 "danger:drop_table"。
    资源段匹配由 evaluate() 单独处理 (rule["resource"])。"""
    if not pattern:
        return False
    return fnmatch.fnmatch(action, pattern)


class PermissionContract:
    """干预授权契约: 规则集 + nonce 签发/校验 + 决策留痕。

    rules: [{"action": "do:*", "allow": True, "actor": "operator"|None}, ...]
    第一条命中的规则生效; 无规则命中 → 默认拒绝 (deny-by-default)。
    """

    def __init__(self, rules: list[dict] | None = None, *, issuer: str = "veya-loop"):
        self.rules = list(rules or [])
        self.issuer = issuer
        self._nonces: dict[str, dict[str, Any]] = {}
        self.decisions: list[PermissionDecision] = []

    # ── 规则 ─────────────────────────────────────────────────────────
    def grant(
        self,
        action: str,
        *,
        allow: bool = True,
        actor: str | None = None,
        resource: str | None = None,
    ) -> None:
        self.rules.append({"action": action, "allow": allow, "actor": actor, "resource": resource})

    # ── 评估 ─────────────────────────────────────────────────────────
    def evaluate(
        self, action: str, *, resource: str | None = None, actor: str | None = None
    ) -> PermissionDecision:
        """规则评估: 命中即决 (首条命中优先), 否则 deny-by-default。"""
        for rule in self.rules:
            if rule.get("actor") and actor and rule["actor"] != actor:
                continue
            if rule.get("resource") and (
                not resource or not fnmatch.fnmatch(resource, rule["resource"])
            ):
                continue
            if _rule_matches(str(rule["action"]), action):
                allowed = bool(rule.get("allow", True))
                return self._decide(
                    allowed, f"规则命中: {rule['action']}", action, resource or "", actor or ""
                )
        return self._decide(
            False, "无规则命中, deny-by-default", action, resource or "", actor or ""
        )

    # ── nonce (谁授权的可追溯凭证) ──────────────────────────────────
    def issue_nonce(self, action: str, *, resource: str = "", actor: str = "") -> str:
        """为已授权的干预签发 nonce (cap_ 前缀, 自描述 + 注册表)。"""
        digest = hashlib.sha256(f"{self.issuer}|{action}|{resource}|{actor}".encode()).hexdigest()[
            :12
        ]
        nonce = f"cap_{digest}_{uuid.uuid4().hex[:8]}"
        self._nonces[nonce] = {
            "action": action,
            "resource": resource,
            "actor": actor,
            "ts": time.time(),
            "used": False,
        }
        return nonce

    def verify_nonce(self, nonce: str) -> bool:
        """校验并单次消费 nonce (防重放)。"""
        entry = self._nonces.get(nonce)
        if entry is None or entry["used"]:
            return False
        entry["used"] = True
        return True

    def nonce_info(self, nonce: str) -> dict[str, Any] | None:
        return self._nonces.get(nonce)

    # ── 内部 ─────────────────────────────────────────────────────────
    def _decide(
        self, allowed: bool, reason: str, action: str, resource: str, actor: str
    ) -> PermissionDecision:
        d = PermissionDecision(
            allowed=allowed, reason=reason, action=action, resource=resource, actor=actor
        )
        self.decisions.append(d)
        return d

    def decision_log(self, limit: int = 50) -> list[dict[str, Any]]:
        return [d.as_dict() for d in self.decisions[-limit:]]


# =========================================================================
# dispatch_intervention — 干预派发 (最小闭环入口)
# =========================================================================


@dataclass
class DispatchResult:
    status: str  # approved_executed | approved_failed | denied
    action: str
    outcome: ExecOutcome | None = None
    nonce: str | None = None
    reason: str = ""
    audit_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "action": self.action,
            "nonce": self.nonce,
            "reason": self.reason,
            "audit_id": self.audit_id,
            "exit_code": self.outcome.exit_code if self.outcome else None,
            "output": self.outcome.summary() if self.outcome else None,
        }


def dispatch_intervention(
    action: str,
    argv: Sequence[str],
    *,
    contract: PermissionContract,
    executor: HardenedExecutor | None = None,
    emitter: Any | None = None,  # oprim.AuditEmitter (可选)
    resource: str = "",
    actor: str = "",
    notes: str = "",
    timeout_s: float | None = None,
) -> DispatchResult:
    """授权 → 硬化执行 → 审计落笔, 一条调用完成。

    emitter 传 AuditEmitter 时, 自动写:
      - decide:   {chosen_strategy: action, utilities: {action: 1.0}}
      - execute:  {primitive: action, status, capability_nonce}
    """
    decision = contract.evaluate(action, resource=resource, actor=actor)
    if not decision.allowed:
        if emitter is not None:
            emitter.decide(
                decision={"chosen_strategy": action, "denied": True, "reason": decision.reason},
                context={"notes": notes},
            )
        return DispatchResult("denied", action, reason=decision.reason)

    nonce = contract.issue_nonce(action, resource=resource, actor=actor)

    if emitter is not None:
        emitter.decide(
            decision={"chosen_strategy": action, "denied": False}, context={"notes": notes}
        )

    if executor is None:
        # 无执行器 → 仅授权派发 (调用方自行执行), 状态 approved
        if emitter is not None:
            emitter.execute(
                execution={"primitive": action, "status": "dispatched", "capability_nonce": nonce}
            )
        return DispatchResult("approved_dispatched", action, nonce=nonce)

    outcome = executor.execute(argv, timeout_s=timeout_s)
    if emitter is not None:
        audit_id = emitter.execute(
            execution={
                "primitive": action,
                "status": "ok" if outcome.ok else "failed",
                "capability_nonce": nonce,
                "exit_code": outcome.exit_code,
                "output": outcome.summary(200),
            },
            context={"notes": notes},
        )
    else:
        audit_id = None

    status = "approved_executed" if outcome.ok else "approved_failed"
    return DispatchResult(status, action, outcome=outcome, nonce=nonce, audit_id=audit_id)


__all__ = [
    "DispatchResult",
    "ExecOutcome",
    "HardenedExecutor",
    "PermissionContract",
    "PermissionDecision",
    "dispatch_intervention",
]
