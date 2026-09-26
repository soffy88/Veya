"""Human Escalation and Interrupt Handling (spec §20, §21).

Invariants:
- Escalations routed through existing JEV and Channel systems; no new message bus.
- Interrupts classified semantically (CLARIFICATION, OBJECTIVE_CHANGE, etc.).
- Objective changes explicitly produce a MissionRevision; silent mutations prohibited.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from .models import (
    EscalationReason,
    EscalationRequest,
    InterruptCategory,
)


class EscalationManager:
    """Manages human escalation requests via canonical JEV/Channel paths (spec §20)."""

    def __init__(self, persistence_path: str | Path | None = None, jev_client: Any = None):
        self._path = Path(persistence_path) if persistence_path else None
        self._jev = jev_client
        self._requests: dict[str, EscalationRequest] = {}
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        if not self._path or not self._path.is_file():
            return
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    req = EscalationRequest.from_dict(data)
                    self._requests[req.escalation_id] = req
                except Exception:
                    continue

    def _persist(self, req: EscalationRequest) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(req.to_dict(), ensure_ascii=False) + "\n")

    def escalate(
        self,
        mission_id: str,
        decision_id: str,
        reason: EscalationReason,
        question: str,
        context_summary: str = "",
        evidence_refs: list[str] | None = None,
        allowed_responses: list[str] | None = None,
    ) -> EscalationRequest:
        req = EscalationRequest(
            mission_id=mission_id,
            decision_id=decision_id,
            reason=reason,
            question=question,
            context_summary=context_summary,
            evidence_refs=list(evidence_refs or []),
            allowed_responses=list(allowed_responses or ["APPROVED", "DENIED", "REVISE"]),
            created_at=time.time(),
            status="PENDING",
        )
        self._requests[req.escalation_id] = req
        self._persist(req)

        # Emit to JEV if available
        if self._jev and hasattr(self._jev, "emit"):
            with contextlib.suppress(Exception):
                self._jev.emit(
                    topic="HUMAN_ESCALATION_REQUESTED",
                    payload=req.to_dict(),
                )

        return req

    def resolve(self, escalation_id: str, reply: str) -> EscalationRequest | None:
        req = self._requests.get(escalation_id)
        if not req:
            return None
        req.status = "RESOLVED"
        req.reply = reply
        req.replied_at = time.time()
        self._persist(req)
        return req

    def resolve_escalation(self, escalation_id: str, reply: str) -> bool:
        return self.resolve(escalation_id, reply) is not None

    def get(self, escalation_id: str) -> EscalationRequest | None:
        return self._requests.get(escalation_id)

    def get_escalation(self, escalation_id: str) -> EscalationRequest | None:
        return self.get(escalation_id)

    def list_for_mission(self, mission_id: str) -> list[EscalationRequest]:
        return [r for r in self._requests.values() if r.mission_id == mission_id]

    def query(self, mission_id: str) -> list[EscalationRequest]:
        return self.list_for_mission(mission_id)


class InterruptHandler:
    """Classifies interrupts and handles objective updates (spec §21)."""

    def classify(self, message: str) -> InterruptCategory:
        msg = message.strip().lower()
        if any(w in msg for w in ["cancel", "stop", "abort", "terminate"]):
            return InterruptCategory.CANCEL
        if any(
            w in msg
            for w in ["change goal", "new goal", "instead", "new objective", "change objective"]
        ):
            return InterruptCategory.OBJECTIVE_CHANGE
        if any(w in msg for w in ["do not", "must not", "only in", "require", "constraint"]):
            return InterruptCategory.CONSTRAINT_CHANGE
        if any(w in msg for w in ["priority", "urgent", "asap"]):
            return InterruptCategory.PRIORITY_CHANGE
        if any(w in msg for w in ["clarify", "mean", "explain"]):
            return InterruptCategory.CLARIFICATION
        return InterruptCategory.ADDITIONAL_CONTEXT

    def classify_and_handle(
        self,
        mission_id: str,
        content: str,
        sender: str = "",
    ) -> tuple[InterruptCategory, str | None]:
        cat = self.classify(content)
        new_obj: str | None = None
        if cat == InterruptCategory.OBJECTIVE_CHANGE:
            # Clean up new objective string
            new_obj = content
            for prefix in ["change goal to", "new goal:", "change objective to", "new objective:"]:
                if prefix in content.lower():
                    idx = content.lower().find(prefix) + len(prefix)
                    new_obj = content[idx:].strip()
                    break
        return cat, new_obj
