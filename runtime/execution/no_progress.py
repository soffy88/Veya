"""Role-specific no-progress detection."""

from __future__ import annotations

import hashlib
import json


class NoProgressGuard:
    def __init__(self, role: str, *, threshold: int = 3):
        if threshold < 1:
            raise ValueError("threshold must be positive")
        self.role = role
        self.threshold = threshold
        self._last_signature: str | None = None
        self._same_count = 0

    def observe(
        self,
        *,
        signature: object,
        new_evidence: int = 0,
        new_artifacts: int = 0,
        state_changed: bool = False,
    ) -> bool:
        if new_evidence or new_artifacts or state_changed:
            self.reset()
            return False
        digest = hashlib.sha256(
            json.dumps(signature, sort_keys=True, default=str).encode()
        ).hexdigest()
        if digest == self._last_signature:
            self._same_count += 1
        else:
            self._last_signature = digest
            self._same_count = 1
        return self._same_count >= self.threshold

    def reset(self) -> None:
        self._last_signature = None
        self._same_count = 0
