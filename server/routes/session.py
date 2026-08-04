"""Session management routes: create, fork, share, compact, undo."""

from __future__ import annotations

import copy
import pathlib
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/session", tags=["session"])

# In-memory session store (replace with obase persistence in P5)
_sessions: dict[str, dict] = {}

# Redacted share payloads keyed by share_token
_shares: dict[str, dict] = {}

# Per-session undo stacks: session_id → list of turns, each turn = list of {path, content}
_undo_stacks: dict[str, list[list[dict]]] = {}


class SessionCreateRequest(BaseModel):
    persona: str = "build"
    project: str = "default"
    config: dict[str, Any] = {}


class SessionForkRequest(BaseModel):
    label: str = ""


def push_file_snapshot(session_id: str, path: str) -> None:
    """Save current file content to undo stack before a write operation."""
    if not session_id:
        return
    p = pathlib.Path(path)
    content = p.read_text(errors="replace") if p.exists() else None
    if session_id not in _undo_stacks:
        _undo_stacks[session_id] = []
    # Start a new turn if the stack is empty or we need a fresh turn
    if not _undo_stacks[session_id]:
        _undo_stacks[session_id].append([])
    _undo_stacks[session_id][-1].append({"path": str(p.resolve()), "content": content})


def open_undo_turn(session_id: str) -> None:
    """Start a new undo turn boundary for this session."""
    if session_id:
        _undo_stacks.setdefault(session_id, []).append([])


@router.post("")
async def create_session(req: SessionCreateRequest) -> dict[str, Any]:
    import datetime

    sid = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat() + "Z"
    _sessions[sid] = {
        "id": sid,
        "persona": req.persona,
        "project": req.project,
        "config": req.config,
        "messages": [],
        "cost_usd": 0.0,
        "created_at": now,
        "updated_at": now,
    }
    return {"id": sid, "status": "created", "project": req.project}


@router.get("/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    return s


@router.post("/{session_id}/fork")
async def fork_session(session_id: str, req: SessionForkRequest) -> dict[str, Any]:
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    new_id = str(uuid.uuid4())
    _sessions[new_id] = {**copy.deepcopy(s), "id": new_id, "label": req.label}
    return {"session_id": new_id, "forked_from": session_id}


@router.post("/{session_id}/compact")
async def compact_session(session_id: str) -> dict[str, Any]:
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")

    msgs = s.get("messages", [])
    _COMPACT_THRESHOLD = 10
    _KEEP_RECENT = 5

    if len(msgs) <= _COMPACT_THRESHOLD:
        return {
            "session_id": session_id,
            "status": "skipped",
            "reason": f"messages ({len(msgs)}) ≤ threshold ({_COMPACT_THRESHOLD})",
            "messages_kept": len(msgs),
        }

    # Build a summary of the older messages
    old_msgs = msgs[:-_KEEP_RECENT]
    old_token_estimate = sum(len(m.get("content", "")) for m in old_msgs)
    summary_text = (
        f"[Compacted: {len(old_msgs)} earlier messages, ~{old_token_estimate} chars] "
        + " / ".join((m.get("content") or "")[:60].replace("\n", " ") for m in old_msgs[:3])
    )

    summary_msg = {"role": "assistant", "content": summary_text}
    s["messages"] = [summary_msg, *msgs[-_KEEP_RECENT:]]
    s["compacted"] = True

    return {
        "session_id": session_id,
        "status": "compacted",
        "messages_before": len(msgs),
        "messages_after": len(s["messages"]),
        "chars_removed": old_token_estimate,
    }


@router.post("/{session_id}/undo")
async def undo_session(session_id: str) -> dict[str, Any]:
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")

    files_restored = []

    # Restore file snapshots from last undo turn
    stacks = _undo_stacks.get(session_id, [])
    if stacks:
        turn_snapshots = stacks.pop()
        for snap in reversed(turn_snapshots):
            p = pathlib.Path(snap["path"])
            content = snap["content"]
            if content is None:
                p.unlink(missing_ok=True)
                files_restored.append(f"deleted {snap['path']}")
            else:
                p.write_text(content)
                files_restored.append(f"restored {snap['path']}")

    # Also remove the last user+assistant pair from message history
    msgs = s.get("messages", [])
    removed_msgs = 0
    if msgs:
        s["messages"] = msgs[:-2]
        removed_msgs = min(2, len(msgs))

    return {
        "session_id": session_id,
        "status": "undone",
        "messages_remaining": len(s["messages"]),
        "messages_removed": removed_msgs,
        "files_restored": files_restored,
    }


@router.post("/{session_id}/resume")
async def resume_session(session_id: str) -> dict[str, Any]:
    from hicode.compat import restore_from_checkpoint
    from server.checkpoint import load_checkpoint
    from server.coordinator import coordinator

    ckpt = await load_checkpoint(session_id)
    if not ckpt:
        raise HTTPException(
            status_code=404, detail=f"No checkpoint found for session '{session_id}'"
        )

    run_state = restore_from_checkpoint(session_id)
    result = await coordinator.resume(run_state)
    return result


@router.get("/{session_id}/changes")
async def get_session_changes(session_id: str) -> dict[str, Any]:
    from hicode.compat import compute_diff

    stacks = _undo_stacks.get(session_id, [])
    # Keep FIRST snapshot per path — captures state before any session writes
    seen: dict[str, dict] = {}
    for turn in stacks:
        for snap in turn:
            seen.setdefault(snap["path"], snap)

    changes = []
    for path, snap in seen.items():
        before = snap["content"] or ""
        try:
            after = pathlib.Path(path).read_text(errors="replace")
            status = "added" if snap["content"] is None else "modified"
        except FileNotFoundError:
            after = ""
            status = "deleted"

        diff = compute_diff(before, after, path=path)
        additions = sum(
            1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")
        )
        deletions = sum(
            1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")
        )
        if additions == 0 and deletions == 0:
            continue
        changes.append(
            {
                "path": path,
                "additions": additions,
                "deletions": deletions,
                "status": status,
                "diff": diff,
            }
        )

    return {"session_id": session_id, "changes": changes}


@router.post("/{session_id}/share")
async def share_session(session_id: str) -> dict[str, Any]:
    from hicode.compat import redact_share_secrets

    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")

    # Serialize and redact BEFORE any upload — prevents API key / path leakage
    payload = {
        "id": s["id"],
        "persona": s.get("persona", ""),
        "messages": s.get("messages", []),
        "cost_usd": s.get("cost_usd", 0.0),
        # Deliberately exclude config (may contain api_key fields)
    }
    redacted = redact_share_secrets(payload)

    share_token = str(uuid.uuid4())
    _shares[share_token] = redacted  # store locally; upload_share goes here in P5

    return {"session_id": session_id, "share_token": share_token, "status": "shared"}
