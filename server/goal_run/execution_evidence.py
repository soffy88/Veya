"""Shared durable-execution evidence helpers.

C3 Step 2 extracts the receipt finalization that previously lived only in
``server/bot_runtime/execution.py`` so the canonical execution plane and the
Bot execution chain assemble completion receipts through **one** authority.

This module owns no new storage and no new status: it reads the authoritative
binding record and persists an :class:`ExecutionReceipt` through the existing
``record_execution_receipt`` seam.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.goal_run.execution_admission import (
    ExecutionReceipt,
    _admission_dir,
    record_execution_receipt,
)
from server.goal_run.models import GoalStatus

__all__ = [
    "finalize_execution_receipt",
    "latest_receipt",
    "load_binding_record",
    "load_durable_receipts",
    "persist_harness_task_evidence",
    "read_execution_stdout",
]


def persist_harness_task_evidence(
    *,
    project_root: str,
    goal_run_id: str,
    task_id: str,
    objective: str,
    leaf_result: Any,
) -> list[str]:
    """Persist the real task evidence produced by one harness leaf execution.

    Mirrors the authoritative GoalRun projection (``_process_one_task``) using
    the existing ``delegate_result_from_leaf`` authority and ``save_goal_run``
    store. It does NOT set task status, verification or completion -- those stay
    with the runner. Returns the persisted artifact set so the caller can build
    an evidence reference that resolves against the authoritative record.
    """
    from runtime.execution.adapters import delegate_result_from_leaf
    from runtime.execution.models import DelegateRequest
    from server.goal_run.store import load_goal_run, save_goal_run

    state = load_goal_run(project_root, goal_run_id)
    if state is None:
        raise ValueError(f"GoalRun {goal_run_id!r} not found")
    task = state.tasks.get(task_id)
    if task is None:
        raise ValueError(f"task {task_id!r} not found in GoalRun {goal_run_id!r}")

    for item in leaf_result.artifacts or []:
        path = str(
            getattr(item, "path", None) or (item.get("path") if isinstance(item, dict) else item)
        )
        if path not in task.artifacts:
            task.artifacts.append(path)
    for item in leaf_result.evidence or []:
        task.evidence.append(item.to_dict() if hasattr(item, "to_dict") else dict(item))
    if leaf_result.summary:
        task.execute_result = leaf_result.summary

    delegate_request = DelegateRequest(
        delegate_id=task_id,
        parent_task_id=goal_run_id,
        parent_trace_id=goal_run_id,
        objective=objective,
        acceptance=list(task.acceptance),
        depth=1,
        estimated_tokens=0,
        timeout_s=int(state.budget.get("subagent_timeout_s", 5400)),
        workspace=project_root,
        output_paths=list(task.artifacts),
    )
    task.delegate_result = delegate_result_from_leaf(delegate_request, leaf_result).to_dict()
    save_goal_run(state, project_root)
    return list(task.artifacts)


def latest_receipt(
    project_root: str,
    goal_run_id: str,
    *,
    binding_id: str = "",
    execution_id: str = "",
) -> dict[str, Any] | None:
    """The most recent durable receipt tied to this binding and execution."""
    receipts = load_durable_receipts(project_root, goal_run_id)
    for record in reversed(receipts):
        if binding_id and str(record.get("binding_id") or "") != binding_id:
            continue
        if execution_id and str(record.get("provider_run_id") or "") == execution_id:
            return record
    for record in reversed(receipts):
        if not binding_id or str(record.get("binding_id") or "") == binding_id:
            return record
    return None


_RECEIPTS_FILE = "execution_receipts.jsonl"


def load_binding_record(project_root: str, binding_id: str) -> dict[str, Any] | None:
    """Read the authoritative persisted binding record (read-only)."""
    if not binding_id:
        return None
    path = _admission_dir(project_root) / f"binding_{binding_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_durable_receipts(project_root: str, goal_run_id: str) -> list[dict[str, Any]]:
    """Read the append-only durable receipts for a GoalRun (read-only)."""
    path = Path(project_root) / ".veya-project" / "goal-runs" / goal_run_id / _RECEIPTS_FILE
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def finalize_execution_receipt(
    *,
    project_root: str,
    goal_run_id: str,
    execution_id: str,
    admitted_executor: str,
    substrate_id: str,
    final_state: str,
) -> dict[str, Any]:
    """Append the durable completion receipt for this execution.

    ``executor_id`` is the admitted executor the admission/binding were granted
    for; ``substrate_id`` is the physical substrate the work actually ran on.
    Both are read from the authoritative binding record.
    """
    from server.goal_run.store import load_goal_run

    state = load_goal_run(project_root, goal_run_id)
    checkpoint = (state.runtime_checkpoint or {}) if state else {}

    binding_id = str(checkpoint.get("binding_id") or "")
    binding_record = load_binding_record(project_root, binding_id) or {}

    receipt = ExecutionReceipt(
        goal_run_id=goal_run_id,
        admission_id=str(checkpoint.get("admission_id") or ""),
        binding_id=binding_id,
        executor_id=str(binding_record.get("executor_id") or admitted_executor),
        substrate_id=str(binding_record.get("substrate_id") or substrate_id),
        executor_kind=str(binding_record.get("executor_kind") or ""),
        substrate_kind=str(binding_record.get("substrate_kind") or ""),
        installation_id="default",
        provider_session_id=binding_id,
        provider_run_id=execution_id,
        workspace_id=project_root,
        permission_decision=str(checkpoint.get("permission_decision") or "admitted"),
        resource_leases=[],
        start_time=str(checkpoint.get("start_time") or datetime.now(UTC).isoformat()),
        end_time=datetime.now(UTC).isoformat(),
        final_state=final_state,
        verification_result="verified" if final_state == GoalStatus.completed.value else None,
    )
    record_execution_receipt(project_root, receipt)
    return receipt.to_dict()


def read_execution_stdout(project_root: str, goal_run_id: str) -> str:
    """Reassemble real executor stdout from the durable activity stream."""
    from server.goal_run.execution_activity import ActivityRecorder

    activities, _cursor = ActivityRecorder(project_root).stream(goal_run_id, cursor=0)
    chunks = [
        a.payload.get("text") or a.payload.get("tail") or ""
        for a in activities
        if a.type == "OUTPUT"
    ]
    return "".join(chunks).strip()
