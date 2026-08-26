"""Adapters from existing Veya executors to ``DelegateResult``."""

from __future__ import annotations

from typing import Any

from .models import ArtifactRef, DelegateRequest, DelegateResult


def _artifact_values(values: object, *, producer: str) -> list[ArtifactRef]:
    if not isinstance(values, list):
        return []
    return [ArtifactRef.from_value(value, producer=producer) for value in values]


def delegate_result_from_mapping(
    request: DelegateRequest,
    value: dict[str, Any],
    *,
    producer: str = "delegate",
) -> DelegateResult:
    return DelegateResult.from_mapping(
        request.delegate_id,
        value,
        child_trace_id=request.parent_trace_id,
        producer=producer,
    )


def delegate_result_from_leaf(request: DelegateRequest, leaf: Any) -> DelegateResult:
    """Convert the current GoalRun ``LeafResult`` without changing its API."""
    status = str(getattr(leaf, "status", "partial"))
    reason = getattr(leaf, "stop_reason", None) or (
        "completed" if status == "completed" else "exception"
    )
    return DelegateResult(
        delegate_id=request.delegate_id,
        status="complete" if status == "completed" else "failed",
        stop_reason=reason,
        summary=str(getattr(leaf, "summary", "") or ""),
        evidence=list(getattr(leaf, "evidence", []) or []),
        assertions=list(getattr(leaf, "assertions", []) or []),
        artifacts=_artifact_values(getattr(leaf, "artifacts", []), producer=request.delegate_id),
        completed_work=list(getattr(leaf, "completed_work", []) or []),
        unfinished_work=list(getattr(leaf, "unfinished_work", []) or []),
        child_trace_id=request.parent_trace_id,
        error_class=None if status == "completed" else "leaf_failed",
        error_message=getattr(leaf, "block_reason", None),
    )


def delegate_result_from_agent_loop(
    request: DelegateRequest,
    value: dict[str, Any],
) -> DelegateResult:
    return delegate_result_from_mapping(request, value, producer="agent_loop")


def delegate_result_from_hicode(
    request: DelegateRequest,
    value: dict[str, Any],
) -> DelegateResult:
    return delegate_result_from_mapping(request, value, producer="hicode")
