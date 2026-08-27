"""Personal Agent Runtime API.

These endpoints expose durable projections and correction controls.  They do
not select tools, infer intent, or expose hidden reasoning; MasterAgent still
decides when to call the capability tools.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from runtime.personal import PersonalRuntimeError, get_personal_runtime
from server import auth as auth_mod

router = APIRouter(tags=["personal-runtime"])


def _error(exc: PersonalRuntimeError) -> HTTPException:
    status = (
        409
        if exc.code in {"CONFLICT_REVIEW_REQUIRED", "LEARNING_GATE", "SAFETY_HOLD"}
        else 404
        if exc.code == "NOT_FOUND"
        else 422
    )
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


def _workspace(value: str | None) -> str:
    return str(value or os.environ.get("VEYA_WORKSPACE", "default"))


def _assert_user_scope(record: dict[str, Any], user: dict[str, Any]) -> None:
    """Reject cross-user records while leaving workspace membership policy intact."""
    if record.get("scope_type") == "user" and str(record.get("scope_id")) != str(user["user_id"]):
        raise HTTPException(status_code=404, detail="personal record not found")


class MemoryWriteRequest(BaseModel):
    content: str = Field(..., min_length=1)
    scope_type: str = "user"
    scope_id: str | None = None
    memory_type: str = "semantic"
    source_event_ids: list[str] = Field(default_factory=list)
    source_session_ids: list[str] = Field(default_factory=list)
    source_task_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(0.8, ge=0, le=1)
    reason: str = "explicit user instruction"
    commit: bool = False


class MemoryCorrectionRequest(BaseModel):
    content: str = Field(..., min_length=1)
    source_event_ids: list[str] = Field(default_factory=list)
    source_session_ids: list[str] = Field(default_factory=list)
    source_task_ids: list[str] = Field(default_factory=list)


class MemoryCommitRequest(BaseModel):
    allow_conflicts: bool = False
    supersedes: list[str] = Field(default_factory=list)


@router.get("/api/v1/memory")
async def list_memory(
    query: str = "",
    scope_type: str | None = None,
    scope_id: str | None = None,
    memory_type: str | None = None,
    limit: int = 20,
    min_confidence: float = 0.0,
    include_superseded: bool = False,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    if scope_type is None:
        scope_type = "user"
    requested_scope_id = str(user["user_id"]) if scope_type == "user" else scope_id
    records = await get_personal_runtime().search_memory(
        query,
        scope_type=scope_type,
        scope_id=requested_scope_id,
        memory_type=memory_type,
        limit=limit,
        min_confidence=min_confidence,
        include_superseded=include_superseded,
    )
    return {"records": records, "count": len(records), "authority": "execution-runtime"}


@router.post("/api/v1/memory", status_code=201)
async def write_memory(
    req: MemoryWriteRequest, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    scope_id = req.scope_id or (
        str(user["user_id"]) if req.scope_type == "user" else _workspace(req.scope_id)
    )
    try:
        store = get_personal_runtime()
        event = await store.record_event(
            "memory.user_instruction",
            {"content": req.content, "memory_type": req.memory_type},
            session_id=(req.source_session_ids or [None])[0],
            task_id=(req.source_task_ids or [None])[0],
            workspace_id=scope_id if req.scope_type == "workspace" else None,
        )
        candidate = await store.create_memory_candidate(
            req.content,
            scope_type=req.scope_type,
            scope_id=scope_id,
            memory_type=req.memory_type,
            source_event_ids=[*req.source_event_ids, event["id"]],
            source_session_ids=req.source_session_ids,
            source_task_ids=req.source_task_ids,
            confidence=req.confidence,
            reason=req.reason,
            provenance={"actor": user["user_id"], "entrypoint": "api"},
        )
        if req.commit:
            committed = await store.commit_memory_candidate(candidate["id"])
            return {"candidate": candidate, "commit": committed}
        return {"candidate": candidate, "status": "candidate"}
    except PersonalRuntimeError as exc:
        raise _error(exc) from exc


@router.get("/api/v1/memory/candidates/{candidate_id}")
async def get_memory_candidate(
    candidate_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    store = get_personal_runtime()
    candidate = await store.get_memory_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="memory candidate not found")
    _assert_user_scope(candidate, user)
    return candidate


@router.post("/api/v1/memory/candidates/{candidate_id}/commit")
async def commit_memory(
    candidate_id: str,
    req: MemoryCommitRequest | None = None,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    try:
        store = get_personal_runtime()
        candidate = await store.get_memory_candidate(candidate_id)
        if candidate is None:
            raise PersonalRuntimeError("NOT_FOUND", candidate_id)
        _assert_user_scope(candidate, user)
        return await store.commit_memory_candidate(
            candidate_id,
            allow_conflicts=bool(req and req.allow_conflicts),
            supersedes=req.supersedes if req else [],
        )
    except PersonalRuntimeError as exc:
        raise _error(exc) from exc


@router.get("/api/v1/memory/doctor")
async def memory_doctor() -> dict[str, Any]:
    return await get_personal_runtime().memory_doctor()


@router.post("/api/v1/memory/{memory_id}/correct")
async def correct_memory(
    memory_id: str,
    req: MemoryCorrectionRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    try:
        store = get_personal_runtime()
        record = await store.get_memory(memory_id)
        if record is None:
            raise PersonalRuntimeError("NOT_FOUND", memory_id)
        _assert_user_scope(record, user)
        return await store.correct_memory(
            memory_id,
            req.content,
            source_event_ids=req.source_event_ids,
            source_session_ids=req.source_session_ids,
            source_task_ids=req.source_task_ids,
            trace_id=str(user["user_id"]),
        )
    except PersonalRuntimeError as exc:
        raise _error(exc) from exc


@router.post("/api/v1/memory/{memory_id}/forget")
async def forget_memory(
    memory_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    try:
        store = get_personal_runtime()
        record = await store.get_memory(memory_id)
        if record is None:
            raise PersonalRuntimeError("NOT_FOUND", memory_id)
        _assert_user_scope(record, user)
        return await store.forget_memory(memory_id)
    except PersonalRuntimeError as exc:
        raise _error(exc) from exc


@router.get("/api/v1/memory/{memory_id}")
async def show_memory(
    memory_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    record = await get_personal_runtime().get_memory(memory_id, include_sources=True)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    _assert_user_scope(record, user)
    return {"record": record}


class SkillCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    scope_type: str = "user"
    scope_id: str | None = None
    trigger_examples: list[str] = Field(default_factory=list)
    parameters_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    execution_type: str = "prompt"
    execution_ref: str = ""
    source_event_ids: list[str] = Field(default_factory=list)
    source_task_ids: list[str] = Field(default_factory=list)


class SkillRunRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    trace_id: str | None = None


class SkillRollbackRequest(BaseModel):
    version: int = Field(..., ge=1)


@router.get("/api/v1/skills")
async def list_skills(
    query: str = "",
    scope_type: str | None = None,
    scope_id: str | None = None,
    include_candidates: bool = False,
    limit: int = 50,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    if scope_type is None:
        scope_type = "user"
    if scope_type == "user":
        scope_id = str(user["user_id"])
    items = await get_personal_runtime().search_skills(
        query,
        scope_type=scope_type,
        scope_id=scope_id,
        include_candidates=include_candidates,
        limit=limit,
    )
    return {"skills": items, "count": len(items), "authority": "execution-runtime"}


@router.post("/api/v1/skills", status_code=201)
async def create_skill(
    req: SkillCreateRequest, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    scope_id = (str(user["user_id"]) if req.scope_type == "user" else req.scope_id) or (
        str(user["user_id"]) if req.scope_type == "user" else _workspace(req.scope_id)
    )
    try:
        return await get_personal_runtime().create_skill_candidate(
            req.name,
            req.description,
            scope_type=req.scope_type,
            scope_id=scope_id,
            trigger_examples=req.trigger_examples,
            parameters_schema=req.parameters_schema,
            execution_type=req.execution_type,
            execution_ref=req.execution_ref,
            source_event_ids=req.source_event_ids,
            source_task_ids=req.source_task_ids,
            created_by=str(user["user_id"]),
        )
    except PersonalRuntimeError as exc:
        raise _error(exc) from exc


@router.get("/api/v1/skills/{skill_id}")
async def show_skill(
    skill_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    value = await get_personal_runtime().get_skill(skill_id, versions=False)
    if value is None:
        raise HTTPException(status_code=404, detail="skill not found")
    _assert_user_scope(value, user)
    return value


@router.get("/api/v1/skills/{skill_id}/versions")
async def skill_versions(
    skill_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    value = await get_personal_runtime().get_skill(skill_id, versions=True)
    if value is None:
        raise HTTPException(status_code=404, detail="skill not found")
    _assert_user_scope(value, user)
    return {"skill_id": skill_id, "versions": value.get("versions", [])}


@router.post("/api/v1/skills/{skill_id}/confirm")
async def confirm_skill(
    skill_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    try:
        store = get_personal_runtime()
        version = await store.get_skill_version(skill_id)
        if version is None:
            raise PersonalRuntimeError("NOT_FOUND", skill_id)
        _assert_user_scope(version, user)
        return await store.confirm_skill(skill_id)
    except PersonalRuntimeError as exc:
        raise _error(exc) from exc


@router.post("/api/v1/skills/{skill_id}/run")
async def run_skill(
    skill_id: str,
    req: SkillRunRequest | None = None,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    try:
        skill = await get_personal_runtime().get_skill(skill_id)
        if skill is None:
            raise PersonalRuntimeError("NOT_FOUND", skill_id)
        _assert_user_scope(skill, user)
        return await get_personal_runtime().run_skill(
            skill_id,
            (req.params if req else {}),
            task_id=req.task_id if req else None,
            trace_id=req.trace_id if req else None,
        )
    except PersonalRuntimeError as exc:
        raise _error(exc) from exc


@router.post("/api/v1/skills/{skill_id}/rollback")
async def rollback_skill(
    skill_id: str,
    req: SkillRollbackRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    try:
        skill = await get_personal_runtime().get_skill(skill_id)
        if skill is None:
            raise PersonalRuntimeError("NOT_FOUND", skill_id)
        _assert_user_scope(skill, user)
        return await get_personal_runtime().rollback_skill(skill_id, req.version)
    except PersonalRuntimeError as exc:
        raise _error(exc) from exc


@router.post("/api/v1/skills/{skill_id}/deprecate")
async def deprecate_skill(
    skill_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    try:
        skill = await get_personal_runtime().get_skill(skill_id)
        if skill is None:
            raise PersonalRuntimeError("NOT_FOUND", skill_id)
        _assert_user_scope(skill, user)
        return await get_personal_runtime().deprecate_skill(skill_id)
    except PersonalRuntimeError as exc:
        raise _error(exc) from exc


@router.delete("/api/v1/skills/{skill_id}")
async def delete_skill(
    skill_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    try:
        skill = await get_personal_runtime().get_skill(skill_id)
        if skill is None:
            raise PersonalRuntimeError("NOT_FOUND", skill_id)
        _assert_user_scope(skill, user)
        return await get_personal_runtime().deprecate_skill(skill_id)
    except PersonalRuntimeError as exc:
        raise _error(exc) from exc


@router.get("/api/v1/continuity")
async def get_continuity(
    workspace_id: str | None = None, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    """Build a projection from shared history/task state and durable personal refs."""
    from server.task_store import task_store
    from veya.history_store import default_history_store

    uid = str(user["user_id"])
    workspace = workspace_id or _workspace(None)
    sessions = await default_history_store().list_sessions(user_id=uid, limit=20)
    session_ids = {str(item["sid"]) for item in sessions}
    tasks = [
        task.to_dict()
        for task in task_store.list(workspace_id=workspace, limit=200)
        if task.session_id in session_ids
    ]
    active = [
        task for task in tasks if task.get("status") in {"pending", "running", "waiting_approval"}
    ]
    paused = [task for task in tasks if task.get("status") in {"failed", "cancelled"}]
    memories = await get_personal_runtime().search_memory(
        "", scope_type="workspace", scope_id=workspace, limit=20
    )
    user_memories = await get_personal_runtime().search_memory(
        "", scope_type="user", scope_id=uid, limit=20
    )
    user_skills = await get_personal_runtime().search_skills(
        "", scope_type="user", scope_id=uid, limit=20
    )
    skills = await get_personal_runtime().search_skills(
        "", scope_type="workspace", scope_id=workspace, limit=20
    )
    snapshot = {
        "recent_sessions": sessions,
        "active_tasks": active,
        "paused_tasks": paused,
        "recent_decisions": [
            m for m in [*memories, *user_memories] if m.get("memory_type") == "decision"
        ],
        "recent_artifacts": [],
        "open_questions": [],
        "unfinished_work": [
            {"task_id": t.get("id"), "objective": t.get("objective"), "status": t.get("status")}
            for t in [*active, *paused]
        ],
        "latest_project_state": {"workspace_id": workspace},
        "memory_refs": [m["id"] for m in [*memories, *user_memories]],
        "skill_refs": [s["skill_id"] for s in [*user_skills, *skills]],
    }
    return await get_personal_runtime().save_continuity(
        snapshot,
        user_id=uid,
        workspace_id=workspace,
        source_event_cursor=str(len(sessions) + len(tasks)),
    )


class ContinueRequest(BaseModel):
    text: str | None = Field(None, min_length=1)
    workspace_id: str | None = None
    max_rounds: int | None = Field(None, ge=1, le=100)


@router.post("/api/v1/tasks/{task_id}/continue")
async def continue_task(
    task_id: str,
    req: ContinueRequest | None = None,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    snapshot = await get_continuity(workspace_id=req.workspace_id if req else None, user=user)
    task = next(
        (
            item
            for item in snapshot.get("active_tasks", []) + snapshot.get("paused_tasks", [])
            if item.get("id") == task_id
        ),
        None,
    )
    if task is None:
        raise HTTPException(
            status_code=404, detail="task is not in the current user's continuity projection"
        )
    if not req or not req.text:
        return {"task_id": task_id, "resumed": True, "status": "ready", "continuity": snapshot}
    from server.coordinator_master import master_coordinator

    # This is an explicit continuation request.  The structured facts are
    # supplied to MasterAgent as context; no keyword router chooses a tool.
    context = {
        "task": task,
        "unfinished_work": snapshot.get("unfinished_work", []),
        "memory_refs": snapshot.get("memory_refs", []),
        "skill_refs": snapshot.get("skill_refs", []),
    }
    prompt = f"[CONTINUATION CONTEXT]\n{context}\n[/CONTINUATION CONTEXT]\n\n{req.text}"
    result = await master_coordinator.chat_stream(
        prompt, session_id=task.get("session_id"), max_rounds=req.max_rounds
    )
    await get_personal_runtime().record_event(
        "continuity.resumed",
        {"task_id": task_id, "snapshot_id": snapshot.get("id")},
        session_id=task.get("session_id"),
        task_id=task_id,
        workspace_id=snapshot.get("latest_project_state", {}).get("workspace_id"),
    )
    return {"task_id": task_id, "resumed": True, "continuity": snapshot, "result": result}


@router.get("/api/v1/learning/candidates")
async def learning_candidates(
    scope: str | None = None,
    status: str | None = None,
    limit: int = 50,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    if scope in {None, "", "default", "user"}:
        scope = f"user:{user['user_id']}"
    items = await get_personal_runtime().list_learning(scope=scope, status=status, limit=limit)
    return {"candidates": items, "count": len(items)}


@router.get("/api/v1/learning/{learning_id}")
async def learning_detail(
    learning_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    item = await get_personal_runtime().get_learning(learning_id)
    if item is None:
        raise HTTPException(status_code=404, detail="learning candidate not found")
    if (
        str(item.get("scope", "")).startswith("user:")
        and item["scope"] != f"user:{user['user_id']}"
    ):
        raise HTTPException(status_code=404, detail="learning candidate not found")
    return item


@router.get("/health/personal-runtime")
async def personal_runtime_health() -> dict[str, Any]:
    return await get_personal_runtime().health()


@router.get("/api/v1/personal-runtime/metrics")
async def personal_runtime_metrics() -> dict[str, Any]:
    return await get_personal_runtime().personal_metrics()
