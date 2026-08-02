"""
server/checkpoint.py — checkpoint 落盘 / 恢复

使用 obase.versionstore (JSONL append + latest) 落盘。
make_checkpoint / restore_from_checkpoint 是 oprim 纯函数(无 IO)。
IO 在本层(layer4)完成。

落盘路径: ~/.hicode/checkpoints/{session_id}.jsonl
"""
from __future__ import annotations

import pathlib
from typing import Any

from obase.versionstore import jsonl_append, jsonl_latest

_CKPT_DIR = pathlib.Path.home() / ".hicode" / "checkpoints"


async def save_checkpoint(session_id: str, run_state: Any) -> None:
    """Serialize run_state to CheckpointData and append to JSONL file."""
    from oprim import make_checkpoint

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


async def load_checkpoint(session_id: str) -> Any | None:
    """Load the latest checkpoint for session_id. Returns CheckpointData or None."""
    from oprim._make_checkpoint import CheckpointData

    path = _CKPT_DIR / f"{session_id}.jsonl"
    entry = jsonl_latest(path=path, by_key="session_id")
    if entry is None:
        return None
    return CheckpointData(
        session_id=entry["session_id"],
        timestamp=entry["timestamp"],
        version=entry["version"],
        payload=entry["payload"],
    )


def ckpt_path(session_id: str) -> pathlib.Path:
    """Return the JSONL file path for a session (for existence checks in tests)."""
    return _CKPT_DIR / f"{session_id}.jsonl"
