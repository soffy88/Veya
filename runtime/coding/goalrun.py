"""GoalRun integration for coding tasks.

This module provides the bridge between CodingTask and GoalRun durable execution.
It creates GoalRun leaf tasks for coding operations and returns DelegateResult
compatible evidence.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from runtime.execution.models import (
    AcceptanceCriterion,
    AcceptanceResult,
    ArtifactRef,
    Assertion,
    DelegateResult,
    DelegateStatus,
    Evidence,
    StopReason,
)


@dataclass
class CodingLeafRequest:
    """Request to execute a coding leaf operation."""

    tool: str
    args: dict[str, Any]
    worktree_path: str
    task_id: str
    step_index: int = 0


@dataclass
class CodingLeafResult:
    """Result from a coding leaf operation."""

    status: Literal["ok", "partial", "failed"]
    summary: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    acceptance_results: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "completed"


def build_coding_delegate_result(
    task_id: str,
    leaf_results: list[CodingLeafResult],
    *,
    objective: str,
    worktree_path: str,
    acceptance_criteria: list[AcceptanceCriterion] | None = None,
) -> DelegateResult:
    """Build a DelegateResult from a sequence of coding leaf results.

    This preserves the Execution Runtime ABI while aggregating coding-specific
    evidence.
    """
    if not leaf_results:
        return DelegateResult(
            delegate_id=f"delegate-{task_id}",
            status="failed",
            summary="No coding operations executed",
            stop_reason="exception",
            evidence=[],
            artifacts=[],
            assertions=[],
            acceptance_results=[],
        )

    # Aggregate evidence and artifacts
    all_evidence: list[Evidence] = []
    all_artifacts: list[ArtifactRef] = []
    all_acceptance: list[AcceptanceResult] = []

    for i, leaf in enumerate(leaf_results):
        # Convert evidence dicts to Evidence objects
        for ev in leaf.evidence:
            all_evidence.append(
                Evidence(
                    id=ev.get("id", f"ev-{task_id}-{i}-{uuid.uuid4().hex[:8]}"),
                    kind=ev.get("kind", "observation"),
                    source=ev.get("source", "coding_leaf"),
                    content=json.dumps(ev, ensure_ascii=False) if isinstance(ev, dict) else str(ev),
                    producer=f"leaf-{i}",
                    confidence=ev.get("confidence"),
                )
            )

        # Convert artifact dicts to ArtifactRef
        for art in leaf.artifacts:
            path = art.get("path", "")
            if path:
                all_artifacts.append(ArtifactRef(path=path, kind="file", producer=f"leaf-{i}"))

    # Determine overall status
    has_failure = any(leaf.status == "failed" for leaf in leaf_results)
    all_ok = all(leaf.status == "ok" for leaf in leaf_results)

    if has_failure:
        status: DelegateStatus = "failed"
        stop_reason: StopReason = "exception"
    elif all_ok:
        status = "complete"
        stop_reason = "completed"
    else:
        status = "partial"
        stop_reason = "acceptance_failed"

    # Evaluate acceptance criteria
    if acceptance_criteria:
        for criterion in acceptance_criteria:
            # Check if any leaf result satisfies this criterion
            satisfied = False
            for leaf in leaf_results:
                for ar in leaf.acceptance_results:
                    if ar.get("criterion_id") == criterion.id and ar.get("status") == "passed":
                        satisfied = True
                        break
                if satisfied:
                    break

            all_acceptance.append(
                AcceptanceResult(
                    id=criterion.id,
                    status="passed" if satisfied else "failed",
                    summary=criterion.description,
                    required=criterion.required,
                )
            )
            if criterion.required and not satisfied and status == "complete":
                status = "partial"
                stop_reason = "acceptance_failed"

    # Build summary
    summary_parts = [f"Coding task: {objective[:200]}"]
    for i, leaf in enumerate(leaf_results):
        summary_parts.append(f"  Step {i}: {leaf.status}")
    summary = "\n".join(summary_parts)

    return DelegateResult(
        delegate_id=f"delegate-{task_id}",
        status=status,
        summary=summary,
        stop_reason=stop_reason,
        evidence=all_evidence,
        artifacts=all_artifacts,
        assertions=[],
        acceptance_results=all_acceptance,
    )


def persist_delegate_result(
    project_root: Path,
    task_id: str,
    result: DelegateResult,
) -> Path:
    """Persist DelegateResult to .veya/runs/<task_id>/outputs/delegate_result.json"""
    output_dir = project_root / ".veya" / "runs" / task_id / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "delegate_result.json"

    data = {
        "delegate_id": result.delegate_id,
        "status": result.status,
        "summary": result.summary,
        "stop_reason": result.stop_reason,
        "evidence": [e.to_dict() for e in result.evidence],
        "artifacts": [a.to_dict() for a in result.artifacts],
        "assertions": [a.to_dict() for a in result.assertions],
        "acceptance_results": [a.to_dict() for a in result.acceptance_results],
        "completed_work": result.completed_work,
        "unfinished_work": result.unfinished_work,
        "persisted_at": datetime.now(UTC).isoformat(),
    }

    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def load_delegate_result(
    project_root: Path,
    task_id: str,
) -> DelegateResult | None:
    """Load DelegateResult from .veya/runs/<task_id>/outputs/delegate_result.json"""
    path = project_root / ".veya" / "runs" / task_id / "outputs" / "delegate_result.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return DelegateResult(
            delegate_id=data["delegate_id"],
            status=data["status"],
            summary=data["summary"],
            stop_reason=data["stop_reason"],
            evidence=[Evidence(**e) for e in data.get("evidence", [])],
            artifacts=[ArtifactRef(**a) for a in data.get("artifacts", [])],
            assertions=[Assertion(**a) for a in data.get("assertions", [])],
            acceptance_results=[AcceptanceResult(**a) for a in data.get("acceptance_results", [])],
            completed_work=data.get("completed_work", []),
            unfinished_work=data.get("unfinished_work", []),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


__all__ = [
    "CodingLeafRequest",
    "CodingLeafResult",
    "build_coding_delegate_result",
    "load_delegate_result",
    "persist_delegate_result",
]
