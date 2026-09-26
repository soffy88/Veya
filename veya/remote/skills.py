"""Skill registry + governance with progressive disclosure (plane C).

Never load hundreds of ``SKILL.md`` at once. Three levels:

* Level 1 (METADATA): name + description + triggers — the default.
* Level 2 (CORE): the SKILL.md workflow body, loaded on selection.
* Level 3 (RESOURCES): references/scripts/assets, loaded on demand.

A skill declares its permissions; it can never escalate them. Third-party skills
are ``UNTRUSTED_CONTENT`` with recorded provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any


class SkillLevel(IntEnum):
    METADATA = 1
    CORE = 2
    RESOURCES = 3


class SkillPermission(StrEnum):
    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    SHELL = "shell"
    NETWORK = "network"
    GITHUB = "github"
    BROWSER = "browser"
    SECRET_REFERENCE = "secret_reference"


@dataclass
class SkillRecord:
    skill_id: str
    name: str
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    version: str = "0.0.0"
    source: str = ""
    source_commit: str = ""
    license: str = "UNKNOWN"
    capabilities_required: list[str] = field(default_factory=list)
    permissions_required: list[str] = field(default_factory=list)
    compatible_workers: list[str] = field(default_factory=list)
    trust_level: str = "UNTRUSTED_CONTENT"
    eval_status: str = "UNKNOWN"
    root: str = ""

    def metadata(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "triggers": list(self.triggers),
            "trust_level": self.trust_level,
            "eval_status": self.eval_status,
        }

    def provenance(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "source": self.source,
            "source_commit": self.source_commit,
            "license": self.license,
            "permissions": list(self.permissions_required),
            "trust_level": self.trust_level,
        }


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, SkillRecord] = {}
        self._loaded_core: dict[str, str] = {}

    def register(self, record: SkillRecord) -> None:
        self._skills[record.skill_id] = record

    def get(self, skill_id: str) -> SkillRecord | None:
        return self._skills.get(skill_id)

    def metadata(self) -> list[dict[str, Any]]:
        """Level 1 only — safe to inject into an initial context."""

        return [record.metadata() for record in self._skills.values()]

    def load_core(self, skill_id: str) -> str:
        record = self._require(skill_id)
        path = Path(record.root) / "SKILL.md"
        if not path.is_file():
            raise SkillError("SKILL_CORE_MISSING", skill_id)
        content = path.read_text(encoding="utf-8")
        self._loaded_core[skill_id] = content
        return content

    def loaded_core(self, skill_id: str) -> str:
        """Return the Level-2 body loaded during this execution."""

        return self._loaded_core.get(skill_id, "")

    def load_resources(self, skill_id: str) -> dict[str, str]:
        record = self._require(skill_id)
        resources: dict[str, str] = {}
        for sub in ("references", "scripts", "assets"):
            base = Path(record.root) / sub
            if base.is_dir():
                for path in sorted(base.rglob("*")):
                    if path.is_file():
                        resources[str(path.relative_to(record.root))] = str(path)
        return resources

    def resolve(self, text: str) -> dict[str, Any]:
        """Trigger evaluation with explicit selected/declined reasons."""

        haystack = (text or "").lower()
        candidates: list[str] = []
        selected: list[dict[str, Any]] = []
        declined: list[dict[str, Any]] = []
        for record in self._skills.values():
            hits = [t for t in record.triggers if t.lower() in haystack]
            if hits:
                candidates.append(record.skill_id)
                selected.append({"skill_id": record.skill_id, "reason": f"trigger:{hits[0]}"})
            else:
                declined.append({"skill_id": record.skill_id, "reason": "no_trigger"})
        return {"candidates": candidates, "selected": selected, "declined": declined}

    def check_permissions(self, skill_id: str, allowed: list[str]) -> tuple[bool, list[str]]:
        record = self._require(skill_id)
        allowed_set = {str(a) for a in allowed}
        missing = [p for p in record.permissions_required if p not in allowed_set]
        return (not missing), missing

    def health(self, skill_id: str) -> dict[str, Any]:
        record = self._require(skill_id)
        core = Path(record.root) / "SKILL.md"
        return {
            "skill_id": skill_id,
            "schema_ok": bool(record.name and record.description),
            "core_present": core.is_file(),
            "permissions": list(record.permissions_required),
            "eval_status": record.eval_status,
            "trust_level": record.trust_level,
        }

    def _require(self, skill_id: str) -> SkillRecord:
        record = self._skills.get(skill_id)
        if record is None:
            raise SkillError("UNKNOWN_SKILL", skill_id)
        return record


class SkillError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = ["SkillError", "SkillLevel", "SkillPermission", "SkillRecord", "SkillRegistry"]
