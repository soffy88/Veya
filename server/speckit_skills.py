"""Spec Kit (SDD) Phase 0: read-only command-template index (default off).

The 10 upstream command templates (``specify``, ``plan``, ``tasks``, …) are
knowledge, not Veya skill packages: they carry no ``manifest.json`` and no
``run.py``, so they must not enter the canonical
``server.skill_hub.VeyaSkillHub`` write authority or ``registries.skills``.

They are surfaced the same way the Claude-BugHunter and wshobson corpora are:
as read-only hits inside a ``search_tools`` result, which the model reads on
demand via the returned path.  They are never registered as tools and never
enter the resident or lazy tool surface, so the master tool list is
byte-identical when ``VEYA_SPECKIT`` is off.

Indexing is lazy and mtime-cached, and only happens when the flag is on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.speckit_phases import FLAG_NAME, TEMPLATE_ARTIFACTS_DIR, enabled

#: Frontmatter fields we surface; the templates only carry ``description``.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):\s?(.*)$")

#: Ranking weights.  Mirrors server.external_skill_index: a query that names a
# command must outrank a template that merely mentions the word.
_SCORE_EXACT_NAME = 20
_SCORE_NAME_PREFIX = 12
_SCORE_NAME_TOKEN = 8
_SCORE_DESCRIPTION_CONTAINS = 4
_SCORE_DESCRIPTION_TOKEN = 2
_SCORE_QUERY_SUBSTRING_IN_HAY = 5

#: Where the vendored templates live, relative to the repository root.
COMMANDS_DIR = "commands"
ARTIFACT_SUFFIX = "-template.md"


@dataclass(frozen=True)
class SpeckitSkill:
    """A read-only view of one vendored Spec Kit template."""

    name: str
    description: str
    path: str
    kind: str  # "command" or "artifact"
    dir_name: str = ""


def _parse_frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key_match = _KEY_RE.match(line)
        if key_match:
            out[key_match.group(1).lower()] = key_match.group(2).strip()
    return out


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _search_tokens(text: str) -> set[str]:
    text = text.lower()
    tokens = {t for t in re.findall(r"[a-z0-9_]+", text) if len(t) > 1}
    for run in re.findall(r"[一-鿿]+", text):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _overlap_ratio(query_tokens: set[str], text: str) -> float:
    """Shared query tokens as a fraction of the field's own token count."""
    field_tokens = _search_tokens(text)
    if not field_tokens:
        return 0.0
    return len(query_tokens & field_tokens) / len(field_tokens)


class SpeckitSkillIndex:
    """Lazy, read-only index over the vendored Spec Kit templates."""

    def __init__(self) -> None:
        self._entries: list[SpeckitSkill] | None = None
        self._mtimes: dict[str, tuple[float, int]] = {}

    def _root(self) -> Path:
        return Path(__file__).resolve().parent.parent / TEMPLATE_ARTIFACTS_DIR

    def _collect(self) -> list[SpeckitSkill]:
        root = self._root()
        if not root.is_dir():
            return []
        found: list[SpeckitSkill] = []
        # 1. Command templates: templates/speckit/commands/<name>.md
        commands = root / COMMANDS_DIR
        if commands.is_dir():
            for path in sorted(commands.glob("*.md")):
                stat = path.stat()
                key = str(path)
                if self._mtimes.get(key) != (stat.st_mtime, stat.st_size):
                    self._mtimes[key] = (stat.st_mtime, stat.st_size)
                front = _parse_frontmatter(_safe_read(path))
                found.append(
                    SpeckitSkill(
                        name=path.stem,
                        description=front.get("description", ""),
                        path=str(path),
                        kind="command",
                    )
                )
        # 2. Artifact templates: templates/speckit/<name>-template.md
        for path in sorted(root.glob(f"*{ARTIFACT_SUFFIX}")):
            stat = path.stat()
            key = str(path)
            if self._mtimes.get(key) != (stat.st_mtime, stat.st_size):
                self._mtimes[key] = (stat.st_mtime, stat.st_size)
            text = _safe_read(path)
            front = _parse_frontmatter(text)
            found.append(
                SpeckitSkill(
                    name=path.name,
                    description=front.get("description") or _first_heading(text),
                    path=str(path),
                    kind="artifact",
                )
            )
        return found

    def _scan_if_stale(self) -> None:
        if not enabled():
            self._entries = []
            return
        if self._entries is None:
            self._entries = self._collect()
            return
        # Re-scan on every call while enabled: a template edit is rare and the
        # glob is cheap (15 files), so staleness detection stays exact.
        self._entries = self._collect()

    def entries(self) -> list[SpeckitSkill]:
        if not enabled():
            return []
        self._scan_if_stale()
        return list(self._entries or [])

    def count(self) -> int:
        return len(self.entries())

    def search(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        if not enabled():
            return []
        q = (query or "").lower().strip()
        if not q:
            return []
        q_tokens = _search_tokens(q)
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for entry in self.entries():
            name = entry.name.lower()
            description = entry.description.lower()
            hay = f"{name} {description}".lower()
            score = 0
            if name == q:
                score += _SCORE_EXACT_NAME
            elif name.startswith(q) or q.startswith(name):
                score += _SCORE_NAME_PREFIX
            name_hits = len(q_tokens & _search_tokens(name))
            if name_hits:
                score += _SCORE_NAME_TOKEN * name_hits
                if q_tokens and name_hits >= len(q_tokens):
                    score += _SCORE_NAME_PREFIX
            if q in description:
                score += _SCORE_DESCRIPTION_CONTAINS
            score += round(_SCORE_DESCRIPTION_TOKEN * 100 * _overlap_ratio(q_tokens, description))
            if q in hay:
                score += _SCORE_QUERY_SUBSTRING_IN_HAY
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    entry.name,
                    {
                        "name": entry.name,
                        "description": entry.description,
                        "path": entry.path,
                        "source": "speckit",
                        "kind": entry.kind,
                        "score": score,
                    },
                )
            )
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [row[2] for row in scored[: max(1, min(top_k, 20))]]

    def read(self, name: str) -> str | None:
        if not enabled():
            return None
        wanted = str(name or "").strip()
        for entry in self.entries():
            if entry.name == wanted:
                return _safe_read(Path(entry.path))
        return None


#: Module-level singleton, lazily populated by entries()/search().
index = SpeckitSkillIndex()


def search_speckit_skills(query: str, top_k: int = 8) -> list[dict[str, Any]]:
    """Public hook for ``search_tools``. Returns [] unless the flag is on."""
    if not enabled():
        return []
    return index.search(query, top_k=top_k)


def status() -> dict[str, Any]:
    """Read-only projection; never spawns, never writes."""
    if not enabled():
        return {
            "name": "speckit_skills",
            "available": False,
            "reason": f"unavailable: flag disabled ({FLAG_NAME})",
            "count": 0,
        }
    return {
        "name": "speckit_skills",
        "available": True,
        "reason": "ready",
        "count": index.count(),
    }


__all__ = [
    "ARTIFACT_SUFFIX",
    "COMMANDS_DIR",
    "FLAG_NAME",
    "SpeckitSkill",
    "SpeckitSkillIndex",
    "index",
    "search_speckit_skills",
    "status",
]
