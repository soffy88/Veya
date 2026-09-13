"""Production controls for the existing durable execution authority.

This is a control plane, not an executor or scheduler.  It admits work,
records lifecycle/budget facts, and fences an existing GoalRun.  Physical
execution remains owned by GoalRun and its ActionGateway/SideEffectLedger.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class AdmissionRejected(RuntimeError):
    """Work cannot be admitted without violating a hard capacity limit."""


class BudgetExhausted(RuntimeError):
    """A GoalRun is blocked because a hard resource budget was exhausted."""


class GoalRunNotRunnable(RuntimeError):
    """A paused, cancelled, or stopped GoalRun cannot start another action."""


@dataclass(frozen=True)
class HardeningLimits:
    max_active_user: int = 4
    max_active_bot: int = 2
    max_active_goal_run: int = 1
    max_queue: int = 128
    max_tokens: int = 300_000
    max_tool_calls: int = 1_000
    max_provider_calls: int = 1_000
    max_wall_s: float = 7_200.0


@dataclass
class AdmissionLease:
    lease_id: str
    user_id: str
    bot_id: str
    goal_run_id: str
    status: str = "queued"
    admitted_at: float = field(default_factory=time.time)


@dataclass
class BudgetAccount:
    tokens: int = 0
    tool_calls: int = 0
    provider_calls: int = 0
    started_at: float = field(default_factory=time.time)
    blocked: bool = False
    reason: str | None = None


class ProductionControlPlane:
    """Admission, lifecycle and accounting around one existing execution plane."""

    def __init__(
        self,
        limits: HardeningLimits | None = None,
        *,
        state_path: str | Path | None = None,
        audit_writer: Callable[[dict[str, Any]], Any] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.limits = limits or HardeningLimits()
        self.state_path = Path(state_path).expanduser() if state_path else None
        self._audit_writer = audit_writer
        self._clock = clock or time.monotonic
        self._leases: dict[str, AdmissionLease] = {}
        self._queue: deque[str] = deque()
        self._accounts: dict[tuple[str, str], BudgetAccount] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._failures: dict[str, list[dict[str, Any]]] = {}
        self._paused: set[str] = set()
        self._draining = False
        self._emergency_stopped = False
        self._lock = asyncio.Lock()
        self.audit_records: list[dict[str, Any]] = []

    def _record(
        self,
        *,
        lease: AdmissionLease | None,
        actor: str,
        action: str,
        provider: str | None = None,
        side_effect: str | None = None,
        approval: str | None = None,
        verdict: str | None = None,
        restart: str | None = None,
        cancel: str | None = None,
        **extra: Any,
    ) -> None:
        event = {
            "ts": time.time(),
            "bot_id": lease.bot_id if lease else extra.get("bot_id"),
            "goal_run_id": lease.goal_run_id if lease else extra.get("goal_run_id"),
            "actor": actor,
            "action": action,
            "provider": provider,
            "side_effect": side_effect,
            "approval": approval,
            "verdict": verdict,
            "restart": restart,
            "cancel": cancel,
            **extra,
        }
        self.audit_records.append(event)
        if self._audit_writer is not None:
            self._audit_writer(event)

    def _active(self) -> list[AdmissionLease]:
        return [lease for lease in self._leases.values() if lease.status == "active"]

    def _can_activate(self, lease: AdmissionLease) -> bool:
        active = self._active()
        return (
            sum(item.user_id == lease.user_id for item in active) < self.limits.max_active_user
            and sum(item.bot_id == lease.bot_id for item in active) < self.limits.max_active_bot
            and sum(item.goal_run_id == lease.goal_run_id for item in active)
            < self.limits.max_active_goal_run
        )

    def _promote(self) -> None:
        if self._draining or self._emergency_stopped:
            return
        for lease_id in list(self._queue):
            lease = self._leases[lease_id]
            if not self._can_activate(lease):
                continue
            self._queue.remove(lease_id)
            lease.status = "active"
            self._cancel_events.setdefault(lease.goal_run_id, asyncio.Event())
            self._record(lease=lease, actor="control-plane", action="admitted")

    async def admit(self, user_id: str, bot_id: str, goal_run_id: str) -> AdmissionLease:
        async with self._lock:
            if self._draining or self._emergency_stopped:
                raise AdmissionRejected("control plane is closed")
            if len(self._queue) >= self.limits.max_queue:
                self._record(
                    lease=None,
                    actor="control-plane",
                    action="admission_rejected",
                    bot_id=bot_id,
                    goal_run_id=goal_run_id,
                    reason="overload",
                )
                raise AdmissionRejected("admission queue is full")
            lease = AdmissionLease(
                f"lease:{goal_run_id}:{len(self._leases)}", user_id, bot_id, goal_run_id
            )
            self._leases[lease.lease_id] = lease
            self._queue.append(lease.lease_id)
            self._accounts.setdefault((bot_id, goal_run_id), BudgetAccount(started_at=time.time()))
            self._promote()
            self._persist()
            return lease

    async def release(self, lease_id: str) -> None:
        async with self._lock:
            lease = self._leases[lease_id]
            lease.status = "released"
            self._queue = deque(item for item in self._queue if item != lease_id)
            self._record(lease=lease, actor="control-plane", action="released")
            self._promote()
            self._persist()

    async def pause(self, goal_run_id: str, *, actor: str = "operator") -> None:
        async with self._lock:
            self._paused.add(goal_run_id)
            self._record_for_goal(goal_run_id, actor, "paused")
            self._persist()

    async def resume(self, goal_run_id: str, *, actor: str = "operator") -> None:
        async with self._lock:
            self._paused.discard(goal_run_id)
            self._record_for_goal(goal_run_id, actor, "resumed")
            self._persist()

    async def cancel(self, goal_run_id: str, *, actor: str = "operator") -> None:
        async with self._lock:
            event = self._cancel_events.setdefault(goal_run_id, asyncio.Event())
            event.set()
            for lease in self._leases.values():
                if lease.goal_run_id == goal_run_id and lease.status in {"queued", "active"}:
                    lease.status = "cancelled"
            self._queue = deque(
                item for item in self._queue if self._leases[item].status == "queued"
            )
            self._record_for_goal(goal_run_id, actor, "cancelled", cancel="propagated")
            self._promote()
            self._persist()

    async def emergency_stop(self, *, actor: str = "operator") -> None:
        async with self._lock:
            self._emergency_stopped = True
            for event in self._cancel_events.values():
                event.set()
            for lease in self._leases.values():
                if lease.status in {"queued", "active"}:
                    lease.status = "cancelled"
            self._queue.clear()
            self._record(lease=None, actor=actor, action="emergency_stop", cancel="all")
            self._persist()

    async def drain(self, timeout_s: float = 30.0, *, actor: str = "operator") -> bool:
        async with self._lock:
            self._draining = True
            self._record(lease=None, actor=actor, action="drain_started")
            self._persist()
        deadline = self._clock() + timeout_s
        while self._active() and self._clock() < deadline:
            await asyncio.sleep(0.01)
        drained = not self._active()
        self._record(lease=None, actor=actor, action="drain_completed", drained=drained)
        return drained

    def cancellation_event(self, goal_run_id: str) -> asyncio.Event:
        return self._cancel_events.setdefault(goal_run_id, asyncio.Event())

    def is_paused(self, goal_run_id: str) -> bool:
        return goal_run_id in self._paused

    def ensure_runnable(self, goal_run_id: str) -> None:
        if self._emergency_stopped:
            raise GoalRunNotRunnable("emergency stop is active")
        if self._draining:
            raise GoalRunNotRunnable("control plane is draining")
        if goal_run_id in self._paused:
            raise GoalRunNotRunnable("goal run is paused")
        event = self._cancel_events.get(goal_run_id)
        if event is not None and event.is_set():
            raise GoalRunNotRunnable("goal run is cancelled")

    def charge(
        self,
        bot_id: str,
        goal_run_id: str,
        *,
        tokens: int = 0,
        tool_calls: int = 0,
        provider_calls: int = 0,
    ) -> None:
        account = self._accounts.setdefault(
            (bot_id, goal_run_id), BudgetAccount(started_at=time.time())
        )
        elapsed = time.time() - account.started_at
        if account.blocked:
            raise BudgetExhausted(account.reason or "budget exhausted")
        if account.tokens + tokens > self.limits.max_tokens:
            return self._block_budget(account, bot_id, goal_run_id, "token_budget")
        if account.tool_calls + tool_calls > self.limits.max_tool_calls:
            return self._block_budget(account, bot_id, goal_run_id, "tool_budget")
        if account.provider_calls + provider_calls > self.limits.max_provider_calls:
            return self._block_budget(account, bot_id, goal_run_id, "provider_budget")
        if elapsed > self.limits.max_wall_s:
            return self._block_budget(account, bot_id, goal_run_id, "time_budget")
        account.tokens += max(0, tokens)
        account.tool_calls += max(0, tool_calls)
        account.provider_calls += max(0, provider_calls)
        self._persist()

    def _block_budget(
        self, account: BudgetAccount, bot_id: str, goal_run_id: str, reason: str
    ) -> None:
        account.blocked = True
        account.reason = reason
        self._record(
            lease=None,
            actor="control-plane",
            action="budget_exhausted",
            bot_id=bot_id,
            goal_run_id=goal_run_id,
            reason=reason,
        )
        self._persist()
        raise BudgetExhausted(reason)

    def record_failure(self, bot_id: str, goal_run_id: str, error: str) -> None:
        self._failures.setdefault(bot_id, []).append({"goal_run_id": goal_run_id, "error": error})
        self._record(
            lease=None,
            actor="execution",
            action="failure",
            bot_id=bot_id,
            goal_run_id=goal_run_id,
            error=error,
        )
        self._persist()

    def record_execution(
        self,
        bot_id: str,
        goal_run_id: str,
        *,
        actor: str,
        action: str,
        provider: str | None = None,
        side_effect: str | None = None,
        approval: str | None = None,
        verdict: str | None = None,
        restart: str | None = None,
        cancel: str | None = None,
    ) -> None:
        """Append an execution fact to the existing control-plane audit trail."""
        self._record(
            lease=None,
            actor=actor,
            action=action,
            bot_id=bot_id,
            goal_run_id=goal_run_id,
            provider=provider,
            side_effect=side_effect,
            approval=approval,
            verdict=verdict,
            restart=restart,
            cancel=cancel,
        )
        self._persist()

    def failures_for(self, bot_id: str) -> list[dict[str, Any]]:
        return list(self._failures.get(bot_id, []))

    def _record_for_goal(self, goal_run_id: str, actor: str, action: str, **fields: Any) -> None:
        lease = next(
            (item for item in self._leases.values() if item.goal_run_id == goal_run_id), None
        )
        self._record(lease=lease, actor=actor, action=action, **fields)

    def _persist(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "leases": [asdict(item) for item in self._leases.values()],
            "queue": list(self._queue),
            "accounts": {
                f"{bot}\n{goal}": asdict(account) for (bot, goal), account in self._accounts.items()
            },
            "paused": sorted(self._paused),
            "draining": self._draining,
            "emergency_stopped": self._emergency_stopped,
        }
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        temporary.replace(self.state_path)

    @classmethod
    def recover(cls, state_path: str | Path, **kwargs: Any) -> ProductionControlPlane:
        control = cls(state_path=state_path, **kwargs)
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
        control._leases = {
            item["lease_id"]: AdmissionLease(**item) for item in data.get("leases", [])
        }
        control._queue = deque(data.get("queue", []))
        control._accounts = {
            tuple(key.split("\n", 1)): BudgetAccount(**value)
            for key, value in data.get("accounts", {}).items()
        }
        control._paused = set(data.get("paused", []))
        control._draining = bool(data.get("draining", False))
        control._emergency_stopped = bool(data.get("emergency_stopped", False))
        control._cancel_events = {
            lease.goal_run_id: asyncio.Event()
            for lease in control._leases.values()
            if lease.status == "active"
        }
        return control


__all__ = [
    "AdmissionLease",
    "AdmissionRejected",
    "BudgetAccount",
    "BudgetExhausted",
    "GoalRunNotRunnable",
    "HardeningLimits",
    "ProductionControlPlane",
]
