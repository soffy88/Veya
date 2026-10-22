"""Read-only index for Claude-BugHunter security SKILL.md knowledge (Phase 0).

Thin wrapper around server.external_skill_index for backwards compatibility.
"""

from __future__ import annotations

from typing import Any

from server import external_skill_index


def enabled() -> bool:
    return external_skill_index.bughunter_enabled()

def skills_dir() -> str:
    import os
    return os.environ.get("VEYA_BUGHUNTER_SKILLS_DIR", "") or os.path.join(
        os.path.expanduser("~"), "projects", "Claude-BugHunter", "skills"
    )

class _BugHunterIndexProxy:
    @property
    def index(self):
        return external_skill_index.index

    def entries(self):
        return [e for e in external_skill_index.index.entries() if e.source == "bughunter"]

    def count(self) -> int:
        return len(self.entries())

    def search(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        if not enabled():
            return []
        return [h for h in external_skill_index.index.search(query, top_k=top_k) if h.get("source") == "bughunter"]

    def read(self, name: str) -> str | None:
        if not enabled():
            return None
        body = external_skill_index.index.read(name)
        if body is not None:
            for e in external_skill_index.index.entries():
                if (e.name == name or e.dir_name == name) and e.source == "bughunter":
                    return body
        return None

index = _BugHunterIndexProxy()

def search_bughunter_skills(query: str, top_k: int = 8) -> list[dict[str, Any]]:
    if not enabled():
        return []
    return [h for h in external_skill_index.search_external_skills(query, top_k=top_k) if h.get("source") == "bughunter"]
