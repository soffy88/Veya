"""server.skill_authority — SKILL_REGISTRY_AUTHORITY_CONVERGENCE (single write authority).

Phase 0 found 3 live skill stores + 1 dead dict:

1. ``server.skill_hub.VeyaSkillHub`` ............ AUTHORITATIVE (this module declares it)
2. ``veya.remote.skills.SkillRegistry`` ......... ADAPTER (in-memory, no persistence)
3. ``server.capability_model.SkillRegistry`` ..... PROJECTION (JSON cache, bridged read-only)
4. ``registries.skills`` ......................... LEGACY-DEAD (zero production importers)

Exactly ONE write authority exists at the end of this convergence:
``server.skill_hub.VeyaSkillHub``. It owns skill identity (manifest ``name``),
mutation (``create_skill_package`` + ``reload_skills``/``_load_skill``) and
persistence (``$VEYA_SKILLS_DIR/<name>/manifest.json`` + entrypoint +
optional ``SKILL.md``). Everything else is a read-only projection or an
adapter that is (re)filled exclusively through the delegated sync helpers in
this module:

- :func:`register_canonical_skill` — the ONE canonical mutation entry.
- :func:`sync_capability_projection` — hub → capability_model projection.
- :func:`sync_remote_adapter` — hub → remote adapter cache.
- :func:`stage_skill_spec` / :func:`propose_teaching_candidate` — delegated
  staging writes for package import / teaching flows (identity-validated,
  never a second runtime identity).

This module is stdlib-only at import time on purpose: the adapter
(``veya.remote.skills``) and the projection (``server.capability_model``)
both import from here, so importing anything heavier would create cycles.
All hub/registry imports happen lazily inside functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: The single canonical skill write authority. Exactly one.
CANONICAL_SKILL_AUTHORITY = "server.skill_hub.VeyaSkillHub"

#: Evidence-based classification of every candidate store (not filenames).
CANDIDATE_CLASSIFICATION: dict[str, str] = {
    "server.skill_hub.VeyaSkillHub": "AUTHORITATIVE",
    "veya.remote.skills.SkillRegistry": "ADAPTER",
    "server.capability_model.SkillRegistry": "PROJECTION",
    "registries.skills": "LEGACY-DEAD",
}

#: Authority roles (evidence-based classification, see CANDIDATE_CLASSIFICATION).
SKILL_AUTHORITY_ROLE_AUTHORITATIVE = "AUTHORITATIVE"
SKILL_AUTHORITY_ROLE_ADAPTER = "ADAPTER"
SKILL_AUTHORITY_ROLE_PROJECTION = "PROJECTION"
SKILL_AUTHORITY_ROLE_LEGACY_DEAD = "LEGACY-DEAD"

#: Canonical persistence: hub skill manifests on the filesystem.
#: The capability JSON file and the remote in-memory dict are caches only.
PERSISTENCE_PATH = "$VEYA_SKILLS_DIR/<skill-name>/manifest.json"

_MAX_SKILL_ID_LEN = 128


class SkillAuthorityError(ValueError):
    """Raised when a non-canonical store is asked to mint/mutate skill identity.

    The message always points at the canonical path so callers know where
    the write belongs.
    """


def canonical_skill_id(name: Any) -> str:
    """Validate + normalize one canonical skill identity.

    Canonical identity is the hub manifest ``name`` string: projections must
    reuse it verbatim (``SkillSpec.skill_id`` / ``SkillRecord.skill_id`` ==
    hub executor key) and must never mint a second identity for the same
    skill. Only surrounding whitespace is normalized; anything that could
    escape the skill directory or collide ambiguously is rejected.
    """
    text = str(name or "").strip()
    if not text:
        raise SkillAuthorityError(
            "empty skill identity: register through "
            f"{CANONICAL_SKILL_AUTHORITY} with a non-empty manifest name"
        )
    if len(text) > _MAX_SKILL_ID_LEN:
        raise SkillAuthorityError(f"skill identity too long ({len(text)} chars): {text!r}")
    if text in (".", "..") or any(sep in text for sep in ("/", "\\", "\x00")):
        raise SkillAuthorityError(
            f"invalid skill identity {text!r}: must be a plain manifest name, "
            f"register through {CANONICAL_SKILL_AUTHORITY}"
        )
    return text


def register_canonical_skill(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    skills_dir: str | Path | None = None,
    code: str | None = None,
    skill_type: str = "python",
    entrypoint: str = "run.py",
    endpoint: str | None = None,
    hub: Any | None = None,
) -> str:
    """The ONE canonical skill mutation entry: persist manifest, reload hub.

    Writes ``$VEYA_SKILLS_DIR/<name>/manifest.json`` (+ entrypoint) via the
    hub's own package writer, then reloads the given hub (or the module
    singleton when importable) so the skill is immediately resolvable.
    Returns the canonical skill identity.
    """
    from server.skill_hub import create_skill_package

    skill_id = canonical_skill_id(name)
    if not str(description or "").strip():
        raise SkillAuthorityError("skill description must not be empty")
    if not isinstance(parameters, dict) or "properties" not in parameters:
        raise SkillAuthorityError("skill parameters must be a dict with 'properties'")
    create_skill_package(
        skill_id,
        str(description),
        dict(parameters),
        skills_dir=skills_dir,
        code=code,
        skill_type=skill_type,
        entrypoint=entrypoint,
        endpoint=endpoint,
    )
    target = hub
    if target is None:
        try:
            from server.skill_hub import skill_hub as _singleton

            target = _singleton
        except Exception:
            target = None
    if target is not None and hasattr(target, "reload_skills"):
        target.reload_skills()
    return skill_id


def hub_skill_to_spec(name: str, *, instructions: str, provenance: str) -> Any:
    """Build a capability-projection SkillSpec reusing the canonical identity."""
    from server.capability_model import SkillSpec

    skill_id = canonical_skill_id(name)
    return SkillSpec(
        skill_id=skill_id,
        instructions=instructions,
        provenance=provenance or f"{CANONICAL_SKILL_AUTHORITY}({skill_id})",
        status="candidate",
    )


def hub_skill_to_record(
    name: str,
    *,
    description: str = "",
    root: str = "",
    trust_level: str = "PROJECT_LOCAL",
) -> Any:
    """Build a remote-adapter SkillRecord reusing the canonical identity."""
    from veya.remote.skills import SkillRecord

    skill_id = canonical_skill_id(name)
    return SkillRecord(
        skill_id=skill_id,
        name=skill_id,
        description=description,
        triggers=[skill_id],
        source=f"canonical:{CANONICAL_SKILL_AUTHORITY}:{skill_id}",
        trust_level=trust_level,
        root=root,
    )


def sync_capability_projection(hub: Any, *, registry: Any | None = None) -> int:
    """Refill the capability projection from the canonical hub (delegated write).

    With ``registry=None`` this delegates to the existing bridge
    ``sync_skills_from_hub`` (module singleton); otherwise the given registry
    is refilled spec-by-spec through its delegated ``sync_from_canonical``.
    Returns the number of projected skills.
    """
    if registry is None:
        from server.capability_model import sync_skills_from_hub

        return sync_skills_from_hub(hub)
    stats = hub.get_stats()
    names: list[str] = list(stats.get("skills", []))
    specs = [
        hub_skill_to_spec(
            name,
            instructions=hub.describe(name),
            provenance=f"{CANONICAL_SKILL_AUTHORITY}({hub.skills_dir})",
        )
        for name in names
    ]
    registry.sync_from_canonical(specs)
    return len(specs)


def sync_remote_adapter(registry: Any, hub: Any) -> int:
    """Refill a remote adapter registry from the canonical hub (delegated write)."""
    stats = hub.get_stats()
    names: list[str] = list(stats.get("skills", []))
    records = []
    for name in names:
        source_path = ""
        accessor = getattr(hub, "skill_source_path", None)
        if callable(accessor):
            source_path = str(accessor(name) or "")
        records.append(hub_skill_to_record(name, description=hub.describe(name), root=source_path))
    registry.sync_from_canonical(records)
    return len(records)


def stage_skill_spec(registry: Any, spec: Any) -> None:
    """Delegated staging write for package-import flows (identity-validated).

    Direct ``registry.register_candidate(spec)`` without delegation is sealed;
    importers must come through here so the canonical identity rule is enforced
    at the single choke point.
    """
    canonical_skill_id(spec.skill_id)
    registry.register_candidate(spec, via_canonical=True)


def propose_teaching_candidate(
    registry: Any, description: str, config: dict[str, Any] | None = None
) -> Any:
    """Delegated teaching-flow mint (staged candidate, never runtime-resolvable).

    Teaching candidates stay ``status=candidate`` and are excluded from the
    registry's default contract query; they become runtime skills only via
    :func:`register_canonical_skill` + projection sync.
    """
    if not str(description or "").strip():
        raise SkillAuthorityError("teaching description must not be empty")
    return registry.propose_skill(description, config, via_canonical=True)
