"""Read-only multi-source external skill index (Phase 0).

Supports multiple sources:
- bughunter: VEYA_BUGHUNTER_INDEX=1, root ~/projects/Claude-BugHunter
- wshobson: VEYA_WSHOBSON_INDEX=1, root VEYA_WSHOBSON_ROOT or ~/projects/wshobson-agents (plugins/*/skills/*/SKILL.md)

Exposes keyword search over them without copying, without resident tool mounting,
and with zero behavior change when disabled.

Design constraints (from the frozen architecture + AGENTS.md):
- Only ONE model-facing mainline exists; no programmatic routing here. search
  is model-triggered via ``search_tools`` (see server.tool_registry).
- External SKILL.md are *knowledge* files (frontmatter ``name``/``description``
  + markdown body). They are NOT Veya executable skill packages
  (manifest.json+run.py), so they must not enter the canonical
  ``server.skill_hub.VeyaSkillHub`` write authority, nor ``registries.skills``
  (which is LEGACY-DEAD). They stay out of the prompt until explicitly searched.
- The corpora are read lazily (file mtime cache) and only when enabled via
  respective env vars; otherwise this module is a total no-op so the
  master tool surface is byte-for-byte unchanged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):\s?(.*)$")


@dataclass(frozen=True)
class ExternalSkill:
    """A read-only view of one upstream external SKILL.md file."""

    name: str
    description: str
    dir_name: str
    path: str
    source: str  # "bughunter" or "wshobson"
    plugin_name: str | None = None


def bughunter_enabled() -> bool:
    return os.environ.get("VEYA_BUGHUNTER_INDEX", "").strip() == "1"


def wshobson_enabled() -> bool:
    return os.environ.get("VEYA_WSHOBSON_INDEX", "").strip() == "1"


def enabled() -> bool:
    return bughunter_enabled() or wshobson_enabled()


def _parse_frontmatter(text: str) -> dict[str, str]:
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


#: Ranking weights for :meth:`ExternalSkillIndex.search`.  A query that names a
#: skill must outrank a skill whose body merely mentions the same word, so
#: exact/prefix name matches carry the largest bonus, description matches a
#: smaller one, and token overlap is normalized by the field length (a long
#: description must not outscore an exact name match on token count alone).
_SCORE_EXACT_NAME = 20
_SCORE_NAME_PREFIX = 12
_SCORE_NAME_TOKEN = 8
_SCORE_DESCRIPTION_CONTAINS = 4
_SCORE_DESCRIPTION_TOKEN = 2
_SCORE_QUERY_SUBSTRING_IN_HAY = 5


def _overlap_ratio(query_tokens: set[str], text: str) -> float:
    """Shared query tokens as a fraction of the field's own token count.

    Dividing by the field length is what stops a long description from
    out-scoring a short exact name match on raw token count.
    """
    field_tokens = _search_tokens(text)
    if not field_tokens:
        return 0.0
    return len(query_tokens & field_tokens) / len(field_tokens)


class ExternalSkillIndex:
    """Lazy, read-only multi-source external skill index."""

    def __init__(self):
        self._entries: list[ExternalSkill] | None = None
        self._mtimes: dict[str, tuple[float, int]] = {}

    def _scan_source(self, root_path: Path, source: str, entries: list[ExternalSkill]) -> bool:
        if not root_path.is_dir():
            return False
        changed = False
        if source == "bughunter":
            for child in sorted(root_path.iterdir()):
                skill_md = child / "SKILL.md"
                if not (child.is_dir() and skill_md.is_file()):
                    continue
                try:
                    st = skill_md.stat()
                except OSError:
                    continue
                key = str(skill_md)
                prev = self._mtimes.get(key)
                if prev != (st.st_mtime, st.st_size):
                    self._mtimes[key] = (st.st_mtime, st.st_size)
                    changed = True
                text = _safe_read(skill_md)
                fm = _parse_frontmatter(text)
                name = fm.get("name") or child.name
                description = fm.get("description") or ""
                entries.append(
                    ExternalSkill(
                        name=name,
                        description=description,
                        dir_name=child.name,
                        path=str(skill_md),
                        source="bughunter",
                        plugin_name=None,
                    )
                )
        elif source == "wshobson":
            plugins_dir = root_path / "plugins"
            if not plugins_dir.is_dir():
                return False
            for plugin_dir in sorted(plugins_dir.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                skills_dir = plugin_dir / "skills"
                if not skills_dir.is_dir():
                    continue
                for skill_dir in sorted(skills_dir.iterdir()):
                    skill_md = skill_dir / "SKILL.md"
                    if not (skill_dir.is_dir() and skill_md.is_file()):
                        continue
                    try:
                        st = skill_md.stat()
                    except OSError:
                        continue
                    key = str(skill_md)
                    prev = self._mtimes.get(key)
                    if prev != (st.st_mtime, st.st_size):
                        self._mtimes[key] = (st.st_mtime, st.st_size)
                        changed = True
                    text = _safe_read(skill_md)
                    fm = _parse_frontmatter(text)
                    name = fm.get("name") or skill_dir.name
                    description = fm.get("description") or ""
                    entries.append(
                        ExternalSkill(
                            name=name,
                            description=description,
                            dir_name=skill_dir.name,
                            path=str(skill_md),
                            source="wshobson",
                            plugin_name=plugin_dir.name,
                        )
                    )
        return changed

    def _scan_if_stale(self) -> None:
        if not enabled():
            self._entries = []
            return

        entries: list[ExternalSkill] = []
        any_changed = self._entries is None

        if bughunter_enabled():
            bh_dir = Path(
                os.environ.get("VEYA_BUGHUNTER_SKILLS_DIR", "")
                or os.path.join(os.path.expanduser("~"), "projects", "Claude-BugHunter", "skills")
            )
            if self._scan_source(bh_dir, "bughunter", entries):
                any_changed = True

        if wshobson_enabled():
            ws_dir = Path(
                os.environ.get("VEYA_WSHOBSON_ROOT", "")
                or os.path.join(os.path.expanduser("~"), "projects", "wshobson-agents")
            )
            if self._scan_source(ws_dir, "wshobson", entries):
                any_changed = True

        if any_changed or self._entries is None:
            self._entries = entries

    def entries(self) -> list[ExternalSkill]:
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
            dir_name = entry.dir_name.lower()
            hay = f"{name} {dir_name} {description}".lower()
            if entry.plugin_name:
                hay += f" plugin:{entry.plugin_name}".lower()

            score = 0
            # 1. Name: an exact or prefixed name match is the strongest signal,
            #    then a shared name token, then the dir_name (often mirrors name).
            if name == q:
                score += _SCORE_EXACT_NAME
            elif name.startswith(q) or q.startswith(name):
                score += _SCORE_NAME_PREFIX
            name_token_hits = len(q_tokens & _search_tokens(name))
            if name_token_hits:
                score += _SCORE_NAME_TOKEN * name_token_hits
                if name_token_hits >= len(q_tokens) and q_tokens:
                    # Every query word appears in the name — near-exact match.
                    score += _SCORE_NAME_PREFIX
            if dir_name and dir_name != name and q_tokens & _search_tokens(dir_name):
                score += _SCORE_DESCRIPTION_TOKEN
            # 2. Description: contains is a weak bonus; token overlap is
            #    normalized so a long description cannot outscore a name match.
            if q in description:
                score += _SCORE_DESCRIPTION_CONTAINS
            score += round(_SCORE_DESCRIPTION_TOKEN * 100 * _overlap_ratio(q_tokens, description))
            # 3. Whole-record substring (covers multi-word queries that span
            #    name+description, and non-latin queries).
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
                        "dir_name": entry.dir_name,
                        "description": entry.description,
                        "path": entry.path,
                        "source": entry.source,
                        "plugin_name": entry.plugin_name,
                        "score": score,
                    },
                )
            )
        # Name-hit entries first on equal score, so ties resolve toward the
        # skill the query actually named.
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [row[2] for row in scored[: max(1, min(top_k, 20))]]

    def read(self, name: str) -> str | None:
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
    text = text.lower()
    tokens = {t for t in re.findall(r"[a-z0-9_]+", text) if len(t) > 1}
    cjk = re.findall(r"[一-鿿]+", text)
    for run in cjk:
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


#: Module-level singleton, lazily populated by entries()/search().
index = ExternalSkillIndex()


def search_external_skills(query: str, top_k: int = 8) -> list[dict[str, Any]]:
    """Public hook for ``search_tools``. Returns [] unless external index is enabled."""
    if not enabled():
        return []
    return index.search(query, top_k=top_k)
