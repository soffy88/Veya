# 3O-IO-ALLOW — server/bot_team/workers.py
# 理由：worker 状态的**物理落盘面**，与 ``conversations.py`` 完全同源（同一个
# ``<bots_root>/<bot_id>/``、同一套 ``atomic_write_json`` 原子写、同一套
# ``safe_bot_id`` 校验）。拆走会造出第二套 bot 侧状态文件约定。判定/视图/预算/
# brief 合并算法一律在 ``veya.oskill.pure.worker_brief``（3O-PURE，零 IO）。

"""Persistent workers (NANO-W10 W1) and the resume brief that carries them (W2).

A delegated worker keeps its own context across tasks inside one GoalRun; the
next task to the same worker resumes it instead of cold-starting. State lives in
``<bots_root>/<bot_id>/workers.json`` — the same file-atomic-write convention
``conversations.py`` already uses, so there is no new store, no new scheduler and
no second execution authority. A worker row is context bookkeeping only: the
delegation itself still runs through the existing
``runtime.execution.delegate_runtime.DelegateRuntime``.

Flag: ``VEYA_PERSISTENT_WORKERS`` (default OFF). With the flag off every mutating
method is a strict no-op and :func:`resume_brief_for` returns the session brief
unchanged, byte for byte.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json, default_bots_root, safe_bot_id
from veya.oskill.pure.worker_brief import (
    merge_worker_briefs,
    worker_context_budget,
    worker_view,
)

__all__ = [
    "WORKERS_FILE",
    "WorkerRegistry",
    "archive_workers",
    "persistent_workers_enabled",
    "resume_brief_for",
    "worker_registry",
]

WORKERS_FILE = "workers.json"


def persistent_workers_enabled() -> bool:
    """``VEYA_PERSISTENT_WORKERS`` (default OFF)."""
    from server.feature_flags import enabled as _flag_enabled

    try:
        return bool(_flag_enabled("VEYA_PERSISTENT_WORKERS"))
    except KeyError:
        return False


class WorkerRegistry:
    """Per-bot worker rows: name, status, last task, context size."""

    def __init__(self, bot_id: str, *, root: str | Path | None = None) -> None:
        self.bot_id = safe_bot_id(bot_id)
        self.root = Path(root).expanduser() if root is not None else default_bots_root()
        self.path = self.root / self.bot_id / WORKERS_FILE

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        workers = raw.get("workers")
        return workers if isinstance(workers, dict) else {}

    def _save(self, workers: dict[str, dict[str, Any]]) -> None:
        atomic_write_json(self.path, {"bot_id": self.bot_id, "workers": workers})

    # ── public ───────────────────────────────────────────────────────────

    def list(self) -> list[dict[str, Any]]:
        """Worker rows for the existing session/team UI (empty when the flag is off)."""
        if not persistent_workers_enabled():
            return []
        return worker_view(list(self._load().values()))

    def get(self, name: str) -> dict[str, Any] | None:
        if not persistent_workers_enabled():
            return None
        row = self._load().get(str(name or ""))
        return dict(row) if isinstance(row, dict) else None

    def touch(
        self,
        name: str,
        *,
        last_task: str = "",
        context_size: int = 0,
        status: str = "running",
    ) -> dict[str, Any]:
        """Record that a worker did work, so the next task resumes it.

        No-op when the flag is off.
        """
        if not persistent_workers_enabled():
            return {}
        workers = self._load()
        key = str(name or "worker")
        row = workers.setdefault(key, {"name": key})
        row["status"] = status if status in {"idle", "running", "retired"} else "idle"
        if last_task:
            row["last_task"] = str(last_task)[:200]
        try:
            row["context_size"] = max(0, int(context_size or 0))
        except (TypeError, ValueError):
            row["context_size"] = 0
        workers[key] = row
        self._save(workers)
        return dict(row)

    def set_summary(self, name: str, *, summary: str, open_items: list[str] | None = None):
        """Attach the worker's own summary + open items (what W2 writes into the brief)."""
        if not persistent_workers_enabled():
            return {}
        workers = self._load()
        key = str(name or "worker")
        row = workers.setdefault(key, {"name": key})
        row["summary"] = str(summary or "")[:4000]
        row["open_items"] = [str(item)[:300] for item in (open_items or [])][:20]
        workers[key] = row
        self._save(workers)
        return dict(row)

    def reset(self, name: str) -> bool:
        """Forget one worker's context (the user can always start it fresh)."""
        if not persistent_workers_enabled():
            return False
        workers = self._load()
        key = str(name or "")
        if key not in workers:
            return False
        workers.pop(key)
        self._save(workers)
        return True

    def retire(self, name: str) -> bool:
        """Mark one worker retired; it keeps its row so the user can see it."""
        if not persistent_workers_enabled():
            return False
        workers = self._load()
        key = str(name or "")
        if key not in workers:
            return False
        workers[key]["status"] = "retired"
        self._save(workers)
        return True

    def budget(self, total_tokens: int) -> dict[str, int]:
        """Split a context budget between the session and each worker."""
        if not persistent_workers_enabled():
            return {"session": int(total_tokens)}
        return worker_context_budget(total_tokens, list(self._load().values()))


_REGISTRY: dict[str, WorkerRegistry] = {}


def worker_registry(bot_id: str) -> WorkerRegistry:
    """Process-level registry for one bot (same lifetime model as the tool cache)."""
    key = safe_bot_id(bot_id)
    if key not in _REGISTRY:
        _REGISTRY[key] = WorkerRegistry(key)
    return _REGISTRY[key]


def resume_brief_for(bot_id: str, session_brief: str) -> str:
    """Fold every worker of *bot_id* into the ONE resume brief (W2).

    Flag off, no workers, or no bot: returns ``session_brief`` unchanged, so the
    existing compact/handoff output is byte-identical.
    """
    if not persistent_workers_enabled():
        return str(session_brief or "")
    try:
        workers = worker_registry(safe_bot_id(bot_id)).list()
    except ValueError:
        return str(session_brief or "")
    return merge_worker_briefs(session_brief, workers)


def archive_workers(bot_id: str, *, root: str | Path | None = None) -> str:
    """Move a bot's worker file aside when the bot is deleted (never destroyed)."""
    registry = WorkerRegistry(bot_id, root=root)
    if not registry.path.exists():
        return ""
    target = registry.path.with_name("workers.archived.json")
    with contextlib.suppress(OSError):
        registry.path.replace(target)
        return str(target)
    return ""