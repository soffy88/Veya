"""registries.skills — LEGACY-DEAD (SKILL_REGISTRY_AUTHORITY_CONVERGENCE).

Evidence: zero production importers (no ``registries.skills`` / ``register_skill``
imports outside this file; coordinator owns ``server.skill_hub.VeyaSkillHub``,
the remote plane owns ``veya.remote.skills.SkillRegistry`` as an adapter, and
the capability plane owns ``server.capability_model.SkillRegistry`` as a
projection). The single canonical write authority is
``server.skill_hub.VeyaSkillHub`` — see ``server.skill_authority``.

This module stays deactivated: every entry raises. Do NOT revive it (add a new
skill store) without an approved reason — that would re-fork skill identity.
"""

from __future__ import annotations

from collections.abc import Callable

#: Deactivation marker. True == this store accepts no reads/writes.
SKILL_AUTHORITY_ROLE = "LEGACY-DEAD"
_DEACTIVATED = True

_DEAD_MESSAGE = (
    "registries.skills is LEGACY-DEAD (zero production importers since the "
    "skill-authority convergence): use the canonical authority "
    "server.skill_hub.VeyaSkillHub for writes, veya.remote.skills / "
    "server.capability_model projections for reads. See server.skill_authority."
)


def register_skill(name: str, fn: Callable) -> None:
    raise RuntimeError(_DEAD_MESSAGE)


def get_skill(name: str) -> Callable | None:
    raise RuntimeError(_DEAD_MESSAGE)


def list_skills() -> list[str]:
    raise RuntimeError(_DEAD_MESSAGE)
