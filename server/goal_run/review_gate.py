"""Gated code review: severity, diff-change rounds, 3-round cap, incident.

This is the CI discipline's review step (event-hub design section 4).
It wraps the advisory dual-axis review and ADDS the hard rules; the
advisory path (``server/goal_run/runner.py``) is untouched.

Rules, all fail-closed:

* severity: every finding carries blocker/major/nit; nits never reach a
  report (a linter could catch them); only blocker/major are reported.
* pass: zero blockers AND both axes actually scanned. An unscanned axis
  (LLM down, unparseable output) is inconclusive, never a pass.
* rounds: the author must change the diff to earn a new round. A
  resubmission with an unchanged diff reuses the previous round's
  conclusion without calling the model again.
* cap: ``MAX_REVIEW_ROUNDS`` rounds. Past the cap there is no fourth
  review: an incident card goes to the owner and the subject waits.
* record: every round is a canonical ``review.completed`` event (the
  merge gate reads it). A capped subject gets ``review.capped``; it is
  not a pass and the gate will refuse it.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from server.goal_run.code_review import dual_axis_review

_log = logging.getLogger("veya.goal_run.review_gate")

MAX_REVIEW_ROUNDS = 3
REVIEW_COMPLETED_TOPIC = "review.completed"
REVIEW_CAPPED_TOPIC = "review.capped"


def diff_hash_of(diff_text: str) -> str:
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


def classify(axis: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Split one axis result into blocker/major/nit buckets (nits kept aside)."""

    buckets: dict[str, list[dict[str, Any]]] = {"blocker": [], "major": [], "nit": []}
    for finding in axis.get("findings") or []:
        if not isinstance(finding, dict):
            buckets["major"].append({"detail": str(finding), "severity": "major"})
            continue
        severity = finding.get("severity")
        buckets[severity if severity in buckets else "major"].append(finding)
    return buckets


def review_passed(dual: dict[str, Any]) -> tuple[bool, str]:
    """Pass = no blockers and both axes scanned. Anything else fails closed."""

    for axis_name in ("standards", "spec"):
        axis = dual.get(axis_name) or {}
        if axis.get("unscanned_reason"):
            return False, f"{axis_name} axis unscanned: {axis['unscanned_reason'][:200]}"
        if classify(axis)["blocker"]:
            return False, f"{axis_name} axis has blocker findings"
    return True, "no blockers, both axes scanned"


def _prior_rounds(events: Any, subject_id: str) -> list[dict[str, Any]]:
    found = events.read_all(task_id=subject_id, topics={REVIEW_COMPLETED_TOPIC})
    return [event for event in found if isinstance(event, dict)]


def _record(events: Any, topic: str, subject_id: str, payload: dict[str, Any]) -> None:
    try:
        events.append({"topic": topic, "task_id": subject_id, "actor": "system", "payload": payload})
    except Exception:
        _log.warning("review gate could not record %s", topic, exc_info=True)


def _default_incident(subject_id: str, payload: dict[str, Any]) -> None:
    from server.bot_team.attention import AttentionStore

    AttentionStore().create(
        question=(
            f"审查轮次已封顶：{subject_id} 在 {payload.get('rounds', MAX_REVIEW_ROUNDS)} 轮内"
            f"仍有 blocker，任务已挂起等拍板。"
        ),
        context=(
            f"subject={subject_id} diff_hash={payload.get('diff_hash', '')[:12]} "
            f"reason={payload.get('reason', '')}"
        ),
        kind="incident",
        tell="needed",
    )


async def run_gated_review(
    *,
    diff_text: str,
    task_instruction: str,
    acceptance: list[str],
    standards_doc: str,
    subject_id: str,
    events: Any,
    llm_call_fn: Callable[..., Awaitable[dict]] | None = None,
    max_rounds: int = MAX_REVIEW_ROUNDS,
    incident_fn: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one gated review round for a subject (diff). Never raises."""

    digest = diff_hash_of(diff_text)
    prior = _prior_rounds(events, subject_id)
    last = prior[-1] if prior else None
    if last is not None:
        last_payload = last.get("payload") or {}
        if last_payload.get("diff_hash") == digest:
            # Unchanged resubmission: reuse the previous conclusion, do
            # not spend another model call or count another round.
            return {
                "subject_id": subject_id,
                "diff_hash": digest,
                "round": int(last_payload.get("round", 1)),
                "reused": True,
                "capped": False,
                "passed": bool(last_payload.get("passed")),
                "blockers": list(last_payload.get("blockers") or []),
                "majors": list(last_payload.get("majors") or []),
                "reason": "unchanged diff: previous round reused",
            }
        round_no = int(last_payload.get("round", 0)) + 1
    else:
        round_no = 1
    if round_no > max_rounds:
        payload = {
            "subject_id": subject_id,
            "diff_hash": digest,
            "round": round_no,
            "rounds": max_rounds,
            "passed": False,
            "reason": f"review round cap reached ({max_rounds}) with blockers outstanding",
        }
        _record(events, REVIEW_CAPPED_TOPIC, subject_id, payload)
        try:
            (incident_fn or _default_incident)(subject_id, payload)
        except Exception:
            _log.warning("review-cap incident failed", exc_info=True)
        return {**payload, "reused": False, "capped": True, "blockers": [], "majors": []}
    kwargs: dict[str, Any] = {}
    if llm_call_fn is not None:
        kwargs["llm_call_fn"] = llm_call_fn
    dual = await dual_axis_review(
        diff_text=diff_text,
        task_instruction=task_instruction,
        acceptance=list(acceptance),
        standards_doc=standards_doc,
        severity=True,
        **kwargs,
    )
    standards_buckets = classify(dual.get("standards") or {})
    spec_buckets = classify(dual.get("spec") or {})
    blockers = standards_buckets["blocker"] + spec_buckets["blocker"]
    majors = standards_buckets["major"] + spec_buckets["major"]
    passed, reason = review_passed(dual)
    payload = {
        "subject_id": subject_id,
        "diff_hash": digest,
        "round": round_no,
        "passed": passed,
        "blockers": blockers,
        "majors": majors,
        "reason": reason,
    }
    _record(events, REVIEW_COMPLETED_TOPIC, subject_id, payload)
    return {
        **payload,
        "reused": False,
        "capped": False,
        "worst_standards": (dual.get("standards") or {}).get("worst"),
        "worst_spec": (dual.get("spec") or {}).get("worst"),
    }
