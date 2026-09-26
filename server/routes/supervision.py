"""Supervision HTTP surface — a thin, authorized adapter over the canonical service.

There is exactly one supervision implementation: :mod:`server.supervision_tools`
(itself a facade over :mod:`veya.supervision`). This module only translates HTTP
into calls on that service; it never re-implements routing, review, Jev or the
state machine, and it never reads store files directly.

Authorization (fail closed, see :mod:`server.web_workspace_auth`):

* the caller is a Web user (``server.auth``), never a Remote MCP token;
* the workspace must be authorized for that user before a Mission is created;
* every read/mutation re-resolves the Mission and re-checks ownership *and* that
  its workspace is still authorized — knowing a ``mission_id`` is never enough.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.supervision_tools import (
    veya_escalation_list,
    veya_events,
    veya_mission_cancel,
    veya_mission_continue,
    veya_mission_create,
    veya_mission_inspect,
    veya_mission_run,
    veya_mission_set_mode,
    veya_report_get,
    veya_report_latest,
    veya_reviews,
)
from server.web_workspace_auth import (
    MissionMissing,
    WorkspaceDenied,
    authorize_workspace,
    mission_for_user,
    missions_for_user,
    stamp_owner,
    user_id,
)

router = APIRouter(
    prefix="/api/v1/supervision",
    tags=["supervision"],
    dependencies=[Depends(auth_mod.require_user)],
)

_SSE_POLL_S = 1.0
_SSE_MAX_TICKS = 1800  # ~30 min; clients reconnect

User = dict[str, Any]


class MissionCreateRequest(BaseModel):
    goal: str
    supervision_mode: str = "auto"
    workspace: str = ""
    executor: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    # Canonical SupervisorReview payload (decision: CONTINUE/REVISE/RETRY/ROLLBACK/ACCEPT/DONE).
    review: dict[str, Any]


class ModeRequest(BaseModel):
    mode: str


def _deny(exc: Exception) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


def _owned(mission_id: str, user: User) -> str:
    """Authorize + resolve the mission's store root (403/404 on failure)."""
    try:
        root, _mission, _store = mission_for_user(mission_id, user)
    except MissionMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkspaceDenied as exc:
        raise _deny(exc) from exc
    return root


def _call(fn: Any, **kwargs: Any) -> Any:
    try:
        return fn(**kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorkspaceDenied as exc:
        raise _deny(exc) from exc


@router.post("/missions")
def create_mission(
    payload: MissionCreateRequest, user: User = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    try:
        root = authorize_workspace(user, payload.workspace or None)
    except WorkspaceDenied as exc:
        # No Mission, no `.veya-project`, no executor side effect.
        raise _deny(exc) from exc
    created = _call(
        veya_mission_create,
        project_root=root,
        goal=payload.goal,
        supervision_mode=payload.supervision_mode,
        workspace=root,
        acceptance_criteria=payload.acceptance_criteria or None,
        constraints=payload.constraints or None,
        executor=payload.executor or "",
    )
    from veya.supervision import MissionStore

    store = MissionStore(root)
    mission = store.load(created["mission"]["mission_id"])
    if mission is not None:
        stamp_owner(mission, user_id(user))
        store.save(mission)
        created["mission"] = mission.to_dict()
    return created


@router.get("/missions")
def list_missions(user: User = Depends(auth_mod.get_current_user)) -> dict[str, Any]:
    try:
        return {"missions": missions_for_user(user)}
    except WorkspaceDenied as exc:
        raise _deny(exc) from exc


@router.get("/missions/{mission_id}")
def inspect_mission(
    mission_id: str, user: User = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    return _call(veya_mission_inspect, project_root=_owned(mission_id, user), mission_id=mission_id)


@router.post("/missions/{mission_id}/run")
async def run_mission(
    mission_id: str, user: User = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    root = _owned(mission_id, user)
    try:
        return await veya_mission_run(project_root=root, mission_id=mission_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/missions/{mission_id}/cancel")
def cancel_mission(
    mission_id: str, user: User = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    return _call(veya_mission_cancel, project_root=_owned(mission_id, user), mission_id=mission_id)


@router.post("/missions/{mission_id}/continue")
def continue_mission(
    mission_id: str, payload: ReviewRequest, user: User = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    """Canonical review/retask path — this is what the UI's Retry uses."""
    return _call(
        veya_mission_continue,
        project_root=_owned(mission_id, user),
        mission_id=mission_id,
        review=payload.review,
    )


@router.post("/missions/{mission_id}/mode")
def set_mode(
    mission_id: str, payload: ModeRequest, user: User = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    return _call(
        veya_mission_set_mode,
        project_root=_owned(mission_id, user),
        mission_id=mission_id,
        mode=payload.mode,
    )


@router.get("/missions/{mission_id}/reports/latest")
def latest_report(
    mission_id: str, user: User = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    return _call(veya_report_latest, project_root=_owned(mission_id, user), mission_id=mission_id)


@router.get("/missions/{mission_id}/reports/{iteration}")
def report_by_iteration(
    mission_id: str, iteration: int, user: User = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    return _call(
        veya_report_get,
        project_root=_owned(mission_id, user),
        mission_id=mission_id,
        iteration=iteration,
    )


@router.get("/missions/{mission_id}/reviews")
def review_history(
    mission_id: str, user: User = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    return _call(veya_reviews, project_root=_owned(mission_id, user), mission_id=mission_id)


@router.get("/missions/{mission_id}/escalations")
def escalations(mission_id: str, user: User = Depends(auth_mod.get_current_user)) -> dict[str, Any]:
    return _call(veya_escalation_list, project_root=_owned(mission_id, user), mission_id=mission_id)


async def _event_stream(root: str, mission_id: str, start: int) -> AsyncIterator[str]:
    """Read-only SSE adapter over the canonical event store (no new semantics)."""
    seen = max(0, int(start))
    for _ in range(_SSE_MAX_TICKS):
        payload = await asyncio.to_thread(veya_events, project_root=root, mission_id=mission_id)
        events = payload.get("events") or []
        while seen < len(events):
            body = json.dumps(events[seen], ensure_ascii=False)
            yield f"id: {seen}\nevent: mission_event\ndata: {body}\n\n"
            seen += 1
        yield ": keep-alive\n\n"
        await asyncio.sleep(_SSE_POLL_S)


@router.get("/missions/{mission_id}/events")
async def mission_events(
    mission_id: str,
    user: User = Depends(auth_mod.get_current_user),
    fmt: str = Query(default="sse", alias="format"),
    since: int = Query(default=0),
) -> Any:
    root = _owned(mission_id, user)
    if fmt == "json":
        # Polling fallback for clients without SSE.
        return _call(veya_events, project_root=root, mission_id=mission_id)
    return StreamingResponse(
        _event_stream(root, mission_id, since),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
