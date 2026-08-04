"""GET /projects — project list; POST /projects — create project."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/projects", tags=["projects"])

_PROJECT_COLORS = ["#4A90D9", "#7B68EE", "#50C878", "#FF7F50", "#FFD700"]

# In-memory project store: id → project dict
_projects: dict[str, dict] = {}


class ProjectCreateRequest(BaseModel):
    name: str = "New Project"
    icon: str = ""


@router.get("")
async def list_projects() -> dict[str, Any]:
    from server.routes.session import _sessions

    # Count sessions per project
    session_counts: dict[str, int] = {}
    for s in _sessions.values():
        proj = s.get("project", "default")
        session_counts[proj] = session_counts.get(proj, 0) + 1

    # Merge explicit projects with implicit ones from sessions
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


@router.post("")
async def create_project(req: ProjectCreateRequest) -> dict[str, Any]:
    import uuid

    pid = str(uuid.uuid4())[:8]
    icon = req.icon or req.name[0].upper() if req.name else "P"
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
