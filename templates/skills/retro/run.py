"""retro — persist mistake/lesson into Genesis + user memory. No new store."""

from __future__ import annotations

from typing import Any


def main(
    action: str,
    mistake: str = "",
    lesson: str = "",
    context: str = "",
    n: int = 5,
    storage_dir: str = "",
    **_: Any,
) -> dict[str, Any]:
    from server.agents.genesis_memory import GenesisMemory

    mem = GenesisMemory(storage_dir=storage_dir or None)
    if action == "record":
        if not (mistake or "").strip() or not (lesson or "").strip():
            return {"ok": False, "error": "record needs mistake and lesson"}
        mem.record_experience(mistake.strip(), lesson.strip(), context.strip() or None)
        user_id = "anonymous"
        try:
            from server.auth import current_user

            user_id = str(current_user().get("user_id") or "anonymous")
        except Exception:
            user_id = "anonymous"
        try:
            from veya.oskill.memory_store import default_memory_store

            store = default_memory_store()
            text = f"[retro] {mistake.strip()} → {lesson.strip()}"
            store.add_sync(user_id, "summary", text, salience=0.7)
        except Exception:
            pass
        return {"ok": True, "action": "record", "user_id": user_id}
    if action == "recent":
        count = max(1, min(int(n or 5), 20))
        return {"ok": True, "action": "recent", "entries": mem.recent_experiences(count)}
    return {"ok": False, "error": f"unknown action: {action}"}
