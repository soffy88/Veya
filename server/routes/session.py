"""Session management routes: create, fork, share, compact, undo."""

from __future__ import annotations

import contextlib
import copy
import pathlib
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod
from server.events import event_store
from server.session_identity import new_session_id
from veya.platform import obase as _load_obase

router = APIRouter(prefix="/session", tags=["session"])

# ---------------------------------------------------------------------------
# 会话持久化: obase.CheckpointStore (SQLite WAL, ~/.veya/checkpoints)
# 3O 铁律 §1.4 — 机制在主库 obase, 本路由只装配。容器内 ~/.veya 落在
# veya-data 卷, 重启后 hydration 恢复全部会话。
# ---------------------------------------------------------------------------
_obase = _load_obase()
_store = _obase.CheckpointStore()

# In-memory session store (persisted via _store; hydrate on boot)
_sessions: dict[str, dict] = {}


def _persist(sid: str) -> None:
    """写盘当前会话 (全量快照, SQLite WAL 原子提交)。"""
    s = _sessions.get(sid)
    if s is not None:
        _store.save(sid, s)
        with contextlib.suppress(Exception):
            from veya.history_store import default_history_store

            default_history_store().save_sync(
                sid,
                list(s.get("messages") or []),
                user_id=s.get("user_id", "anonymous"),
            )


def _hydrate() -> None:
    """启动/导入时从 checkpoint 库恢复全部会话 (重启不丢历史)。"""
    for key in _store.keys():  # noqa: SIM118 — CheckpointStore 非 dict, 无 __iter__
        s = _store.load(key)
        if isinstance(s, dict) and s.get("id"):
            _sessions[key] = s


_hydrate()

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


def _owned_session(session_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """取 session 并校验归属；不存在/不属于当前用户一律 404 (不区分两者,

    防止靠枚举 id 探测"这个 id 存在但不是你的")。历史遗留数据 (hydrate 自
    CheckpointStore 的旧记录) 没有 user_id 字段, 视为属于 anonymous 桶。
    """
    s = _sessions.get(session_id)
    owner = (s or {}).get("user_id", "anonymous")
    if not s or owner != user["user_id"]:
        raise HTTPException(status_code=404, detail="Session not found")
    return s


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
async def create_session(
    req: SessionCreateRequest, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    import datetime

    sid = new_session_id()
    now = datetime.datetime.utcnow().isoformat() + "Z"
    _sessions[sid] = {
        "id": sid,
        "user_id": user["user_id"],
        "persona": req.persona,
        "project": req.project,
        "config": req.config,
        "messages": [],
        "cost_usd": 0.0,
        "created_at": now,
        "updated_at": now,
    }
    _persist(sid)
    event_store.append(
        {
            "topic": "session.created",
            "session_id": sid,
            "trace_id": sid,
            "actor": user["user_id"],
            "payload": {"session_id": sid},
        }
    )
    return {"id": sid, "status": "created", "project": req.project}


@router.get("/{session_id}")
async def get_session(
    session_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    return _owned_session(session_id, user)


@router.get("/{session_id}/lineage")
async def session_lineage(
    session_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    """SessionLineage: 任务/子任务派生谱系 (祖先链 + 直接子任务)。

    对标 Cline SessionLineage —— 支撑多 Agent 协作时追踪任务来源。
    """
    s = _owned_session(session_id, user)

    # 祖先链: 沿 forked_from 上溯 (最远在前); 只收当前用户名下的节点
    ancestors: list[dict[str, Any]] = []
    cur_id = s.get("forked_from")
    seen: set[str] = set()
    while cur_id and cur_id not in seen:
        seen.add(cur_id)
        p = _sessions.get(cur_id)
        if not p or p.get("user_id", "anonymous") != user["user_id"]:
            break
        ancestors.append(p)
        cur_id = p.get("forked_from")
    ancestors.reverse()

    # 直接子任务 (同样按归属过滤)
    descendants = [
        d
        for d in _sessions.values()
        if d.get("forked_from") == session_id and d.get("user_id", "anonymous") == user["user_id"]
    ]
    return {
        "session_id": session_id,
        "depth": len(ancestors),
        "ancestors": ancestors,
        "descendants": descendants,
        "forked_from": s.get("forked_from"),
    }


@router.post("/{session_id}/fork")
async def fork_session(
    session_id: str,
    req: SessionForkRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    s = _owned_session(session_id, user)
    new_id = new_session_id()
    _sessions[new_id] = {**copy.deepcopy(s), "id": new_id, "label": req.label}
    _persist(new_id)
    event_store.append(
        {
            "topic": "session.created",
            "session_id": new_id,
            "trace_id": new_id,
            "actor": user["user_id"],
            "payload": {"session_id": new_id, "forked_from": session_id},
        }
    )
    return {"session_id": new_id, "forked_from": session_id}


@router.post("/{session_id}/compact")
async def compact_session(
    session_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    s = _owned_session(session_id, user)

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
    _persist(session_id)

    return {
        "session_id": session_id,
        "status": "compacted",
        "messages_before": len(msgs),
        "messages_after": len(s["messages"]),
        "chars_removed": old_token_estimate,
    }


def _owned_if_tracked(session_id: str, user: dict[str, Any]) -> None:
    """changes 的 session_id 也可能来自 /tool 的游离用法 (不经过 POST

    /session 创建即可写 undo 快照, 见 server/routes/tool.py), 这类没有对应
    _sessions 记录时无法判断归属, 保留原有行为不额外限制。有记录的情况下
    (常规用法) 必须校验归属, 防止拿泄露的 id 读到别人会话的文件改动 diff。
    """
    s = _sessions.get(session_id)
    if s is not None and s.get("user_id", "anonymous") != user["user_id"]:
        raise HTTPException(status_code=404, detail="Session not found")


@router.post("/{session_id}/undo")
async def undo_session(
    session_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    s = _owned_session(session_id, user)

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
    _persist(session_id)

    return {
        "session_id": session_id,
        "status": "undone",
        "messages_remaining": len(s["messages"]),
        "messages_removed": removed_msgs,
        "files_restored": files_restored,
    }


@router.post("/{session_id}/resume")
async def resume_session(
    session_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    # 2026-08-16 修复: server/checkpoint.py 现在落盘时打 owner (见
    # save_checkpoint), 这里能真正校验"这个 checkpoint 是不是你的"了，不再
    # 只是拦"必须登录"。owner=None 是早于本次修复写入的旧数据, 视为无主放行
    # (与 _owned_session/ensure_session 的一致口径, 不强行让旧数据不可恢复)。
    from server.checkpoint import load_checkpoint
    from server.coordinator import coordinator
    from veya.compat import restore_from_checkpoint

    ckpt = await load_checkpoint(session_id)
    if not ckpt:
        raise HTTPException(
            status_code=404, detail=f"No checkpoint found for session '{session_id}'"
        )
    if ckpt.owner is not None and ckpt.owner != user["user_id"]:
        raise HTTPException(
            status_code=404, detail=f"No checkpoint found for session '{session_id}'"
        )

    run_state = restore_from_checkpoint(session_id)
    result = await coordinator.resume(run_state)
    return result


@router.get("/{session_id}/changes")
async def get_session_changes(
    session_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    from veya.compat import compute_diff

    _owned_if_tracked(session_id, user)
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
async def share_session(
    session_id: str, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    from veya.compat import redact_share_secrets

    s = _owned_session(session_id, user)

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
