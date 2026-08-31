"""Unified Workbench API assembled from existing Layer4 authorities.

The endpoints are adapters only.  Task cancellation/resume and approval keep
using their existing mechanisms; this route does not own a policy, ledger,
task store, browser store, or event store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.browser_computer_adapter import get_browser_adapter
from server.events import append_canonical_event
from server.task_store import task_store
from server.workbench_projection import WorkbenchProjection, redact_text, redact_value

router = APIRouter(prefix="/api/v1/workbench", tags=["workbench"])
projection = WorkbenchProjection()


class ApprovalRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=200)
    approved: bool
    expected_version: str | None = Field(None, min_length=1, max_length=100)


class BrowserControlRequest(BaseModel):
    action: Literal["status", "takeover", "return_control"]
    browser_session_id: str | None = Field(None, max_length=200)
    expected_handle_version: int | None = Field(None, ge=1)


class TaskControlRequest(BaseModel):
    action: Literal["cancel", "resume"]
    expected_version: str | None = Field(None, min_length=1, max_length=100)


def _stale(
    detail: str, *, expected: str | int | None = None, actual: str | int | None = None
) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": detail, "expected": expected, "actual": actual},
    )


async def _view_or_404(task_id: str) -> dict[str, Any]:
    view = await projection.build(task_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return view


@router.get("/{task_id}")
async def get_workbench(task_id: str) -> dict[str, Any]:
    """Return the current canonical, redacted Workbench view."""

    return await _view_or_404(task_id)


@router.get("/{task_id}/events")
async def get_workbench_events(task_id: str) -> dict[str, Any]:
    """Return the same canonical timeline for reconnect/polling consumers."""

    view = await _view_or_404(task_id)
    return {
        "task_id": task_id,
        "version": view["state"]["version"],
        "events": view["timeline"],
    }


@router.post("/{task_id}/approval")
async def resolve_workbench_approval(task_id: str, request: ApprovalRequest) -> dict[str, Any]:
    """Resolve one existing user-control approval, with a stale guard."""

    view = await _view_or_404(task_id)
    if request.expected_version and request.expected_version != view["state"]["version"]:
        raise _stale(
            "STALE_WORKBENCH",
            expected=request.expected_version,
            actual=view["state"]["version"],
        )
    pending = {item["request_id"] for item in view["approvals"]["pending"]}
    if request.request_id not in pending:
        raise _stale("STALE_APPROVAL", expected=request.request_id, actual=None)

    # This is the PR-09 pending store, not a Workbench approval store.
    from server.user_control import resolve_approval

    if not resolve_approval(request.request_id, request.approved):
        raise _stale("STALE_APPROVAL", expected=request.request_id, actual=None)
    return await _view_or_404(task_id)


@router.post("/{task_id}/task")
async def control_workbench_task(task_id: str, request: TaskControlRequest) -> dict[str, Any]:
    """Delegate task controls to the existing TaskStore/CodingTask service."""

    view = await _view_or_404(task_id)
    if request.expected_version and request.expected_version != view["state"]["version"]:
        raise _stale(
            "STALE_WORKBENCH",
            expected=request.expected_version,
            actual=view["state"]["version"],
        )
    if task_store.get(task_id) is not None:
        from server.routes import tasks as task_routes
        from server.routes.tasks import TaskResumeRequest

        if request.action == "cancel":
            await task_routes.cancel_task(task_id)
        else:
            await task_routes.resume_task(task_id, TaskResumeRequest(text=None, max_rounds=None))
    else:
        from runtime.coding.task_service import CodingTaskService

        service = CodingTaskService(projection.project_root)
        result = (
            await service.cancel_task(task_id)
            if request.action == "cancel"
            else await service.resume_task(task_id)
        )
        if result is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return await _view_or_404(task_id)


@router.post("/{task_id}/browser/control")
async def control_workbench_browser(task_id: str, request: BrowserControlRequest) -> dict[str, Any]:
    """Delegate browser status/control to the existing PR-11 adapter.

    Browser handles are process-local by design.  A missing adapter is an
    explicit unavailable state, never a fabricated successful takeover.
    """

    view = await _view_or_404(task_id)
    browser = view["browser"]
    session_id = request.browser_session_id or browser.get("session_id")
    if not session_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "BROWSER_SESSION_UNAVAILABLE", "reason": "no browser handle observed"},
        )
    adapter = get_browser_adapter(str(session_id))
    if adapter is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "BROWSER_SESSION_UNAVAILABLE",
                "reason": "browser handle is not attached to this process",
                "browser_session_id": str(session_id),
            },
        )

    current = await adapter.status(
        {
            "session_id": str(session_id),
            **{key: value for key, value in browser.items() if value is not None},
        }
    )
    if not current.get("ok"):
        raise _stale("STALE_BROWSER_CONTROL", expected=session_id, actual=current.get("error"))
    handle = current.get("handle") or current.get("browser") or {}
    actual_version = handle.get("version") if isinstance(handle, dict) else None
    if (
        request.expected_handle_version is not None
        and actual_version != request.expected_handle_version
    ):
        raise _stale(
            "STALE_BROWSER_CONTROL",
            expected=request.expected_handle_version,
            actual=actual_version,
        )
    if request.action == "status":
        return {"task_id": task_id, "browser": redact_value(current)}

    result = (
        await adapter.take_control(handle)
        if request.action == "takeover"
        else await adapter.return_control(handle)
    )
    if not result.get("ok", result.get("status") not in {"failed", "error"}):
        raise HTTPException(status_code=409, detail=redact_value(result))
    next_handle = result.get("handle") or result.get("browser") or handle
    task = task_store.get(task_id)
    append_canonical_event(
        "browser.control_changed",
        {
            "browser_handle": redact_value(next_handle),
            "control_state": next_handle.get("control_state")
            if isinstance(next_handle, dict)
            else None,
        },
        actor="user",
        session_id=task.session_id if task else None,
        trace_id=task.trace_id if task else None,
        task_id=task_id,
    )
    return await _view_or_404(task_id)


def _artifact_path(task_id: str, name: str) -> Path:
    # The allowlist is intentionally duplicated as a boundary check here; a
    # path from a task artifact cannot select arbitrary files from the host.
    allowed = {
        "diff.patch",
        "changed_files.json",
        "sensor_report.json",
        "verification_report.json",
        "artifact_manifest.json",
        "final_result.json",
    }
    if name not in allowed or Path(name).name != name:
        raise HTTPException(status_code=404, detail="artifact not found")
    return projection.project_root / ".veya" / "runs" / task_id / "outputs" / name


@router.get("/{task_id}/artifact/{name}")
async def get_workbench_artifact(task_id: str, name: str) -> dict[str, Any]:
    """Read one allowlisted task artifact after redaction."""

    await _view_or_404(task_id)
    path = _artifact_path(task_id, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise HTTPException(status_code=415, detail="artifact is not text")
    if len(content) > 2_000_000:
        raise HTTPException(status_code=413, detail="artifact is too large for Workbench")
    if name.endswith(".json"):
        try:
            value: Any = redact_value(json.loads(content))
            return {
                "task_id": task_id,
                "name": name,
                "content_type": "application/json",
                "content": value,
            }
        except json.JSONDecodeError:
            pass
    return {
        "task_id": task_id,
        "name": name,
        "content_type": "text/plain",
        "content": redact_text(content),
    }


__all__ = ["router"]
