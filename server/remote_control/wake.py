"""W13 wake policy — the receiver decides, per sender+agent.

The sender may *request* ``now`` / ``batch`` / ``none``. The receiver decides,
because message delivery never implies permission to wake or execute
(``W13_REMOTE_CONTROL_SPEC.md`` §6.3).

Decision order:

1. policy ceiling — the operator-set maximum for that sender+agent; a
   requested ``now`` is downgraded to ``batch`` when the ceiling is ``batch``.
2. quiet hours — ``veya.oskill.pure.wake_policy.is_quiet_hours``, the module the
   wake router already uses.
3. rate limit — wakes per sender+agent inside a sliding window.

Batched wakes are materialised as an existing ``RoutineSpec`` in the existing
``RoutineRegistry`` and driven by the existing ``RoutineClock``. W13 adds **no**
scheduler, no thread and no ``asyncio`` loop. Because the registry has no
``delete()``, removal is ``set_enabled(routine_id, False)`` — the same
workaround ``server/supervisor/handoff.py:113-117`` uses.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from server.remote_control import _ledger

_LEDGER_ENV = "VEYA_REMOTE_CONTROL_WAKE_PATH"
_LEDGER_FILE = "remote_control_wake.json"

#: What the sender may ask for.
REQUESTS: tuple[str, ...] = ("now", "batch", "none")
#: What the receiver may answer.
OUTCOMES: tuple[str, ...] = ("woken_now", "batched", "rejected")

#: Ceiling ranks — a request is downgraded to the ceiling, never upgraded.
_CEILING_RANK = {"none": 0, "batch": 1, "now": 2}

DEFAULT_RATE_LIMIT = 5
DEFAULT_RATE_WINDOW_S = 300.0
DEFAULT_BATCH_INTERVAL_MIN = 15


@dataclass
class WakePolicy:
    """Per sender+agent wake ceiling plus its rate-limit window."""

    sender: str
    agent_label: str
    ceiling: str = "now"
    rate_limit: int = DEFAULT_RATE_LIMIT
    rate_window_s: float = DEFAULT_RATE_WINDOW_S
    batch_interval_minutes: int = DEFAULT_BATCH_INTERVAL_MIN
    wake_times: list[float] = field(default_factory=list)
    routine_id: str = ""
    updated_at: float = field(default_factory=time.time)

    def key(self) -> str:
        return f"{self.sender}|{self.agent_label}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WakePolicy":
        return cls(
            sender=str(value.get("sender") or ""),
            agent_label=str(value.get("agent_label") or ""),
            ceiling=str(value.get("ceiling") or "now"),
            rate_limit=int(value.get("rate_limit") or DEFAULT_RATE_LIMIT),
            rate_window_s=float(value.get("rate_window_s") or DEFAULT_RATE_WINDOW_S),
            batch_interval_minutes=int(
                value.get("batch_interval_minutes") or DEFAULT_BATCH_INTERVAL_MIN
            ),
            wake_times=[float(t) for t in (value.get("wake_times") or [])],
            routine_id=str(value.get("routine_id") or ""),
            updated_at=float(value.get("updated_at") or time.time()),
        )


@dataclass
class WakeDecision:
    """The receiver's answer, with the reason a sender can act on."""

    outcome: str
    reason: str
    effective: str = ""
    routine_id: str = ""
    retry_after_s: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.outcome in {"woken_now", "batched"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _wake_path():
    return _ledger.default_path(_LEDGER_ENV, _LEDGER_FILE)


def get_policy(sender: str, agent_label: str) -> WakePolicy:
    """Read one policy, or a default when none was configured."""

    key = f"{sender}|{agent_label}"
    row = _ledger.read_map(_wake_path()).get(key)
    return WakePolicy.from_dict(row) if row else WakePolicy(sender=sender, agent_label=agent_label)


def set_policy(policy: WakePolicy) -> WakePolicy:
    """Persist one policy. Rejects an unknown ceiling."""

    if policy.ceiling not in _CEILING_RANK:
        raise ValueError(f"ceiling must be one of {sorted(_CEILING_RANK)}")
    if policy.rate_limit < 0:
        raise ValueError("rate_limit must be >= 0")
    policy.updated_at = time.time()

    def _mutate(rows: dict[str, dict[str, Any]]):
        rows[policy.key()] = policy.to_dict()
        return rows, policy

    return _ledger.update(_wake_path(), _mutate)


def list_policies() -> list[WakePolicy]:
    rows = _ledger.read_map(_wake_path())
    return sorted((WakePolicy.from_dict(row) for row in rows.values()), key=lambda p: p.key())


def delete_policy(sender: str, agent_label: str) -> bool:
    key = f"{sender}|{agent_label}"
    removed = False

    def _mutate(rows: dict[str, dict[str, Any]]):
        nonlocal removed
        removed = rows.pop(key, None) is not None
        return rows, None

    _ledger.update(_wake_path(), _mutate)
    return removed


def quiet_hours_active(now: float, settings: dict[str, Any] | None = None) -> bool:
    """Delegate to the existing pure policy. No W13-local hours logic."""

    from veya.oskill.pure.wake_policy import is_quiet_hours

    return is_quiet_hours(settings or {}, now=now)


def decide(
    *,
    sender: str,
    agent_label: str,
    requested: str,
    urgent: bool = False,
    quiet_settings: dict[str, Any] | None = None,
    now: float | None = None,
) -> tuple[WakeDecision, WakePolicy]:
    """Pure decision: what happens to this request, given current settings."""

    if requested not in REQUESTS:
        return (
            WakeDecision("rejected", f"unknown wake request: {requested!r}"),
            get_policy(sender, agent_label),
        )
    policy = get_policy(sender, agent_label)
    stamp = time.time() if now is None else float(now)

    if requested == "none":
        return WakeDecision("rejected", "sender requested no wake"), policy

    effective = requested
    if _CEILING_RANK[policy.ceiling] < _CEILING_RANK[requested]:
        effective = policy.ceiling
    if effective == "none":
        return (
            WakeDecision("rejected", "policy ceiling for this sender+agent is none"),
            policy,
        )

    # Quiet hours: `urgent` bypasses, matching veya.oskill.pure.wake_policy.
    if not urgent and quiet_hours_active(stamp, quiet_settings):
        if effective == "now":
            return (
                WakeDecision(
                    "batched", "quiet hours — request held for the next window"
                ),
                policy,
            )

    if policy.rate_limit >= 0:
        window_start = stamp - policy.rate_window_s
        recent = [t for t in policy.wake_times if t >= window_start]
        if len(recent) >= policy.rate_limit:
            retry = max(0.0, (min(recent) + policy.rate_window_s) - stamp)
            return (
                WakeDecision(
                    "rejected",
                    f"rate limit: {len(recent)} wakes in {policy.rate_window_s:.0f}s "
                    f"(limit {policy.rate_limit})",
                    retry_after_s=retry,
                ),
                policy,
            )

    if effective == "batch":
        return (
            WakeDecision(
                "batched",
                "policy ceiling is batch" if requested == "now" else "sender requested batch",
                effective="batch",
                routine_id=policy.routine_id,
            ),
            policy,
        )
    return (
        WakeDecision("woken_now", "waking now", effective="now"),
        policy,
    )


def record_wake(
    policy: WakePolicy, *, now: float | None = None, routine_id: str = ""
) -> WakePolicy:
    """Append a wake timestamp so the rate limit counts real wakes."""

    stamp = time.time() if now is None else float(now)
    policy.wake_times = [t for t in policy.wake_times if t >= stamp - policy.rate_window_s]
    policy.wake_times.append(stamp)
    if routine_id:
        policy.routine_id = routine_id
    return set_policy(policy)


def materialise_batch(
    policy: WakePolicy, *, bot_id: str, project_root: str = ""
) -> tuple[str, int]:
    """Register the batched wake as an existing routine; return (routine_id, minutes).

    Uses ``RoutineRegistry.register`` with ``trigger_topic="schedule.trigger"``,
    the same shape ``server/supervision_tools.py:222-233`` creates. The existing
    ``RoutineClock`` dispatches it; nothing here starts a timer.
    """

    from server.goal_run.models import RoutineSpec
    from server.goal_run.routine_registry import RoutineRegistry

    minutes = max(1, int(policy.batch_interval_minutes or DEFAULT_BATCH_INTERVAL_MIN))
    routine_id = policy.routine_id or f"remote-wake-{policy.sender}-{policy.agent_label}"
    spec = RoutineSpec(
        routine_id=routine_id,
        bot_id=bot_id,
        trigger_topic="schedule.trigger",
        schedule=f"every {minutes}m",
        goal_template={"kind": "remote_wake_batch", "sender": policy.sender,
                       "agent_label": policy.agent_label},
        probe={"kind": "none"},
        workspace_path=project_root,
        enabled=True,
    )
    registry = RoutineRegistry(bot_id=bot_id)
    if registry.get(routine_id) is None:
        registry.register(spec)
    else:
        registry.set_enabled(routine_id, True)
    policy.routine_id = routine_id
    set_policy(policy)
    return routine_id, minutes


def disable_batch(policy: WakePolicy) -> bool:
    """Stop a batched wake. The registry cannot delete, so disable it."""

    if not policy.routine_id:
        return False
    from server.goal_run.routine_registry import RoutineRegistry

    registry = RoutineRegistry(bot_id=policy.sender)
    stopped = registry.set_enabled(policy.routine_id, False)
    policy.routine_id = ""
    set_policy(policy)
    return stopped


__all__ = [
    "DEFAULT_BATCH_INTERVAL_MIN",
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_RATE_WINDOW_S",
    "OUTCOMES",
    "REQUESTS",
    "WakeDecision",
    "WakePolicy",
    "decide",
    "delete_policy",
    "disable_batch",
    "get_policy",
    "list_policies",
    "materialise_batch",
    "quiet_hours_active",
    "record_wake",
    "set_policy",
]
