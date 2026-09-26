"""Admission Controller and Backpressure for Veya Agent Runtime V1.

Enforces:
- Global concurrency limits
- Principal-level concurrency limits
- Channel-level concurrency limits
- Mission-level concurrency limits
- Queue depth thresholds
- Safe admission policies: WAIT, REJECT, DEFER (default)

Invariants:
- Queue saturation does NOT fail executions.
- Waiting items consume zero execution attempts.
- Cancelled/timed-out waiters release capacity immediately.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from .models import AdmissionPolicy, AdmissionPolicyMode


class AdmissionDecision:
    ADMITTED = "ADMITTED"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"


class AdmissionController:
    """Manages resource concurrency and backpressure."""

    def __init__(self, policy: AdmissionPolicy | None = None) -> None:
        self.policy = policy or AdmissionPolicy()
        self._lock = asyncio.Lock()
        self._active_global: int = 0
        self._active_principals: dict[str, int] = defaultdict(int)
        self._active_channels: dict[str, int] = defaultdict(int)
        self._active_missions: dict[str, int] = defaultdict(int)
        self._queue_size: int = 0

    @property
    def active_global(self) -> int:
        return self._active_global

    @property
    def queue_size(self) -> int:
        return self._queue_size

    def check_capacity(
        self,
        principal_id: str,
        channel_id: str,
        mission_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Check if admission is currently permitted without modifying state."""
        if self._active_global >= self.policy.global_concurrency:
            return False, f"global concurrency limit ({self.policy.global_concurrency}) reached"

        if self._active_principals[principal_id] >= self.policy.principal_concurrency:
            return (
                False,
                f"principal concurrency limit ({self.policy.principal_concurrency}) reached",
            )

        if self._active_channels[channel_id] >= self.policy.channel_concurrency:
            return False, f"channel concurrency limit ({self.policy.channel_concurrency}) reached"

        if mission_id and self._active_missions[mission_id] >= self.policy.mission_concurrency:
            return False, f"mission concurrency limit ({self.policy.mission_concurrency}) reached"

        if self._queue_size >= self.policy.queue_depth:
            return False, f"queue depth limit ({self.policy.queue_depth}) reached"

        return True, None

    def acquire(
        self,
        principal_id: str,
        channel_id: str,
        mission_id: str | None = None,
    ) -> bool:
        """Attempt to synchronously reserve capacity slot."""
        allowed, _ = self.check_capacity(principal_id, channel_id, mission_id)
        if not allowed:
            return False

        self._active_global += 1
        self._active_principals[principal_id] += 1
        self._active_channels[channel_id] += 1
        if mission_id:
            self._active_missions[mission_id] += 1
        return True

    def release(
        self,
        principal_id: str,
        channel_id: str,
        mission_id: str | None = None,
    ) -> None:
        """Release reserved capacity slot."""
        if self._active_global > 0:
            self._active_global -= 1

        if self._active_principals[principal_id] > 0:
            self._active_principals[principal_id] -= 1
            if self._active_principals[principal_id] == 0:
                del self._active_principals[principal_id]

        if self._active_channels[channel_id] > 0:
            self._active_channels[channel_id] -= 1
            if self._active_channels[channel_id] == 0:
                del self._active_channels[channel_id]

        if mission_id and self._active_missions[mission_id] > 0:
            self._active_missions[mission_id] -= 1
            if self._active_missions[mission_id] == 0:
                del self._active_missions[mission_id]

    def increment_queue(self) -> None:
        self._queue_size += 1

    def decrement_queue(self) -> None:
        if self._queue_size > 0:
            self._queue_size -= 1

    def evaluate_admission(
        self,
        principal_id: str,
        channel_id: str,
        mission_id: str | None = None,
    ) -> tuple[str, str | None]:
        """Evaluate admission decision based on policy mode.

        Returns (decision, reason).
        """
        allowed, reason = self.check_capacity(principal_id, channel_id, mission_id)
        if allowed:
            self.acquire(principal_id, channel_id, mission_id)
            return AdmissionDecision.ADMITTED, None

        if self.policy.policy_mode == AdmissionPolicyMode.REJECT:
            return AdmissionDecision.REJECTED, reason

        # AdmissionPolicyMode.DEFER or WAIT
        return AdmissionDecision.DEFERRED, reason

    def get_stats(self) -> dict[str, Any]:
        return {
            "active_global": self._active_global,
            "queue_size": self._queue_size,
            "active_principals": dict(self._active_principals),
            "active_channels": dict(self._active_channels),
            "active_missions": dict(self._active_missions),
            "policy": self.policy.to_dict(),
        }
