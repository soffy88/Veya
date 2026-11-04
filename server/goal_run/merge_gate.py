"""Merge gate: both records, same diff, or no merge.

The gate is a pure read over the canonical event log. A subject may
merge only when its LATEST review record and its LATEST checks record
both passed AND both describe the same diff hash. Anything else --
missing record, failed record, hash mismatch -- refuses, fail-closed.

The gate records nothing itself; callers record their verdict as a
``delivery.merge_gate`` event if they want the decision auditable.
"""

from __future__ import annotations

from typing import Any

REVIEW_COMPLETED_TOPIC = "review.completed"
CHECKS_COMPLETED_TOPIC = "checks.completed"


def _latest(events: Any, subject_id: str, topic: str) -> dict[str, Any] | None:
    found = events.read_all(task_id=subject_id, topics={topic})
    rows = [event for event in found if isinstance(event, dict)]
    return rows[-1] if rows else None


def merge_gate_passed(subject_id: str, *, events: Any) -> tuple[bool, str]:
    """Return (passed, reason). No record exists, or no pass, means refuse."""

    review = _latest(events, subject_id, REVIEW_COMPLETED_TOPIC)
    if review is None:
        return False, "no review record for subject"
    checks = _latest(events, subject_id, CHECKS_COMPLETED_TOPIC)
    if checks is None:
        return False, "no checks record for subject"
    review_payload = review.get("payload") or {}
    checks_payload = checks.get("payload") or {}
    if not review_payload.get("passed"):
        return False, "latest review did not pass"
    if not checks_payload.get("passed"):
        return False, "latest checks did not pass"
    review_hash = review_payload.get("diff_hash")
    checks_hash = checks_payload.get("diff_hash")
    if not review_hash or review_hash != checks_hash:
        return False, "review/checks diff hash mismatch"
    return True, f"review+checks passed on diff {str(review_hash)[:12]}"
