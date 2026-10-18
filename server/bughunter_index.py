"""Read-only index for Claude-BugHunter security SKILL.md knowledge (Phase 0).

This is the Phase 0 *knowledge-layer* bridge for the Claude-BugHunter pilot.
It treats the upstream ``skills/`` directory of the Claude-BugHunter repo as a
read-only external corpus of security SKILL.md files and exposes a keyword
search over them — without copying the corpus into this repo, without ever
mounting them into the resident tool surface, and without changing behavior
when the feature is off.

Design constraints (from the frozen architecture + AGENTS.md):
- Only ONE model-facing mainline exists; no programmatic routing here. search
  is model-triggered via ``search_tools`` (see server.tool_registry).
- BugHunter SKILL.md are *knowledge* files (frontmatter ``name``/``description``
  + markdown body). They are NOT Veya executable skill packages
  (manifest.json+run.py), so they must not enter the canonical
  ``server.skill_hub.VeyaSkillHub`` write authority, nor ``registries.skills``
  (which is LEGACY-DEAD). They stay out of the prompt until explicitly searched.
- The corpus is read lazily (file mtime cache) and only when enabled via
  ``VEYA_BUGHUNTER_INDEX=1``; otherwise this module is a total no-op so the
  master tool surface is byte-for-byte unchanged.

Licensing: upstream is dual-licensed MIT (code) / CC BY 4.0 (skill content).
We only *index and read* the files; nothing is vendored or redistributed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ENABLED_ENV = "VEYA_BUGHUNTER_INDEX"
_DIR_ENV = "VEYA_BUGHUNTER_SKILLS_DIR"
_DEFAULT_SKILLS_DIR = os.path.join(
    os.path.expanduser("~"), "projects", "Claude-BugHunter", "skills"
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):\s?(.*)$")


@dataclass(frozen=True)
class BugHunterSkill:
    """A read-only view of one upstream Claude-BugHunter SKILL.md file."""

    name: str
    description: str
    dir_name: str
    path: str


def enabled() -> bool:
    """Phase 0 pilot is OFF by default; only an explicit '1' turns it on."""
    return os.environ.get(_ENABLED_ENV, "").strip() == "1"


def skills_dir() -> str:
    return os.environ.get(_DIR_ENV, "") or _DEFAULT_SKILLS_DIR


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract simple ``key: value`` frontmatter into a dict."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        km = _KEY_RE.match(line)
        if km:
            out[km.group(1).lower()] = km.group(2).strip()
    return out


class BugHunterIndex:
    """Lazy, read-only index over a Claude-BugHunter ``skills/`` directory.

    Files are scanned on first use and re-scanned only when an mtime/size
    changes. The index never mutates the corpus; it only reads it.
    """

    def __init__(self, skills_dir: str | None = None):
        self._dir = Path(skills_dir or skills_dir_func())
        self._entries: list[BugHunterSkill] | None = None
        self._mtimes: dict[str, tuple[float, int]] = {}

    # ── discovery ───────────────────────────────────────────────────────
    def _scan_if_stale(self) -> None:
        if not self._dir.is_dir():
            self._entries = []
            return
        changed = self._entries is None
        entries: list[BugHunterSkill] = []
        for child in sorted(self._dir.iterdir()):
            skill_md = child / "SKILL.md"
            if not (child.is_dir() and skill_md.is_file()):
                continue
            try:
                st = skill_md.stat()
            except OSError:
                continue
            prev = self._mtimes.get(str(skill_md))
            if prev != (st.st_mtime, st.st_size):
                self._mtimes[str(skill_md)] = (st.st_mtime, st.st_size)
                changed = True
            text = _safe_read(skill_md)
            fm = _parse_frontmatter(text)
            name = fm.get("name") or child.name
            description = fm.get("description") or ""
            entries.append(
                BugHunterSkill(
                    name=name,
                    description=description,
                    dir_name=child.name,
                    path=str(skill_md),
                )
            )
        if changed or self._entries is None:
            self._entries = entries

    def entries(self) -> list[BugHunterSkill]:
        if not enabled():
            return []
        self._scan_if_stale()
        return list(self._entries or [])

    def count(self) -> int:
        """Number of indexed SKILL.md files (0 when disabled/missing)."""
        return len(self.entries())

    # ── search ──────────────────────────────────────────────────────────
    def search(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        """Keyword search over name+description (and dir name).

        Reuses the same latin-word + CJK-bigram tokenization spirit as
        ``server.tool_registry._search_tokens`` so query semantics match the
        rest of the codebase. Returns ranked dicts (no file body read for the
        hit — keeps the surface light; the path is provided for on-demand load).
        """
        if not enabled():
            return []
        q = (query or "").lower().strip()
        q_tokens = _search_tokens(q)
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for entry in self.entries():
            hay = f"{entry.name} {entry.dir_name} {entry.description}".lower()
            score = 0
            if q and q in hay:
                score += 5
            score += len(q_tokens & _search_tokens(hay))
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    entry.name,
                    {
                        "name": entry.name,
                        "dir_name": entry.dir_name,
                        "description": entry.description,
                        "path": entry.path,
                        "score": score,
                    },
                )
            )
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [row[2] for row in scored[: max(1, min(top_k, 20))]]

    def read(self, name: str) -> str | None:
        """Read a single SKILL.md body on demand (after a search hit)."""
        if not enabled():
            return None
        for entry in self.entries():
            if entry.name == name or entry.dir_name == name:
                return _safe_read(Path(entry.path))
        return None


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _search_tokens(text: str) -> set[str]:
    """Latin words (>=2 chars) + CJK bigrams. Mirrors tool_registry tokenization."""
    text = text.lower()
    tokens = {t for t in re.findall(r"[a-z0-9_]+", text) if len(t) > 1}
    cjk = re.findall(r"[一-鿿]+", text)
    for run in cjk:
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def skills_dir_func() -> str:
    return skills_dir()


#: Module-level singleton, lazily populated by entries()/search().
index = BugHunterIndex()


def search_bughunter_skills(query: str, top_k: int = 8) -> list[dict[str, Any]]:
    """Public hook for ``search_tools``. Returns [] unless Phase 0 is enabled."""
    if not enabled():
        return []
    return index.search(query, top_k=top_k)
