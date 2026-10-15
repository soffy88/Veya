"""TDX server routes and owner-only dependency for Veya."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from server import auth as auth_mod
from server.routes.tailnet import require_tailnet_owner as require_owner
from server.feature_flags import enabled as flag_enabled

router = APIRouter(prefix="/api/v1/tdx", tags=["tdx"])
logger = logging.getLogger(__name__)


@router.get("/needs-you")
def get_needs_you(user: dict[str, Any] = Depends(require_owner)):
    """Project real pending approvals and blocked task events into one owner queue."""
    if not flag_enabled("VEYA_TDX_NEEDS_YOU"):
        raise HTTPException(status_code=404, detail="VEYA_TDX_NEEDS_YOU disabled")

    from server import approvals as approval_mod
    from server import events as event_mod

    items: list[dict[str, Any]] = []
    try:
        scope = approval_mod._scope_of(user)
        pending = approval_mod.get_approval_service().store.list_approvals(
            scope=scope, status="pending"
        )
    except Exception as exc:
        logger.exception("TDX Needs You could not read the canonical approval store")
        raise HTTPException(status_code=503, detail="Approval store unavailable") from exc

    for approval in pending:
        request_id = str(approval.get("request_id") or "")
        if not request_id:
            continue
        created_at = approval.get("created_at")
        tool = approval.get("tool")
        title = f"Approval required: {tool}" if tool else "Approval required"
        item: dict[str, Any] = {
            "id": f"approval:{request_id}",
            "request_id": request_id,
            "kind": "approval",
            "title": title,
            "source": "approvals",
            "created_at": created_at,
            "link": "/approvals",
            "actions": ["once", "session", "always", "deny"],
        }
        if isinstance(created_at, (int, float)):
            item["age_s"] = max(0, int(time.time() - float(created_at)))
        if tool:
            item["tool"] = tool
        if approval.get("tool_args") is not None:
            item["tool_args"] = approval.get("tool_args")
        items.append(item)

    try:
        blocked_events = event_mod.event_store.read_all(
            topic_prefixes=("task.", "goal_run.", "work_item.", "supervision.", "review.")
        )
    except Exception as exc:
        logger.exception("TDX Needs You could not read the canonical event store")
        raise HTTPException(status_code=503, detail="Execution event store unavailable") from exc

    latest_by_task: dict[str, dict[str, Any]] = {}
    for event in blocked_events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        task_id = (
            event.get("task_id")
            or payload.get("task_id")
            or payload.get("goal_run_id")
            or payload.get("run_id")
            or payload.get("work_item_id")
            or payload.get("supervision_id")
            or payload.get("mission_id")
        )
        if task_id is None or not str(task_id).strip():
            continue
        task_id = str(task_id)
        topic = str(event.get("topic") or event.get("type") or "").lower()
        status_value = str(payload.get("status") or payload.get("state") or "").lower()
        for terminal in ("blocked", "needs_input", "needs_owner", "waiting_for_input", "review_rejected", "reviewer_rejected", "needs_review", "running", "completed", "complete", "failed", "cancelled", "canceled"):
            if topic.endswith("." + terminal):
                status_value = terminal
                break
        ts = event.get("ts")
        previous = latest_by_task.get(task_id)
        previous_ts = previous.get("ts") if previous else None
        if previous is None or (isinstance(ts, (int, float)) and (not isinstance(previous_ts, (int, float)) or ts >= previous_ts)):
            latest_by_task[task_id] = {
                "task_id": task_id,
                "topic": topic,
                "status": status_value,
                "payload": payload,
                "ts": ts,
            }

    attention_statuses = {"blocked", "needs_input", "needs_owner", "waiting_for_input", "review_rejected", "reviewer_rejected", "needs_review"}
    for task in latest_by_task.values():
        payload = task["payload"]
        if task["status"] not in attention_statuses and payload.get("needs_input") is not True:
            continue
        is_rejected = task["status"] in {"review_rejected", "reviewer_rejected"}
        item = {
            "id": f"task:{task['task_id']}",
            "task_id": task["task_id"],
            "kind": "review_rejected" if is_rejected else "blocked",
            "title": str(payload.get("block_reason") or payload.get("message") or payload.get("summary") or "Task is blocked"),
            "source": task["topic"] or "canonical_events",
            "created_at": task["ts"],
            "link": "/team",
            "actions": [],
        }
        if isinstance(task["ts"], (int, float)):
            item["age_s"] = max(0, int(time.time() - float(task["ts"])))
        items.append(item)

    items.sort(key=lambda item: item.get("created_at") if isinstance(item.get("created_at"), (int, float)) else 0, reverse=True)
    return {"enabled": True, "count": len(items), "items": items}


# Feature 2: Publication permissions are persisted and enforced by the existing ToolGuard.
from veya.tdx.publish_permissions import (
    check_publish_permission,
    load_permissions,
    save_permissions,
    tool_guard_policy,
)
from server.tool_guard import global_tool_guard


class PublishPermUpdate(BaseModel):
    push: bool = False
    merge: bool = False
    tag: bool = False
    deploy: bool = False


@router.get("/publish-perms")
def get_publish_perms(user: dict[str, Any] = Depends(require_owner)):
    if not flag_enabled("VEYA_TDX_AGENT_PUBLISH_PERMS"):
        raise HTTPException(status_code=404, detail="VEYA_TDX_AGENT_PUBLISH_PERMS disabled")
    try:
        permissions = load_permissions()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Publish permission store unavailable") from exc
    return {"enabled": True, "permissions": permissions}


@router.put("/publish-perms/{agent_id}")
def update_publish_perms(agent_id: str, body: PublishPermUpdate, user: dict[str, Any] = Depends(require_owner)):
    if not flag_enabled("VEYA_TDX_AGENT_PUBLISH_PERMS"):
        raise HTTPException(status_code=404, detail="VEYA_TDX_AGENT_PUBLISH_PERMS disabled")
    try:
        permissions = save_permissions(agent_id, body.model_dump())
        from server.events import append_canonical_event

        append_canonical_event(
            "tdx.publish_perms_updated",
            {"agent_id": agent_id, "permissions": permissions},
            actor=str(user.get("user_id") or "owner"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not persist publish permission update and audit") from exc
    return {"status": "ok", "agent_id": agent_id, "permissions": permissions}


if not global_tool_guard.has_policy("tdx_publish_permission"):
    global_tool_guard.register_policy("tdx_publish_permission", tool_guard_policy, enforce=True)


# Feature 3: Run History — projections over the existing canonical event store.
def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _first_present(records: list[dict[str, Any]], *keys: str) -> Any:
    for record in reversed(records):
        payload = _event_payload(record)
        for key in keys:
            value = payload.get(key)
            if value is not None and value != "":
                return value
            value = record.get(key)
            if value is not None and value != "":
                return value
    return None


def _run_events(task_id: str | None = None) -> list[dict[str, Any]]:
    from server import events as event_mod

    try:
        events = event_mod.event_store.read_all(
            task_id=task_id,
            topic_prefixes=("task.", "goal_run.", "execution.", "work_item.", "supervision.", "verification.", "usage."),
        )
    except Exception as exc:
        logger.exception("TDX Run History could not read the canonical event store")
        raise HTTPException(status_code=503, detail="Execution event store unavailable") from exc
    return sorted(
        events,
        key=lambda event: float(event.get("ts") or 0) if isinstance(event.get("ts"), (int, float)) else 0,
    )


def _task_key(event: dict[str, Any]) -> str | None:
    payload = _event_payload(event)
    value = event.get("task_id") or payload.get("task_id") or payload.get("goal_run_id") or payload.get("run_id")
    return str(value) if value is not None and str(value).strip() else None


def _event_status(event: dict[str, Any]) -> str | None:
    payload = _event_payload(event)
    value = payload.get("status") or payload.get("state")
    if value is not None:
        return str(value)
    topic = str(event.get("topic") or event.get("type") or "").lower()
    for suffix in ("completed", "complete", "blocked", "failed", "cancelled", "canceled", "running", "started", "pending"):
        if topic.endswith("." + suffix):
            return "completed" if suffix == "complete" else ("running" if suffix == "started" else suffix)
    return None


def _explicit_round_number(event: dict[str, Any]) -> int | None:
    payload = _event_payload(event)
    for key in ("round", "round_no", "round_number", "attempt", "attempt_no"):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def _round_number(event: dict[str, Any]) -> int:
    """Return an explicit round when present; inference is handled by _project_runs."""
    return _explicit_round_number(event) or 1


def _event_timestamp(event: dict[str, Any]) -> float:
    value = event.get("ts")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _infer_round_groups(records: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Respect persisted round numbers; infer repeated starts only when no round is recorded."""
    ordered = sorted(records, key=_event_timestamp)
    if any(_explicit_round_number(event) is not None for event in ordered):
        groups: dict[int, list[dict[str, Any]]] = {}
        current_round = 1
        for event in ordered:
            explicit = _explicit_round_number(event)
            if explicit is not None:
                current_round = explicit
            groups.setdefault(current_round, []).append(event)
        return groups

    groups = {}
    current_round = 1
    saw_start = False
    for event in ordered:
        topic = str(event.get("topic") or event.get("type") or "").lower()
        if topic.endswith(".started") or topic in {"run.started", "execution.started"}:
            if saw_start:
                current_round += 1
            saw_start = True
        groups.setdefault(current_round, []).append(event)
    return groups


def _read_run_evidence(task_id: str, records: list[dict[str, Any]]) -> Any:
    """Evidence adapter: prefer the optional GSK ledger, then existing supervision events.

    The GSK branch is deliberately optional because its evidence module lives on a
    separate branch. The fallback reads evidence payloads already recorded in the
    canonical event store; it does not create an additional ledger.
    """
    try:
        from veya.gsk import evidence as gsk_evidence
    except ImportError:
        gsk_evidence = None

    if gsk_evidence is not None:
        for name in ("list_task_evidence", "read_task_evidence", "get_task_evidence"):
            reader = getattr(gsk_evidence, name, None)
            if not callable(reader):
                continue
            try:
                value = reader(task_id=task_id)
            except TypeError:
                try:
                    value = reader(task_id)
                except Exception:
                    logger.exception("Optional GSK evidence adapter failed for task %s", task_id)
                    value = None
            except Exception:
                logger.exception("Optional GSK evidence adapter failed for task %s", task_id)
                value = None
            if value is not None:
                return value

    for event in reversed(records):
        topic = str(event.get("topic") or event.get("type") or "").lower()
        if not topic.startswith(("supervision.", "verification.")):
            continue
        payload = _event_payload(event)
        for key in ("evidence", "verification_evidence", "evidence_chain", "test_results"):
            if payload.get(key) is not None:
                return payload[key]
    return None


def _project_round(task_id: str, round_no: int, records: list[dict[str, Any]]) -> dict[str, Any]:
    latest = records[-1] if records else {}
    round_data: dict[str, Any] = {"task_id": task_id, "round": round_no}
    status_value = next((_event_status(event) for event in reversed(records) if _event_status(event)), None)
    if status_value is not None:
        round_data["status"] = status_value
    for output, keys in (
        ("plan", ("plan", "plan_summary", "objective", "goal", "intent")),
        ("diff_summary", ("diff_summary", "diff", "git_diff", "git_delta")),
        ("evidence", ("evidence", "test_results", "tests", "verification")),
        ("evidence_tags", ("evidence_tags", "gstack_evidence_tags", "evidence_labels")),
        ("model", ("model", "model_name", "provider_model")),
        ("token_usage", ("token_usage", "usage", "tokens")),
        ("commit_sha", ("commit_sha", "head_sha", "git_commit_sha")),
        ("changed_files", ("changed_files", "files_changed_paths", "modified_files")),
    ):
        value = _first_present(records, *keys)
        if value is not None:
            round_data[output] = value
    adapter_evidence = _read_run_evidence(task_id, records)
    if adapter_evidence is not None:
        round_data["evidence"] = adapter_evidence
    passed = _first_present(records, "passed", "tests_passed")
    failed = _first_present(records, "failed", "tests_failed")
    if passed is not None or failed is not None:
        round_data["test_counts"] = {key: value for key, value in (("passed", passed), ("failed", failed)) if value is not None}
    diff_value = round_data.get("diff_summary")
    if not isinstance(diff_value, dict):
        diff_value = {}
    stat_values = {
        key: _first_present(records, *aliases)
        for key, aliases in (
            ("additions", ("additions", "lines_added")),
            ("deletions", ("deletions", "lines_deleted")),
            ("files_changed", ("files_changed_count", "changed_file_count")),
        )
    }
    stat_values = {key: value for key, value in stat_values.items() if isinstance(value, (int, float))}
    if stat_values:
        round_data["diff_summary"] = {**diff_value, **stat_values}
    token_usage = round_data.get("token_usage")
    if isinstance(token_usage, (int, float)):
        round_data["token_usage"] = {"total": token_usage}
    times = [event.get("ts") for event in records if isinstance(event.get("ts"), (int, float))]
    if times:
        round_data["started_at"] = min(times)
        round_data["updated_at"] = max(times)
    duration = _first_present(records, "duration_s", "duration_seconds")
    if duration is None:
        duration_ms = _first_present(records, "duration_ms")
        if isinstance(duration_ms, (int, float)):
            duration = float(duration_ms) / 1000
    if duration is None and len(times) >= 2:
        duration = max(times) - min(times)
    if isinstance(duration, (int, float)) and duration >= 0:
        round_data["duration_s"] = float(duration)
    return round_data


def _project_runs(events: list[dict[str, Any]], selected_task_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        task_id = _task_key(event)
        if task_id is None or (selected_task_id is not None and task_id != selected_task_id):
            continue
        grouped.setdefault(task_id, []).append(event)
    output: dict[str, list[dict[str, Any]]] = {}
    for task_id, records in grouped.items():
        by_round = _infer_round_groups(records)
        output[task_id] = [
            _project_round(task_id, round_no, round_records)
            for round_no, round_records in sorted(by_round.items())
        ]
    return output


@router.get("/runs")
def get_runs(task_id: str | None = None, limit: int = 50, user: dict[str, Any] = Depends(require_owner)):
    if not flag_enabled("VEYA_TDX_RUN_HISTORY"):
        raise HTTPException(status_code=404, detail="VEYA_TDX_RUN_HISTORY disabled")
    limit = max(1, min(int(limit), 200))
    projected = _project_runs(_run_events(task_id), task_id)
    runs: list[dict[str, Any]] = []
    for run_id, rounds in projected.items():
        if not rounds:
            continue
        latest = rounds[-1]
        run: dict[str, Any] = {"task_id": run_id, "rounds": rounds}
        for key in ("plan", "diff_summary", "evidence", "model", "token_usage", "duration_s", "status", "updated_at"):
            if key in latest:
                run[key] = latest[key]
        if len(rounds) > 1:
            run["round_count"] = len(rounds)
        runs.append(run)
    runs.sort(key=lambda run: run.get("updated_at", 0) if isinstance(run.get("updated_at"), (int, float)) else 0, reverse=True)
    return {"enabled": True, "runs": runs[:limit], "count": min(len(runs), limit)}


@router.get("/runs/{task_id}/rounds")
def get_run_rounds(task_id: str, user: dict[str, Any] = Depends(require_owner)):
    if not flag_enabled("VEYA_TDX_RUN_HISTORY"):
        raise HTTPException(status_code=404, detail="VEYA_TDX_RUN_HISTORY disabled")
    rounds = _project_runs(_run_events(task_id), task_id).get(task_id, [])
    if not rounds:
        raise HTTPException(status_code=404, detail="No canonical task/execution events found for this task")
    return {"enabled": True, "task_id": task_id, "rounds": rounds}


@router.get("/runs/{task_id}/compare")
def compare_run_rounds(task_id: str, a: int = 1, b: int = 2, user: dict[str, Any] = Depends(require_owner)):
    if not flag_enabled("VEYA_TDX_RUN_HISTORY"):
        raise HTTPException(status_code=404, detail="VEYA_TDX_RUN_HISTORY disabled")
    rounds = _project_runs(_run_events(task_id), task_id).get(task_id, [])
    left = next((item for item in rounds if item.get("round") == a), None)
    right = next((item for item in rounds if item.get("round") == b), None)
    if left is None or right is None:
        raise HTTPException(status_code=404, detail="Both requested rounds must exist in canonical events")

    comparison: dict[str, Any] = {"status_change": {"from": left.get("status"), "to": right.get("status")}}
    for field, output_name in (("duration_s", "duration_delta_s"),):
        if isinstance(left.get(field), (int, float)) and isinstance(right.get(field), (int, float)):
            comparison[output_name] = float(right[field]) - float(left[field])
    diff_a = left.get("diff_summary") if isinstance(left.get("diff_summary"), dict) else {}
    diff_b = right.get("diff_summary") if isinstance(right.get("diff_summary"), dict) else {}
    line_deltas = {}
    for field in ("additions", "deletions", "files_changed"):
        if isinstance(diff_a.get(field), (int, float)) and isinstance(diff_b.get(field), (int, float)):
            line_deltas[field] = diff_b[field] - diff_a[field]
    if line_deltas:
        comparison["diff_delta"] = line_deltas
    tests_a = left.get("test_counts") if isinstance(left.get("test_counts"), dict) else {}
    tests_b = right.get("test_counts") if isinstance(right.get("test_counts"), dict) else {}
    tests_delta = {key: tests_b[key] - tests_a[key] for key in ("passed", "failed") if isinstance(tests_a.get(key), (int, float)) and isinstance(tests_b.get(key), (int, float))}
    if tests_delta:
        comparison["tests_delta"] = tests_delta
    tokens_a = left.get("token_usage") if isinstance(left.get("token_usage"), dict) else {}
    tokens_b = right.get("token_usage") if isinstance(right.get("token_usage"), dict) else {}
    token_delta = {
        key: tokens_b[key] - tokens_a[key]
        for key in set(tokens_a).intersection(tokens_b)
        if isinstance(tokens_a.get(key), (int, float)) and isinstance(tokens_b.get(key), (int, float))
    }
    if token_delta:
        comparison["token_usage_delta"] = token_delta
    changed_fields = sorted(
        key for key in set(left).intersection(right)
        if key not in {"task_id", "round", "started_at", "updated_at"} and left[key] != right[key]
    )
    comparison["changed_fields"] = changed_fields
    return {"enabled": True, "task_id": task_id, "round_a": a, "round_b": b, "diff_comparison": comparison}

