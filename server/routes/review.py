"""server.routes.review — ORCA-W3 review endpoints.

Three feature groups:
  1. Line annotations  — /api/v1/review/annotations/{goal_id}/*
  2. AI attribution    — /api/v1/review/attribution/*
  3. Fix broken checks — /api/v1/review/fix-checks

All routes are additive; existing server behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod
from server.goal_run.agent_mailbox import AgentMailbox
from server.goal_run.ai_attribution import export_all, get_file_attribution, revert_to_human
from server.goal_run.annotations import (
    batch_send,
    create_annotation,
    list_annotations,
    resolve_annotation,
)

router = APIRouter(tags=["review"], dependencies=[Depends(auth_mod.require_user)])

_DEFAULT_PROJECT_ROOT = os.environ.get("VEYA_WORKSPACE") or os.getcwd()
_GH_TIMEOUT = 30
_MAX_LOG_CHARS = 8_000


def _project_root(repo_path: str | None = None) -> str:
    if repo_path and Path(repo_path).is_dir():
        return str(Path(repo_path).resolve())
    return _DEFAULT_PROJECT_ROOT


# ── 1. Line Annotations ──────────────────────────────────────────────────────


class CreateAnnotationRequest(BaseModel):
    worktree: str = ""
    file: str
    line: int
    side: Literal["left", "right"] = "right"
    body: str


class SendAnnotationsRequest(BaseModel):
    recipient: str  # session_id or goal_id of the target agent
    repo_path: str | None = None


@router.post("/api/v1/review/annotations/{goal_id}")
async def annotation_create(
    goal_id: str,
    req: CreateAnnotationRequest,
    repo_path: str | None = None,
) -> dict[str, Any]:
    """Create a new line annotation for a goal_run diff."""
    root = _project_root(repo_path)
    ann = create_annotation(
        root,
        goal_id,
        worktree=req.worktree,
        file=req.file,
        line=req.line,
        side=req.side,  # type: ignore[arg-type]
        body=req.body,
    )
    return {"ok": True, "annotation": ann.to_dict()}


@router.get("/api/v1/review/annotations/{goal_id}")
async def annotation_list(
    goal_id: str,
    status: str | None = None,
    repo_path: str | None = None,
) -> dict[str, Any]:
    """List annotations for a goal_run, optionally filtered by status."""
    root = _project_root(repo_path)
    filtered_status: Any = status if status in ("open", "resolved") else None
    annotations = list_annotations(root, goal_id, status=filtered_status)
    return {"annotations": [a.to_dict() for a in annotations]}


@router.post("/api/v1/review/annotations/{goal_id}/resolve/{annotation_id}")
async def annotation_resolve(
    goal_id: str,
    annotation_id: str,
    repo_path: str | None = None,
) -> dict[str, Any]:
    """Resolve a single annotation."""
    root = _project_root(repo_path)
    found = resolve_annotation(root, goal_id, annotation_id)
    if not found:
        raise HTTPException(status_code=404, detail="annotation not found or already resolved")
    return {"ok": True, "resolved": annotation_id}


@router.post("/api/v1/review/annotations/{goal_id}/send")
async def annotation_send(
    goal_id: str,
    req: SendAnnotationsRequest,
) -> dict[str, Any]:
    """Batch all OPEN annotations into a FOLLOW_UP message to the target recipient.

    Marks sent annotations as resolved. Returns count and message_id.
    """
    root = _project_root(req.repo_path)
    mailbox = AgentMailbox()
    result = batch_send(root, goal_id, recipient=req.recipient, mailbox=mailbox)
    return result


# ── 2. AI Attribution ────────────────────────────────────────────────────────


@router.get("/api/v1/review/attribution/export")
async def attribution_export(repo_path: str | None = None) -> dict[str, Any]:
    """Full JSON dump of all AI attribution ledgers."""
    root = _project_root(repo_path)
    ledgers = export_all(root)
    return {"ledgers": ledgers}


@router.get("/api/v1/review/attribution/file")
async def attribution_file(
    path: str,
    total_lines: int = 0,
    repo_path: str | None = None,
) -> dict[str, Any]:
    """Per-line attribution (ai|human) for a given file path.

    `path` must be URL-encoded. `total_lines` is the number of lines in the
    file; 0 means we attempt to count from disk.
    """
    root = _project_root(repo_path)
    file_path = urllib.parse.unquote(path)
    if total_lines <= 0:
        abs_path = Path(root) / file_path
        if abs_path.exists():
            try:
                total_lines = sum(1 for _ in abs_path.open("rb"))
            except OSError:
                total_lines = 0
    if total_lines <= 0:
        return {"file": file_path, "lines": []}
    lines = get_file_attribution(root, file_path, total_lines)
    return {"file": file_path, "lines": lines}


@router.post("/api/v1/review/attribution/revert")
async def attribution_revert(
    path: str,
    line_start: int,
    line_end: int,
    repo_path: str | None = None,
) -> dict[str, Any]:
    """Revert a line range to human attribution (human edited those lines)."""
    root = _project_root(repo_path)
    file_path = urllib.parse.unquote(path)
    affected = revert_to_human(root, file_path, line_start, line_end)
    return {"ok": True, "affected_entries": affected}


# ── 3. Fix Broken Checks ─────────────────────────────────────────────────────


class FixChecksRequest(BaseModel):
    repo_path: str | None = None
    branch: str | None = None
    pr: str | None = None  # PR number as string


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _fetch_failing_checks(branch: str, repo_path: str) -> tuple[list[str], str]:
    """Return (failing_check_names, trimmed_log_text). Raises on gh error."""
    # List runs for the branch
    list_cmd = [
        "gh",
        "run",
        "list",
        "--branch",
        branch,
        "--json",
        "databaseId,name,status,conclusion",
        "--limit",
        "5",
    ]
    r = subprocess.run(list_cmd, capture_output=True, text=True, timeout=_GH_TIMEOUT, cwd=repo_path)
    if r.returncode != 0:
        raise RuntimeError(f"gh run list failed: {r.stderr[:500]}")

    import json as _json

    runs = _json.loads(r.stdout or "[]")
    failing_names: list[str] = []
    log_parts: list[str] = []

    for run in runs:
        if run.get("conclusion") in ("failure", "timed_out", "startup_failure", "action_required"):
            failing_names.append(run.get("name", str(run.get("databaseId", ""))))
            run_id = str(run.get("databaseId", ""))
            if run_id:
                log_cmd = ["gh", "run", "view", run_id, "--log-failed"]
                log_r = subprocess.run(
                    log_cmd, capture_output=True, text=True, timeout=_GH_TIMEOUT, cwd=repo_path
                )
                if log_r.returncode == 0 and log_r.stdout.strip():
                    log_parts.append(f"=== {run.get('name', run_id)} ===\n{log_r.stdout[:2000]}")

    log_text = "\n\n".join(log_parts)[:_MAX_LOG_CHARS]
    return failing_names, log_text


@router.post("/api/v1/review/fix-checks")
async def fix_checks(req: FixChecksRequest) -> dict[str, Any]:
    """Fetch failing CI checks for a branch/PR and start a GoalRun to fix them.

    - Uses `gh` CLI with user's existing auth (no token stored).
    - Returns `{"status": "gh_unavailable"}` if gh is not installed.
    - Returns `{"status": "no_failing_checks"}` if all checks are green.
    - Otherwise starts a GoalRun and returns `{"status": "started", "goal_id": ...}`.
    """
    if not _gh_available():
        return {"status": "gh_unavailable", "message": "gh CLI not found in PATH"}

    root = _project_root(req.repo_path)

    # Resolve branch from PR if needed
    branch = req.branch or ""
    if not branch and req.pr:
        try:
            r = subprocess.run(
                ["gh", "pr", "view", str(req.pr), "--json", "headRefName"],
                capture_output=True,
                text=True,
                timeout=_GH_TIMEOUT,
                cwd=root,
            )
            if r.returncode == 0:
                import json as _json

                data = _json.loads(r.stdout or "{}")
                branch = data.get("headRefName", "")
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass

    if not branch:
        # Fall back to current branch
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=root,
            )
            branch = r.stdout.strip() if r.returncode == 0 else ""
        except (subprocess.TimeoutExpired, OSError):
            pass

    if not branch:
        raise HTTPException(status_code=400, detail="could not determine branch name")

    try:
        failing_names, log_text = _fetch_failing_checks(branch, root)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"gh CLI error: {exc}") from exc

    if not failing_names:
        return {"status": "no_failing_checks", "branch": branch}

    # Start a GoalRun to fix the failing checks
    names_summary = ", ".join(failing_names[:5])
    objective = (
        f"Fix failing CI checks on branch `{branch}`: {names_summary}.\n\n"
        f"Failure logs (trimmed):\n{log_text}"
    )
    try:
        from server.goal_run.runner import project_run_goal

        response = await asyncio.wait_for(
            project_run_goal(root, objective, wait=False),
            timeout=10,
        )
        return {
            "status": "started",
            "goal_id": response.goal_id,
            "branch": branch,
            "failing_checks": failing_names,
        }
    except TimeoutError:
        return {
            "status": "started",
            "goal_id": None,
            "branch": branch,
            "failing_checks": failing_names,
            "note": "GoalRun enqueued (timeout waiting for goal_id)",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"GoalRun start failed: {exc}") from exc
