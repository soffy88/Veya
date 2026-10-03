"""P2-01 Persistent Programmable Agent Environment.

A persistent REPL/runtime state for agents, similar in purpose to Prime Agent's
programmable persistent environment. Must remain subordinate to Goal/Execution/
Workspace authorities.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PersistentEnvironment:
    """Persistent runtime state for an agent.

    This is NOT a second authority — it is a programmable environment that
    agents can use to store and retrieve state across executions.
    All state is subordinate to Goal/Execution/Workspace authorities.
    """

    env_id: str
    agent_id: str
    state: dict[str, Any] = field(default_factory=dict)
    bindings: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def get(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.state[key] = value
        self.updated_at = time.time()

    def bind(self, key: str, value: str) -> None:
        self.bindings[key] = value
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PersistentEnvironmentStore:
    """Durable store for persistent agent environments."""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = Path.home() / ".veya" / "persistent_envs"
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    def _env_path(self, env_id: str) -> Path:
        return self.path / f"{env_id}.json"

    def save(self, env: PersistentEnvironment) -> None:
        with self._lock:
            self.path.mkdir(parents=True, exist_ok=True)
            env.updated_at = time.time()
            self._env_path(env.env_id).write_text(
                json.dumps(env.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def load(self, env_id: str) -> PersistentEnvironment | None:
        with self._lock:
            path = self._env_path(env_id)
            if not path.exists():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return PersistentEnvironment(**data)
            except (json.JSONDecodeError, TypeError):
                return None

    def list_for_agent(self, agent_id: str) -> list[PersistentEnvironment]:
        with self._lock:
            if not self.path.exists():
                return []
            result = []
            for f in self.path.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if data.get("agent_id") == agent_id:
                        result.append(PersistentEnvironment(**data))
                except (json.JSONDecodeError, TypeError):
                    continue
            return result
