"""
server/checkpoint.py — checkpoint 落盘 / 恢复

落盘路径: ~/.veya/checkpoints/{session_id}.jsonl
使用 veya.compat 提供的基础函数 (替代已移除的 oprim/obase)。
"""

from __future__ import annotations

import pathlib

from veya.compat import (
    _CHECKPOINT_DIR,
    CheckpointData,
    RunState,
    jsonl_append,
    make_checkpoint,
)

_CKPT_DIR = _CHECKPOINT_DIR


async def save_checkpoint(session_id: str, run_state: RunState) -> None:
    """Serialize run_state to CheckpointData and append to JSONL file.

    落盘时打上当前已鉴权用户 (server.auth 的 contextvar) 作为 owner；未登录
    落 anonymous。2026-08-16 修复: 此前完全没有归属概念, 任何人凭 session_id
    都能通过 /session/{id}/resume 续跑别人的 checkpoint。
    """
    from server import auth as auth_mod

    ckpt = make_checkpoint(run_state, session_id=session_id)
    path = _CKPT_DIR / f"{session_id}.jsonl"
    await jsonl_append(
        path=path,
        entry={
            "session_id": ckpt.session_id,
            "timestamp": ckpt.timestamp,
            "version": ckpt.version,
            "payload": ckpt.payload,
            "owner": auth_mod.current_user()["user_id"],
        },
    )


async def load_checkpoint(session_id: str) -> CheckpointData | None:
    """Load the latest checkpoint for session_id. Returns CheckpointData or None.

    owner=None 表示旧数据 (早于本次修复写入, 未记录归属)——调用方按此区分
    "无主放行" vs "有主但不是你" (见 server/routes/session.py::resume_session)。
    """
    path = _CKPT_DIR / f"{session_id}.jsonl"
    entry = await _read_latest(path)
    if entry is None:
        return None
    return CheckpointData(
        session_id=entry["session_id"],
        timestamp=entry["timestamp"],
        version=entry["version"],
        payload=entry["payload"],
        owner=entry.get("owner"),
    )


async def _read_latest(path: pathlib.Path) -> dict | None:
    """Read the latest JSONL entry (sync in compat, wrap for async)."""
    if not path.exists():
        return None
    lines = path.read_text().strip().splitlines()
    if not lines:
        return None
    import json

    return json.loads(lines[-1])


def ckpt_path(session_id: str) -> pathlib.Path:
    """Return the JSONL file path for a session (for existence checks in tests)."""
    return _CKPT_DIR / f"{session_id}.jsonl"
