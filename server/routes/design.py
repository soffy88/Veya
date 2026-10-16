"""Design Mode capture endpoint.

Receives a DesignCapture from the browser panel, stores it as an artifact,
and posts a composed message to the current session/goal run.
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from server import auth as auth_mod
from server.events import append_canonical_event
from server.session_identity import new_session_id
from veya.history_store import default_history_store

router = APIRouter(prefix="/api/v1/design", tags=["design"])

MAX_OUTER_HTML = 8 * 1024  # 8KB


class ComputedStyles(BaseModel):
    boxModel: dict[str, str] = Field(default_factory=dict)
    display: str = ""
    flex: dict[str, str] | None = None
    grid: dict[str, str] | None = None
    font: dict[str, str] = Field(default_factory=dict)
    color: dict[str, str] = Field(default_factory=dict)
    position: dict[str, str] = Field(default_factory=dict)


class BoundingBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class SourceLocation(BaseModel):
    type: Literal["veya", "react", "generic", "unknown"] = "unknown"
    file: str = ""
    line: int = 0
    column: int | None = None
    rawAttribute: str | None = None


class DesignCapture(BaseModel):
    outerHTML: str
    selectorPath: str
    computedStyles: ComputedStyles
    boundingBox: BoundingBox
    elementScreenshotBase64: str
    pageUrl: str
    pageTitle: str
    sourceLocation: SourceLocation | None = None

    @field_validator("outerHTML", mode="before")
    @classmethod
    def trim_outer_html(cls, v: str) -> str:
        if len(v) > MAX_OUTER_HTML:
            return v[:MAX_OUTER_HTML] + "\n…[trimmed]"
        return v

    @field_validator("elementScreenshotBase64", mode="before")
    @classmethod
    def validate_base64(cls, v: str) -> str:
        try:
            base64.b64decode(v, validate=True)
        except Exception:
            raise ValueError("elementScreenshotBase64 must be valid base64")
        return v


class DesignCaptureRequest(BaseModel):
    capture: DesignCapture
    instruction: str = ""
    session_id: str | None = None
    task_id: str | None = None


class DesignCaptureResponse(BaseModel):
    status: Literal["sent"]
    artifact_path: str
    message_id: str


async def _get_current_session_id(request: DesignCaptureRequest, user: dict[str, Any]) -> str:
    """Resolve session_id: request > active session > new session."""
    if request.session_id:
        return request.session_id
    store = default_history_store()
    sessions = await store.list_sessions(user_id=user["user_id"], limit=1)
    if sessions:
        return sessions[0]["sid"]
    return new_session_id()


@router.post("/capture", response_model=DesignCaptureResponse)
async def capture_design_element(
    request: DesignCaptureRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> DesignCaptureResponse:
    """Store design capture as artifact and post message to session."""
    session_id = await _get_current_session_id(request, user)
    task_id = request.task_id or f"design-{uuid.uuid4().hex[:8]}"

    store = default_history_store()
    await store.save(session_id, [])  # ensure session exists

    # Build artifact directory
    project_root = Path.cwd()
    artifact_dir = project_root / ".veya" / "runs" / task_id / "design_captures"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    artifact_id = uuid.uuid4().hex
    artifact_path = artifact_dir / f"{artifact_id}.json"

    # Store capture as JSON artifact
    artifact_data = {
        "capture": request.capture.model_dump(),
        "instruction": request.instruction,
        "session_id": session_id,
        "task_id": task_id,
        "user_id": user["user_id"],
    }
    artifact_path.write_text(json.dumps(artifact_data, ensure_ascii=False, indent=2))

    # Compose user message
    cap = request.capture
    src = cap.sourceLocation
    source_str = (
        f"{src.file}:{src.line}" if src and src.type != "unknown" and src.file else "unknown"
    )

    message_parts = [
        "[Design Mode Capture]",
        f"Page: {cap.pageTitle} ({cap.pageUrl})",
        f"Selector: {cap.selectorPath}",
        f"Source: {source_str}",
        "",
        cap.outerHTML,
        "",
        f"User instruction: {request.instruction or '(none)'}",
    ]
    composed_message = "\n".join(message_parts)

    # Append to session history
    messages = await store.load(session_id, user_id=user["user_id"])
    new_message = {
        "role": "user",
        "content": composed_message,
        "metadata": {
            "design_capture": True,
            "artifact_path": str(artifact_path.relative_to(project_root)),
            "task_id": task_id,
        },
    }
    messages.append(new_message)
    await store.save(session_id, messages, user_id=user["user_id"])

    # Fire canonical event
    append_canonical_event(
        "design.capture_sent",
        {
            "session_id": session_id,
            "task_id": task_id,
            "artifact_path": str(artifact_path.relative_to(project_root)),
            "selector": cap.selectorPath,
            "page_url": cap.pageUrl,
            "source_type": src.type if src else "unknown",
        },
        actor=user["user_id"],
        session_id=session_id,
        task_id=task_id,
    )

    return DesignCaptureResponse(
        status="sent",
        artifact_path=str(artifact_path.relative_to(project_root)),
        message_id=artifact_id,
    )


__all__ = ["router"]
