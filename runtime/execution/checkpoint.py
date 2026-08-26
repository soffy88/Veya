"""Task-scoped execution checkpoint persistence."""

from __future__ import annotations

import json
from pathlib import Path

from .models import ExecutionCheckpoint


class ExecutionCheckpointStore:
    def __init__(self, run_root: str | Path):
        self.run_root = Path(run_root).expanduser().resolve()
        self.path = self.run_root / "checkpoints" / "execution.json"

    def write(self, checkpoint: ExecutionCheckpoint) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        return self.path

    def read(self) -> ExecutionCheckpoint | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return ExecutionCheckpoint(**value)
