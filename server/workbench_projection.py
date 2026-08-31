"""Read-only Workbench projection assembled from existing canonical state.

The Workbench is a view, not a new authority.  This module deliberately
reads the existing task/event projections, GoalRun durable repository, and
task-scoped coding artifacts.  It never writes any of them and it never makes
execution or policy decisions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from server.events import EventStore, event_store
from server.task_store import TaskStore, task_store

_SECRET_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "credential",
    "private_key",
    "api_key",
    "access_key",
    "client_secret",
)
_ARTIFACT_NAMES = frozenset(
    {
        "diff.patch",
        "changed_files.json",
        "sensor_report.json",
        "verification_report.json",
        "artifact_manifest.json",
        "final_result.json",
        "delegate_result.json",
    }
)
_USAGE_KEYS = frozenset(
    {
        "provider",
        "model",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "latency_ms",
        "cost_usd",
        "estimated_cost_usd",
    }
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?key|token|password|secret)\s*[:=]\s*[\"']?)[^\s,;\"'}]+"
    ),
    re.compile(r"(?i)\b(?:sk|gh[pousr]|xox[baprs])-[A-Za-z0-9_-]+\b"),
)


def _is_secret_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    if normalized in {"input_tokens", "output_tokens", "total_tokens"}:
        return False
    return any(part in normalized for part in _SECRET_KEY_PARTS)


def redact_text(value: str) -> str:
    """Redact common secret-shaped text before it can reach the browser."""

    result = value
    for pattern in _SECRET_TEXT_PATTERNS:
        result = pattern.sub(
            lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]", result
        )
    return result


def redact_value(value: Any, *, key: object | None = None) -> Any:
    """Return JSON-safe, recursively redacted data for the view boundary."""

    if key is not None and _is_secret_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): redact_value(v, key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, Mapping) else event


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _nested_value(value: Any, wanted: str) -> Any:
    if isinstance(value, Mapping):
        if wanted in value:
            return value[wanted]
        for nested in value.values():
            found = _nested_value(nested, wanted)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _nested_value(nested, wanted)
            if found is not None:
                return found
    return None


def _event_id(event: Mapping[str, Any]) -> str:
    return str(event.get("event_id") or event.get("id") or "")


def _safe_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": _event_id(event),
        "topic": str(event.get("topic") or event.get("type") or event.get("event") or "unknown"),
        "ts": event.get("ts"),
        "actor": str(event.get("actor") or "system"),
        "task_id": event.get("task_id"),
        "session_id": event.get("session_id"),
        "payload": redact_value(dict(_payload(event))),
    }


def _status_from_event(event: Mapping[str, Any]) -> str | None:
    topic = str(event.get("topic") or "")
    payload = _payload(event)
    value = payload.get("status")
    if topic == "goal_run.status_changed" and value:
        return str(value)
    if topic.startswith("goal.") and topic.endswith("completed"):
        return "completed"
    if topic in {"work_item.failed", "goal.failed"}:
        return "failed"
    if topic in {"work_item.cancelled", "goal.cancelled"}:
        return "cancelled"
    return str(value) if topic.startswith("goal_run.") and value else None


class WorkbenchProjection:
    """Build one task-scoped read model without introducing persistence."""

    def __init__(
        self,
        *,
        tasks: TaskStore | None = None,
        events: EventStore | None = None,
        project_root: str | Path | None = None,
    ) -> None:
        self.tasks = tasks or task_store
        self.events = events or event_store
        # Do not discover a second filesystem root here.  The deployment
        # injects VEYA_PROJECT_ROOT; ``.`` is the existing process root when
        # no override is configured.
        self.project_root = Path(
            project_root or os.environ.get("VEYA_PROJECT_ROOT") or "."
        ).expanduser()

    def _events_for(
        self, task_id: str, session_id: str, trace_id: str | None
    ) -> list[dict[str, Any]]:
        selected: dict[str, dict[str, Any]] = {}
        for event in self.events.read_all(task_id=task_id):
            selected[_event_id(event) or f"task:{len(selected)}"] = event
        # A few older events were emitted with a session id only.  Include them
        # only when their trace/turn identifies this task; never expose another
        # turn from the same session in this task-scoped view.
        for event in self.events.read_all(session_id=session_id):
            event_task = event.get("task_id")
            if event_task == task_id:
                selected.setdefault(_event_id(event) or f"session:{len(selected)}", event)
                continue
            if event_task is not None:
                continue
            event_trace_id = str(event.get("trace_id") or "")
            event_turn_id = str(event.get("turn_id") or "")
            task_trace_id = str(trace_id or "")
            if event_trace_id not in {task_trace_id, task_id} and event_turn_id not in {
                task_trace_id,
                task_id,
            }:
                continue
            selected.setdefault(_event_id(event) or f"session:{len(selected)}", event)
        return sorted(
            selected.values(), key=lambda item: (float(item.get("ts") or 0), _event_id(item))
        )

    @staticmethod
    def _active_approvals(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        pending: dict[str, dict[str, Any]] = {}
        for event in events:
            topic = str(event.get("topic") or "")
            if topic not in {"tool.approval_required", "tool.approved", "tool.denied"}:
                continue
            payload = _payload(event)
            request_id = str(payload.get("request_id") or "")
            if not request_id:
                continue
            if topic == "tool.approval_required":
                pending[request_id] = {
                    "request_id": request_id,
                    "tool_name": str(payload.get("tool_name") or "unknown"),
                    "tool_args": redact_value(payload.get("tool_args") or {}),
                    "reason": redact_text(str(payload.get("reason") or "需要用户批准")),
                    "created_at": event.get("ts"),
                    "status": "pending",
                }
            else:
                pending.pop(request_id, None)
        return list(pending.values())

    @staticmethod
    def _browser(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        browser: Mapping[str, Any] | None = None
        for event in reversed(events):
            payload = _payload(event)
            for key in ("browser", "browser_handle", "handle"):
                candidate = _mapping(payload.get(key))
                if candidate and candidate.get("session_id"):
                    browser = candidate
                    break
            if browser is not None:
                break
        if browser is None:
            return {
                "status": "not_observed",
                "control_state": "unknown",
                "session_id": None,
                "current_url": None,
                "snapshot": None,
            }
        snapshot = None
        for event in reversed(events):
            snapshot = _nested_value(_payload(event), "snapshot")
            if snapshot is not None:
                break
        return {
            "status": str(browser.get("state") or "unknown"),
            "control_state": str(browser.get("control_state") or "AGENT_CONTROL"),
            "session_id": str(browser.get("session_id")),
            "computer_id": browser.get("computer_id"),
            "current_url": redact_value(browser.get("url")),
            "version": browser.get("version"),
            "snapshot": redact_value(snapshot),
        }

    @staticmethod
    def _computer(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        computer: Mapping[str, Any] | None = None
        for event in reversed(events):
            payload = _payload(event)
            for key in ("computer", "computer_handle"):
                candidate = _mapping(payload.get(key))
                if candidate and (candidate.get("computer_id") or candidate.get("id")):
                    computer = candidate
                    break
            if computer is not None:
                break
        if computer is None:
            return {"status": "not_observed", "computer_id": None, "workspace": None}
        return {
            "status": str(computer.get("state") or computer.get("status") or "unknown"),
            "computer_id": computer.get("computer_id") or computer.get("id"),
            "workspace": redact_value(computer.get("workspace")),
        }

    @staticmethod
    def _conversation(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for event in events:
            topic = str(event.get("topic") or "")
            if topic not in {"message.user_added", "message.assistant_added"}:
                continue
            payload = _payload(event)
            content = payload.get("content", payload.get("text", ""))
            result.append(
                {
                    "event_id": _event_id(event),
                    "role": "user" if topic == "message.user_added" else "assistant",
                    "content": redact_text(str(content or "")),
                    "ts": event.get("ts"),
                }
            )
        return result

    @staticmethod
    def _usage(task: Any, events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for event in events:
            payload = _payload(event)
            usage = payload.get("usage")
            candidates = [usage] if isinstance(usage, Mapping) else [payload]
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                selected = {
                    key: redact_value(candidate[key], key=key)
                    for key in _USAGE_KEYS
                    if key in candidate
                }
                if selected:
                    selected["event_id"] = _event_id(event)
                    selected["topic"] = str(event.get("topic") or "unknown")
                    records.append(selected)
        return {"cost_usd": float(task.cost_usd or 0.0), "records": records[-100:]}

    def _coding_state(self, task_id: str) -> Any | None:
        try:
            from runtime.coding.task_service import CodingTaskService

            return CodingTaskService(self.project_root).get_task_state(task_id)
        except Exception:
            return None

    def _artifact_json(self, task_id: str, name: str) -> dict[str, Any] | None:
        path = self.project_root / ".veya" / "runs" / task_id / "outputs" / name
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        redacted = redact_value(value)
        return redacted if isinstance(redacted, dict) else None

    async def _goal_run(
        self, task_id: str, events: Sequence[Mapping[str, Any]], coding_state: Any
    ) -> dict[str, Any]:
        goal_run_id = getattr(coding_state, "goal_run_id", None)
        if not goal_run_id:
            goal_run_id = _nested_value(events, "goal_run_id")
        goal_run_id = str(goal_run_id) if goal_run_id else None
        durable_goal: dict[str, Any] | None = None
        durable_items: list[dict[str, Any]] = []
        if goal_run_id and os.environ.get("VEYA_DURABLE_EXECUTION", "0").strip().lower() not in {
            "0",
            "false",
            "off",
            "no",
        }:
            try:
                from runtime.execution.runtime import get_durable_runtime

                runtime = get_durable_runtime()
                if runtime.config.enabled and runtime.config.queue_read:
                    durable_goal = await runtime.repository.get_goal_run(goal_run_id)
                    durable_items = await runtime.repository.list_work_items(goal_run_id)
            except Exception:
                # A view must remain available when an optional durable backend
                # is unavailable; it must not invent a successful state.
                durable_goal = None
                durable_items = []

        item_by_id: dict[str, dict[str, Any]] = {}
        for event in events:
            topic = str(event.get("topic") or "")
            if not topic.startswith("work_item."):
                continue
            payload = _payload(event)
            item = _mapping(payload.get("work_item")) or payload
            item_id = str(
                item.get("id") or item.get("work_item_id") or payload.get("task_id") or ""
            )
            if item_id:
                current = dict(item_by_id.get(item_id, {}))
                current.update({"id": item_id, "state": topic.removeprefix("work_item.")})
                for key in ("logical_key", "kind", "revision", "updated_at"):
                    if key in item:
                        current[key] = redact_value(item[key])
                item_by_id[item_id] = current
        if durable_items:
            item_by_id = {
                str(item.get("id") or item.get("logical_key")): {
                    key: redact_value(item.get(key))
                    for key in ("id", "logical_key", "kind", "state", "revision", "updated_at")
                    if item.get(key) is not None
                }
                for item in durable_items
                if item.get("id") or item.get("logical_key")
            }
        statuses = [status for status in (_status_from_event(event) for event in events) if status]
        status = str(
            (durable_goal or {}).get("status")
            or (
                getattr(coding_state, "status", None)
                or (statuses[-1] if statuses else "not_started")
            )
        )
        return {
            "authority": "DurableExecutionRepository"
            if durable_goal
            else "GoalRun event projection",
            "goal_run_id": goal_run_id,
            "status": status,
            "revision": (durable_goal or {}).get("revision"),
            "work_items": list(item_by_id.values()),
        }

    @staticmethod
    def _artifact_names(
        task_id: str, coding_state: Any, project_root: Path
    ) -> list[dict[str, Any]]:
        output_dir = project_root / ".veya" / "runs" / task_id / "outputs"
        names = (
            {path.name for path in output_dir.iterdir() if path.is_file()}
            if output_dir.is_dir()
            else set()
        )
        if coding_state is not None:
            final = getattr(coding_state, "final_result", None) or {}
            for path in (final.get("outputs") or {}).values() if isinstance(final, Mapping) else []:
                name = Path(str(path)).name
                if name in _ARTIFACT_NAMES:
                    names.add(name)
        return [
            {
                "name": name,
                "available": (output_dir / name).is_file(),
                "endpoint": f"/api/v1/workbench/{task_id}/artifact/{name}",
            }
            for name in sorted(names & _ARTIFACT_NAMES)
        ]

    async def build(self, task_id: str) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        coding_state = self._coding_state(task_id)
        if task is None and coding_state is None:
            return None
        if task is not None:
            task_id = task.id
            session_id = task.session_id
            trace_id = task.trace_id
            task_data = task.to_dict()
        else:
            task_data = {
                "id": task_id,
                "session_id": "",
                "title": str(getattr(coding_state, "objective", task_id))[:80],
                "objective": getattr(coding_state, "objective", ""),
                "status": getattr(coding_state, "status", "unknown"),
                "workspace_id": getattr(coding_state, "workspace_id", None),
                "created_at": getattr(coding_state, "created_at", None),
                "updated_at": getattr(coding_state, "updated_at", None),
                "cost_usd": 0.0,
                "trace_id": None,
            }
            session_id = ""
            trace_id = None
        task_data = redact_value(task_data)
        events = self._events_for(task_id, session_id, trace_id)
        safe_events = [_safe_event(event) for event in events]
        goal_run = await self._goal_run(task_id, events, coding_state)
        computer = self._computer(events)
        browser = self._browser(events)
        active_approvals = self._active_approvals(events)
        event_ids = [_event_id(event) for event in events]
        version_input = {
            "task_updated_at": task_data.get("updated_at"),
            "event_ids": event_ids,
            "goal_run_revision": goal_run.get("revision"),
            "browser_version": browser.get("version"),
        }
        version = hashlib.sha256(
            json.dumps(version_input, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:24]
        final_result = getattr(coding_state, "final_result", None) if coding_state else None
        changed_files = (
            list((final_result or {}).get("changed_files") or [])
            if isinstance(final_result, Mapping)
            else []
        )
        changed_files_report = self._artifact_json(task_id, "changed_files.json") or {}
        if not changed_files:
            changed_files = list(changed_files_report.get("files") or [])
        sensor_report = self._artifact_json(task_id, "sensor_report.json") or {}
        verification_report = self._artifact_json(task_id, "verification_report.json") or {}
        delegate_result = self._artifact_json(task_id, "delegate_result.json") or {}
        task_status = getattr(coding_state, "status", None) or (
            task.status if task is not None else task_data.get("status")
        )
        task_updated_at = task.updated_at if task is not None else task_data.get("updated_at")
        return {
            "schema_version": 1,
            "task": task_data,
            "session": {"session_id": session_id, "trace_id": trace_id},
            "state": {
                "status": str(task_status or "unknown"),
                "updated_at": task_updated_at,
                "version": version,
                "event_count": len(safe_events),
            },
            "conversation": self._conversation(events),
            "timeline": safe_events[-300:],
            "goal_run": goal_run,
            "computer": {**computer, "authority": "ComputerSupervisor"},
            "browser": browser,
            "approvals": {"pending": active_approvals},
            "governance": {
                "decisions": [
                    event for event in safe_events if event["topic"] == "action_gateway.audit"
                ],
                "side_effects": [
                    event for event in safe_events if event["topic"].startswith("side_effect.")
                ],
            },
            "verification": {
                "status": (final_result or {}).get("status")
                if isinstance(final_result, Mapping)
                else None,
                "acceptance_passed": (final_result or {}).get("acceptance_passed")
                if isinstance(final_result, Mapping)
                else None,
                "verification_report_id": (final_result or {}).get("verification_report_id")
                if isinstance(final_result, Mapping)
                else None,
                "changed_files": [str(path) for path in changed_files],
                "sensor_summary": redact_value(
                    sensor_report.get("summary") or verification_report.get("sensor_summary") or {}
                ),
                "acceptance_report": redact_value(
                    {
                        key: verification_report[key]
                        for key in ("acceptance_passed", "delegate_status", "delegate_stop_reason")
                        if key in verification_report
                    }
                ),
                "delegate_result": delegate_result,
            },
            "artifacts": self._artifact_names(task_id, coding_state, self.project_root),
            "usage": self._usage(task, events),
            "source_of_truth": {
                "task": "TaskStore projection",
                "events": "EventStore append-only history",
                "goal_run": goal_run["authority"],
                "browser": "BrowserComputerAdapter process handle when available",
            },
        }


__all__ = ["WorkbenchProjection", "redact_text", "redact_value"]
