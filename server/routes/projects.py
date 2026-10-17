# 3O-IO-ALLOW — server/routes/projects.py: Project Brain REST projection, brief/tasks/messages endpoints
"""GET /projects — project list; POST /projects — create project.

W16 Project Brain (Spec §6 & §3):
  When VEYA_PROJECT_BRAIN is OFF:
    Preserves legacy in-memory dictionary behavior.
  When VEYA_PROJECT_BRAIN is ON:
    Projects from BotRegistry (kind="project", bot_id="proj-<id>", workspace_path=project_root).
    Endpoints (support both /projects and /api/v1/projects):
      GET  /projects                     List user's projects
      POST /projects                     Create project (专属 bot + .veya-project 布局 + 空简报)
      GET  /projects/{id}                Get project metadata
      GET  /projects/{id}/brief          Read brief (JSON + markdown, 404-not-403)
      PUT  /projects/{id}/brief          Update brief sections or raw markdown
      GET  /projects/{id}/messages       Project chat history (proxies get_bot_messages)
      POST /projects/{id}/messages       Project chat send (proxies post_bot_message)
      GET  /projects/{id}/tasks          Task cards (status, evidence, report, repairs)
      POST /projects/{id}/tasks/{tid}/{action}  Task actions (cancel/pause/resume forward to /supervised)
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.feature_flags import enabled as flag_enabled
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.project_brain.brief import (
    ProjectBrief,
    load_brief,
    render_brief,
    save_brief,
    update_brief_section,
)
from server.project_brain.tasks import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_RUNNING,
    get_task,
    list_tasks,
    upsert_task,
)
from server.project_store import ProjectStore

router = APIRouter(tags=["projects"])

_PROJECT_COLORS = ["#4A90D9", "#7B68EE", "#50C878", "#FF7F50", "#FFD700"]

# In-memory project store for legacy fallback when VEYA_PROJECT_BRAIN is OFF: id → project dict
_projects: dict[str, dict] = {}


def project_brain_enabled() -> bool:
    return flag_enabled("VEYA_PROJECT_BRAIN")


class ProjectCreateRequest(BaseModel):
    name: str = "New Project"
    icon: str = ""
    project_root: str = ""
    workspace_path: str = ""
    description: str = ""


class BriefUpdateRequest(BaseModel):
    markdown: str | None = None
    section: str | None = None
    content: Any = None
    sections: dict[str, Any] | None = None
    rules: list[Any] | None = None


class ProjectMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)
    workspace_id: str | None = Field(default=None, max_length=512)
    message_id: str | None = Field(default=None, max_length=200)


def _resolve_project_bot(
    registry: BotRegistry,
    project_id: str,
    *,
    owner_id: str = "",
) -> BotSpec | None:
    """Find a project bot by id or bot_id, enforcing 404-not-403 owner privacy."""
    target_id = project_id.strip()
    spec = registry.get(target_id)
    if spec is None:
        spec = registry.get(f"proj-{target_id}")
    if spec is None and target_id.startswith("proj-"):
        spec = registry.get(target_id.removeprefix("proj-"))

    if spec is None:
        return None
    if getattr(spec, "kind", "bot") != "project":
        return None

    # Owner isolation (§8: 404-not-403)
    if owner_id:
        if owner_id == "anonymous":
            if spec.owner_id not in ("", "anonymous", "system"):
                return None
        elif spec.owner_id and spec.owner_id != owner_id:
            return None
    elif spec.owner_id and spec.owner_id not in ("anonymous", "system"):
        return None

    return spec


def _require_project_bot(
    project_id: str,
    *,
    owner_id: str = "",
    registry: BotRegistry | None = None,
) -> BotSpec:
    reg = registry or BotRegistry()
    spec = _resolve_project_bot(reg, project_id, owner_id=owner_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")
    return spec


def _project_view(spec: BotSpec, idx: int = 0) -> dict[str, Any]:
    pid = spec.bot_id.removeprefix("proj-")
    return {
        "id": pid,
        "project_id": pid,
        "bot_id": spec.bot_id,
        "name": spec.name or pid,
        "icon": spec.avatar or (spec.name[0].upper() if spec.name else "P"),
        "color": _PROJECT_COLORS[idx % len(_PROJECT_COLORS)],
        "project_root": spec.workspace_path,
        "workspace_path": spec.workspace_path,
        "owner_id": spec.owner_id,
        "description": spec.description,
        "session_count": 0,
        "created_at": spec.created_at,
        "updated_at": spec.updated_at,
    }


# ── Project List & Create ──────────────────────────────────────────────


@router.get("/projects")
@router.get("/api/v1/projects")
async def list_projects(
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    if not project_brain_enabled():
        from server.routes.session import _sessions

        # Legacy in-memory behavior
        session_counts: dict[str, int] = {}
        for s in _sessions.values():
            proj = s.get("project", "default")
            session_counts[proj] = session_counts.get(proj, 0) + 1

        all_ids: set[str] = set(_projects.keys())
        if not all_ids and not session_counts:
            all_ids.add("default")
        all_ids |= set(session_counts.keys())

        projects = []
        for i, pid in enumerate(all_ids):
            if pid in _projects:
                p = dict(_projects[pid])
                p["session_count"] = session_counts.get(pid, 0)
                projects.append(p)
            else:
                projects.append(
                    {
                        "id": pid,
                        "name": pid.replace("-", " ").replace("_", " ").title(),
                        "icon": pid[0].upper(),
                        "color": _PROJECT_COLORS[i % len(_PROJECT_COLORS)],
                        "session_count": session_counts.get(pid, 0),
                    }
                )

        return {"projects": projects}

    # Project Brain enabled: project from BotRegistry
    registry = BotRegistry()
    owner_id = str((user or {}).get("user_id") or "anonymous")
    all_bots = registry.list()
    projects = []
    idx = 0
    for spec in all_bots:
        if getattr(spec, "kind", "bot") != "project":
            continue
        if owner_id == "anonymous":
            if spec.owner_id not in ("", "anonymous", "system"):
                continue
        elif spec.owner_id and spec.owner_id != owner_id:
            continue
        projects.append(_project_view(spec, idx))
        idx += 1

    return {"projects": projects}


@router.post("/projects")
@router.post("/api/v1/projects")
async def create_project(
    req: ProjectCreateRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    if not project_brain_enabled():
        pid = str(uuid.uuid4())[:8]
        icon = req.icon or (req.name[0].upper() if req.name else "P")
        idx = len(_projects)
        project = {
            "id": pid,
            "name": req.name,
            "icon": icon,
            "color": _PROJECT_COLORS[idx % len(_PROJECT_COLORS)],
            "session_count": 0,
        }
        _projects[pid] = project
        return {"project": project}

    # Project Brain enabled: create dedicated project bot + .veya-project layout + empty brief
    pid = str(uuid.uuid4())[:8]
    bot_id = f"proj-{pid}"
    owner_id = str((user or {}).get("user_id") or "anonymous")
    if not user or owner_id == "anonymous":
        raise HTTPException(
            status_code=401,
            detail="需要登录才能创建项目",
        )

    raw_root = req.project_root or req.workspace_path
    if raw_root:
        root_path = Path(raw_root).expanduser().resolve()
    else:
        base_dir = (
            Path(os.environ.get("VEYA_PROJECTS_ROOT", Path.home() / ".veya" / "projects"))
            .expanduser()
            .resolve()
        )
        root_path = base_dir / pid

    root_path.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(root_path)
    store.ensure_layout()

    import datetime

    now_iso = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    brief = ProjectBrief(
        goal_scope=f"{req.name} 项目目标与范围",
        updated_at=now_iso,
    )
    save_brief(root_path, brief)
    store.append_log(f"项目 {req.name} 创建。")

    icon = req.icon or (req.name[0].upper() if req.name else "P")
    spec = BotSpec(
        bot_id=bot_id,
        owner_id=owner_id,
        name=req.name,
        description=req.description or f"Project Brain for {req.name}",
        kind="project",
        workspace_path=str(root_path),
        avatar=icon,
        created_at=now_iso,
        updated_at=now_iso,
    )
    BotRegistry().register(spec)

    project = _project_view(spec, len(BotRegistry().list()))
    return {"project": project}


# ── Project Detail & Brief ─────────────────────────────────────────────


@router.get("/projects/{project_id}")
@router.get("/api/v1/projects/{project_id}")
async def get_project(
    project_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    if not project_brain_enabled():
        if project_id in _projects:
            return {"project": _projects[project_id]}
        raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")

    spec = _require_project_bot(
        project_id, owner_id=str((user or {}).get("user_id") or "anonymous")
    )
    return {"project": _project_view(spec)}


@router.get("/projects/{project_id}/brief")
@router.get("/api/v1/projects/{project_id}/brief")
async def get_project_brief(
    project_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    if not project_brain_enabled():
        raise HTTPException(status_code=404, detail="project brain is not enabled")

    spec = _require_project_bot(
        project_id, owner_id=str((user or {}).get("user_id") or "anonymous")
    )
    brief = load_brief(spec.workspace_path)
    md = render_brief(brief)
    return {
        "project_id": spec.bot_id.removeprefix("proj-"),
        "bot_id": spec.bot_id,
        "brief": brief.to_dict(),
        "markdown": md,
    }


@router.put("/projects/{project_id}/brief")
@router.put("/api/v1/projects/{project_id}/brief")
async def update_project_brief(
    project_id: str,
    req: BriefUpdateRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    if not project_brain_enabled():
        raise HTTPException(status_code=404, detail="project brain is not enabled")

    spec = _require_project_bot(
        project_id, owner_id=str((user or {}).get("user_id") or "anonymous")
    )
    root = spec.workspace_path

    if req.markdown is not None:
        ProjectStore(root).write_state(req.markdown)
        brief = load_brief(root)
    else:
        brief = load_brief(root)
        if req.section is not None:
            brief = update_brief_section(root, req.section, req.content)
        if req.sections is not None:
            for s_name, s_val in req.sections.items():
                brief = update_brief_section(root, s_name, s_val)
        if req.rules is not None:
            brief = update_brief_section(root, "rules", req.rules)

    ProjectStore(root).append_log("项目简报更新。")
    return {
        "project_id": spec.bot_id.removeprefix("proj-"),
        "bot_id": spec.bot_id,
        "brief": brief.to_dict(),
        "markdown": render_brief(brief),
    }


# ── Project Messages (Chat Proxy) ──────────────────────────────────────


@router.get("/projects/{project_id}/messages")
@router.get("/api/v1/projects/{project_id}/messages")
async def get_project_messages(
    project_id: str,
    include_hidden: bool = False,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    if not project_brain_enabled():
        raise HTTPException(status_code=404, detail="project brain is not enabled")

    spec = _require_project_bot(project_id, owner_id=str(user.get("user_id") or "anonymous"))
    from server.routes.bot_team import get_bot_messages

    return await get_bot_messages(bot_id=spec.bot_id, include_hidden=include_hidden, user=user)


@router.post("/projects/{project_id}/messages")
@router.post("/api/v1/projects/{project_id}/messages")
async def post_project_message(
    project_id: str,
    req: ProjectMessageRequest,
    async_mode: bool = Query(False, alias="async"),
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> Any:
    if not project_brain_enabled():
        raise HTTPException(status_code=404, detail="project brain is not enabled")

    spec = _require_project_bot(project_id, owner_id=str(user.get("user_id") or "anonymous"))
    from server.routes.bot_team import BotMessageRequest, post_bot_message

    bot_req = BotMessageRequest(
        text=req.text,
        workspace_id=req.workspace_id or spec.workspace_path,
        message_id=req.message_id,
    )
    return await post_bot_message(bot_id=spec.bot_id, req=bot_req, async_mode=async_mode, user=user)


# ── Project Tasks ──────────────────────────────────────────────────────


@router.get("/projects/{project_id}/tasks")
@router.get("/api/v1/projects/{project_id}/tasks")
async def get_project_tasks(
    project_id: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    if not project_brain_enabled():
        raise HTTPException(status_code=404, detail="project brain is not enabled")

    spec = _require_project_bot(
        project_id, owner_id=str((user or {}).get("user_id") or "anonymous")
    )
    tasks = list_tasks(spec.workspace_path)
    tasks_list = []
    for t in tasks:
        tdict = t.to_dict()
        if t.binding_id:
            from server.supervisor import binding as supervisor_binding

            sb = supervisor_binding.get_binding(t.binding_id)
            if sb is not None:
                tdict["report_seq"] = sb.report_seq
                tdict["max_repairs"] = sb.max_repairs
                tdict["repairs_made"] = sb.nudges_sent
                tdict["remaining_repairs"] = max(0, sb.max_repairs - sb.nudges_sent)
                if sb.last_report_at:
                    tdict["last_report_at"] = sb.last_report_at
        tasks_list.append(tdict)

    return {"project_id": spec.bot_id.removeprefix("proj-"), "tasks": tasks_list}


@router.post("/projects/{project_id}/tasks/{task_id}/{action}")
@router.post("/api/v1/projects/{project_id}/tasks/{task_id}/{action}")
async def project_task_action(
    project_id: str,
    task_id: str,
    action: str,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    if not project_brain_enabled():
        raise HTTPException(status_code=404, detail="project brain is not enabled")

    action = action.lower().strip()
    if action not in ("cancel", "pause", "resume"):
        raise HTTPException(status_code=400, detail=f"invalid action {action!r}")

    spec = _require_project_bot(
        project_id, owner_id=str((user or {}).get("user_id") or "anonymous")
    )
    task = get_task(spec.workspace_path, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found in project")

    if action == "cancel":
        if task.binding_id:
            try:
                from server.routes.supervised import cancel_supervised

                cancel_supervised(bot_id=spec.bot_id, binding_id=task.binding_id, user=user)
            except Exception:
                pass
        task.status = TASK_STATUS_CANCELLED
    elif action == "pause":
        if task.binding_id:
            try:
                from server.routes.supervised import pause_supervised

                pause_supervised(bot_id=spec.bot_id, binding_id=task.binding_id, user=user)
            except Exception:
                pass
    elif action == "resume":
        if task.binding_id:
            try:
                from server.routes.supervised import resume_supervised

                resume_supervised(bot_id=spec.bot_id, binding_id=task.binding_id, user=user)
            except Exception:
                pass
        task.status = TASK_STATUS_RUNNING

    upsert_task(spec.workspace_path, task)
    return {
        "task_id": task_id,
        "action": action,
        "status": task.status,
        "task": task.to_dict(),
    }
