"""Skill registry + governance with progressive disclosure (plane C).

ADAPTER role (SKILL_REGISTRY_AUTHORITY_CONVERGENCE): this registry is a
read-only-at-rest adapter over the canonical authority
``server.skill_hub.VeyaSkillHub``. It mints no skill identity of its own —
entries arrive exclusively through :meth:`SkillRegistry.sync_from_canonical`
(delegated sync; identity validated by ``server.skill_authority``). Direct
:meth:`SkillRegistry.register` is sealed and raises
``SkillAuthorityError``. The store is in-memory only (rebuilt per process);
progressive disclosure reads Level-2/Level-3 bodies from skill roots on
demand and never preloads them.

Never load hundreds of ``SKILL.md`` at once. Three levels:

* Level 1 (METADATA): name + description + triggers — the default.
* Level 2 (CORE): the SKILL.md workflow body, loaded on selection.
* Level 3 (RESOURCES): references/scripts/assets, loaded on demand.

A skill declares its permissions; it can never escalate them. Third-party skills
are ``UNTRUSTED_CONTENT`` with recorded provenance.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

from server.skill_authority import (
    CANONICAL_SKILL_AUTHORITY,
    SKILL_AUTHORITY_ROLE_ADAPTER,
    SkillAuthorityError,
    canonical_skill_id,
)

#: Authority role of this store. Not the write authority (see server.skill_authority).
SKILL_AUTHORITY_ROLE = SKILL_AUTHORITY_ROLE_ADAPTER


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
    """Read-only-at-rest adapter over the canonical skill authority.

    Production fill path: ``server.skill_authority.sync_remote_adapter``
    (or :meth:`sync_from_canonical` directly). ``register`` is sealed: there
    is exactly one write authority (``server.skill_hub.VeyaSkillHub``) and
    this adapter must not mint a second skill identity.
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillRecord] = {}
        self._loaded_core: dict[str, str] = {}
        self._canonical_ids: set[str] = set()

    def register(self, record: SkillRecord) -> None:
        raise SkillAuthorityError(
            f"SkillRegistry.register is sealed: {type(record).__name__} identity "
            f"({getattr(record, 'skill_id', '?')!r}) must come from the canonical "
            f"authority {CANONICAL_SKILL_AUTHORITY} via "
            "SkillRegistry.sync_from_canonical (see server.skill_authority)"
        )

    def sync_from_canonical(self, records: Iterable[SkillRecord]) -> int:
        """Delegated fill from the canonical authority (the only write path).

        Every record's ``skill_id`` is validated against the canonical
        identity rule; invalid identities are rejected, never stored.
        Idempotent: re-syncing overwrites, so the adapter cannot fork.
        Returns the number of synced skills.
        """
        items = list(records)
        synced: dict[str, SkillRecord] = {}
        for record in items:
            skill_id = canonical_skill_id(record.skill_id)
            if skill_id != record.skill_id:
                record.skill_id = skill_id
            synced[skill_id] = record
        self._skills = synced
        self._loaded_core = {
            skill_id: body for skill_id, body in self._loaded_core.items() if skill_id in synced
        }
        self._canonical_ids = set(synced)
        return len(synced)

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
