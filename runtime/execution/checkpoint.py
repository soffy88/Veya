"""Durable, idempotent execution checkpoints."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import ExecutionCheckpoint


class CheckpointError(ValueError):
    """Checkpoint is invalid or conflicts with an existing checkpoint."""


@dataclass(frozen=True)
class DurableExecutionCheckpoint:
    checkpoint_id: str
    goal_run_id: str
    execution_id: str
    provider_request_state: dict[str, Any]
    worker_state: dict[str, Any]
    working_directory: str
    git_head: str | None
    uncommitted_changes_digest: str
    last_event_sequence: int
    last_completed_tool: str | None
    resume_context: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    created_at: str = ""
    schema_version: int = 1

    def canonical_payload(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("content_hash", None)
        return data

    def with_content_hash(self) -> "DurableExecutionCheckpoint":
        payload = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return DurableExecutionCheckpoint(**{**asdict(self), "content_hash": hashlib.sha256(payload.encode()).hexdigest()})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExecutionCheckpointStore:
    """Atomic checkpoint persistence with checkpoint-id idempotency."""

    def __init__(self, run_root: str | Path):
        self.run_root = Path(run_root).expanduser().resolve()
        self.directory = self.run_root / "checkpoints"
        self.path = self.directory / "execution.json"

    def write(self, checkpoint: ExecutionCheckpoint) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.path, checkpoint.to_dict())
        return self.path

    def read(self) -> ExecutionCheckpoint | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return ExecutionCheckpoint(**value)

    def write_durable(self, checkpoint: DurableExecutionCheckpoint) -> Path:
        if not checkpoint.checkpoint_id or not checkpoint.goal_run_id or not checkpoint.execution_id:
            raise CheckpointError("checkpoint_id, goal_run_id and execution_id are required")
        normalized = checkpoint.with_content_hash()
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{normalized.checkpoint_id}.json"
        if target.exists():
            existing = self._read_durable_path(target)
            if existing == normalized:
                return target
            raise CheckpointError(f"checkpoint_id already exists with different content: {normalized.checkpoint_id}")
        self._atomic_write(target, normalized.to_dict())
        latest = self.directory / "latest.json"
        self._atomic_write(latest, normalized.to_dict())
        return target

    def read_durable(self, checkpoint_id: str | None = None) -> DurableExecutionCheckpoint | None:
        target = self.directory / (f"{checkpoint_id}.json" if checkpoint_id else "latest.json")
        if not target.exists():
            return None
        return self._read_durable_path(target)

    def _read_durable_path(self, target: Path) -> DurableExecutionCheckpoint:
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
            checkpoint = DurableExecutionCheckpoint(**value)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise CheckpointError(f"invalid checkpoint: {target}") from exc
        if checkpoint.content_hash != checkpoint.with_content_hash().content_hash:
            raise CheckpointError(f"checkpoint content hash mismatch: {target}")
        return checkpoint

    @staticmethod
    def _atomic_write(target: Path, value: dict[str, Any]) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            try:
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
