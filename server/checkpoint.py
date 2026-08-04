"""
server/checkpoint.py — checkpoint 落盘 / 恢复

落盘路径: ~/.hicode/checkpoints/{session_id}.jsonl
使用 hicode.compat 提供的基础函数 (替代已移除的 oprim/obase)。
"""

from __future__ import annotations

import pathlib

from hicode.compat import (
    _CHECKPOINT_DIR,
    CheckpointData,
    RunState,
    jsonl_append,
    make_checkpoint,
)

_CKPT_DIR = _CHECKPOINT_DIR


async def save_checkpoint(session_id: str, run_state: RunState) -> None:
    """Serialize run_state to CheckpointData and append to JSONL file."""
    ckpt = make_checkpoint(run_state, session_id=session_id)
    path = _CKPT_DIR / f"{session_id}.jsonl"
    await jsonl_append(
        path=path,
        entry={
            "session_id": ckpt.session_id,
            "timestamp": ckpt.timestamp,
            "version": ckpt.version,
            "payload": ckpt.payload,
        },
    )


async def load_checkpoint(session_id: str) -> CheckpointData | None:
    """Load the latest checkpoint for session_id. Returns CheckpointData or None."""
    path = _CKPT_DIR / f"{session_id}.jsonl"
    entry = await _read_latest(path)
    if entry is None:
        return None
    return CheckpointData(
        session_id=entry["session_id"],
        timestamp=entry["timestamp"],
        version=entry["version"],
        payload=entry["payload"],
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
