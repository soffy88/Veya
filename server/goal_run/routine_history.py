import json
import os
from pathlib import Path
from typing import Any

class RoutineHistoryStore:
    def __init__(self, bot_id: str, storage_path: str | Path | None = None):
        self.bot_id = bot_id
        if storage_path is None:
            self.storage_path = Path(os.environ.get("VEYA_ROUTINE_HISTORY_DIR", os.path.expanduser("~/.veya/routine_history"))) / f"{bot_id}.json"
        else:
            self.storage_path = Path(storage_path)

    def _load(self) -> dict[str, list[dict]]:
        if not self.storage_path.exists():
            return {}
        try:
            return json.loads(self.storage_path.read_text())
        except Exception:
            return {}

    def _save(self, data: dict[str, list[dict]]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_path.write_text(json.dumps(data, indent=2))

    def add_record(self, routine_id: str, record: dict[str, Any]) -> None:
        data = self._load()
        if routine_id not in data:
            data[routine_id] = []
        data[routine_id].append(record)
        # Keep last 50
        if len(data[routine_id]) > 50:
            data[routine_id] = data[routine_id][-50:]
        self._save(data)

    def get_history(self, routine_id: str) -> list[dict]:
        return self._load().get(routine_id, [])
