"""Capability authoring pipeline (D7, Veya project-layer coordinator).

ONE authoring authority chaining the frozen substrates end to end:

Requirement -> CapabilityDraft -> isolated workspace compile (D4)
-> CandidateArtifact -> CapabilityRegistry.register_candidate
-> qualification (registry verify + D6 skill gate + hash re-check)
-> explicit promotion -> runtime lookup/exposure.

Boundaries (frozen):
- compile != verify != promote. No auto-transitions anywhere.
- The compiler writes ONLY the isolated workspace (never canonical 3O,
  never tracked production source) and never commits.
- Skill references pass the D6 distribution materialize gate (active +
  trusted + bound); no parallel trust logic lives here.
- Selection / top-k / semantic matching / token budgets stay D9 (untouched).
- Failed candidates and qualification evidence are retained, never deleted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "AUTHORING_EVENT_TOPICS",
    "ArtifactFile",
    "CandidateArtifact",
    "CapabilityAuthoring",
    "CapabilityDraft",
]

AUTHORING_EVENT_TOPICS = (
    "capability.drafted",
    "capability.compiled",
    "capability.qualified",
    "capability.promoted",
    "capability.rejected",
    "capability.revoked",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CapabilityDraft:
    """An authored requirement not yet compiled (value object)."""

    draft_id: str
    requirement: str
    capability_id: str
    skills: tuple[str, ...] = ()
    domain: str = ""
    evaluators: tuple[str, ...] = ()
    benchmark_suite: str | None = None
    status: str = "draft"  # draft | compiled | failed
    created_at: str = field(default_factory=_now_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.draft_id.strip() or not self.requirement.strip():
            raise ValueError("CapabilityDraft needs draft_id and requirement")
        if not self.capability_id.strip():
            raise ValueError("CapabilityDraft needs capability_id")
        object.__setattr__(self, "skills", tuple(self.skills))
        object.__setattr__(self, "evaluators", tuple(self.evaluators))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "requirement": self.requirement,
            "capability_id": self.capability_id,
            "skills": list(self.skills),
            "domain": self.domain,
            "evaluators": list(self.evaluators),
            "benchmark_suite": self.benchmark_suite,
            "status": self.status,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CapabilityDraft:
        raw = dict(data)
        raw["skills"] = tuple(raw.get("skills") or ())
        raw["evaluators"] = tuple(raw.get("evaluators") or ())
        raw["metadata"] = dict(raw.get("metadata") or {})
        return cls(**raw)


@dataclass(frozen=True)
class ArtifactFile:
    """One compiled file reference with its content hash."""

    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class CandidateArtifact:
    """A compiled candidate (hashes + refs, never bodies)."""

    artifact_id: str
    draft_id: str
    capability_id: str
    version: int
    package_hash: str
    files: tuple[ArtifactFile, ...] = ()
    workspace_id: str | None = None
    supersedes: str | None = None
    qualification: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if not self.artifact_id.strip() or not self.package_hash.strip():
            raise ValueError("CandidateArtifact needs artifact_id and package_hash")
        object.__setattr__(
            self,
            "files",
            tuple(
                item if isinstance(item, ArtifactFile) else ArtifactFile(**dict(item))
                for item in self.files
            ),
        )
        object.__setattr__(self, "qualification", dict(self.qualification))

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "draft_id": self.draft_id,
            "capability_id": self.capability_id,
            "version": self.version,
            "package_hash": self.package_hash,
            "files": [
                {"path": item.path, "sha256": item.sha256, "size_bytes": item.size_bytes}
                for item in self.files
            ],
            "workspace_id": self.workspace_id,
            "supersedes": self.supersedes,
            "qualification": dict(self.qualification),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CandidateArtifact:
        raw = dict(data)
        raw["files"] = tuple(raw.get("files") or ())
        raw["qualification"] = dict(raw.get("qualification") or {})
        return cls(**raw)


def _render_package(draft: CapabilityDraft) -> dict[str, str]:
    """Render deterministic package file contents (no side effects)."""
    manifest_lines = [
        f"id: {draft.capability_id}",
        f"domain: {draft.domain}",
        f"description: {draft.requirement.strip()}",
        "skills:",
        *[f"  - {skill}" for skill in draft.skills],
        "evaluators:",
        *[f"  - {evaluator}" for evaluator in draft.evaluators],
    ]
    return {
        "CAPABILITY.yaml": "\n".join(manifest_lines) + "\n",
        "SKILL_REFS.md": f"# {draft.capability_id}\n\n{draft.requirement.strip()}\n",
    }


class CapabilityAuthoring:
    """Authoring pipeline coordinator (single authority).

    Owns exactly: drafts, candidate artifacts, promotion ledger. Capability
    metadata/verify state stays in ``CapabilityRegistry``; files stay in the
    isolated workspace + ``ArtifactStore``; skill trust stays in D6.
    """

    STORE_FILENAME = "capability_authoring.json"

    def __init__(
        self,
        store_root: str | Path,
        *,
        emit: Callable[[str, dict[str, Any]], Any] | None = None,
        registry: Any | None = None,
        skill_registry: Any | None = None,
    ) -> None:
        self.store_root = Path(store_root).expanduser().resolve()
        self._emit = emit
        self._registry_override = registry
        self._skill_registry_override = skill_registry

    def _registry(self) -> Any:
        if self._registry_override is not None:
            return self._registry_override
        from server.capability_model import CapabilityRegistry

        return CapabilityRegistry()

    def _skill_registry(self) -> Any:
        if self._skill_registry_override is not None:
            return self._skill_registry_override
        from server.capability_model import skill_registry

        return skill_registry

    def _artifact_file_path(self, artifact: CandidateArtifact, name: str) -> Path:
        return (
            self.store_root
            / ".veya"
            / "runs"
            / f"cap-{artifact.draft_id}"
            / "package"
            / artifact.capability_id
            / name
        )

    # -- state ----------------------------------------------------------
    def _path(self) -> Path:
        return self.store_root / self.STORE_FILENAME

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"drafts": {}, "artifacts": {}, "promotions": {}}
        if not isinstance(data, dict):
            return {"drafts": {}, "artifacts": {}, "promotions": {}}
        return {
            "drafts": dict(data.get("drafts") or {}),
            "artifacts": dict(data.get("artifacts") or {}),
            "promotions": dict(data.get("promotions") or {}),
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _event(self, topic: str, payload: Mapping[str, Any]) -> None:
        if self._emit is None:
            return
        self._emit(topic, dict(payload))

    # -- 1. draft ---------------------------------------------------------
    def create_draft(
        self,
        requirement: str,
        capability_id: str,
        *,
        skills: list[str] | tuple[str, ...] = (),
        domain: str = "",
        evaluators: list[str] | tuple[str, ...] = (),
        benchmark_suite: str | None = None,
        draft_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> CapabilityDraft:
        """Record a requirement as a draft (no compilation, no registration)."""
        import uuid as _uuid

        draft = CapabilityDraft(
            draft_id=draft_id or f"draft-{_uuid.uuid4().hex[:12]}",
            requirement=requirement,
            capability_id=capability_id,
            skills=tuple(skills),
            domain=domain,
            evaluators=tuple(evaluators),
            benchmark_suite=benchmark_suite,
            metadata=dict(metadata or {}),
        )
        state = self._load_state()
        state["drafts"][draft.draft_id] = draft.to_dict()
        self._save_state(state)
        self._event("capability.drafted", draft.to_dict())
        return draft

    # -- 2. isolated compile -----------------------------------------------
    def compile_draft(
        self,
        draft_id: str,
        *,
        workspace_root: str | Path,
        managed_root: str | Path | None = None,
        version: int | None = None,
        supersedes: str | None = None,
    ) -> CandidateArtifact:
        """Compile a draft inside a D4-isolated workspace into an artifact.

        A fresh managed workspace is created/attached/prepared per draft
        (isolation discipline + containment guard); the compiled package
        bytes land in the durable authoring ``ArtifactStore`` (hash-
        addressed, manifest-written). Registers the candidate with
        ``CapabilityRegistry``. Never verifies, never promotes, never
        touches tracked production source.
        """
        from runtime.coding.workspace_lifecycle import (
            WorkspaceStore,
            attach_workspace,
            create_workspace,
            prepare_workspace,
        )
        from runtime.execution.artifacts import ArtifactStore

        state = self._load_state()
        raw = state["drafts"].get(draft_id)
        if raw is None:
            raise ValueError(f"unknown draft: {draft_id!r}")
        draft = CapabilityDraft.from_dict(raw)

        root = Path(workspace_root).expanduser().resolve()
        if managed_root is not None:
            managed = Path(managed_root).expanduser().resolve()
            if root != managed and managed not in root.parents:
                raise ValueError(f"compile workspace escapes managed root: {root}")

        ws_store = WorkspaceStore(self.store_root / "workspaces")
        workspace_id = f"ws-{draft_id}"
        create_workspace(
            ws_store,
            workspace_id=workspace_id,
            owner_scope="capability-compile",
            root=str(root),
            kind="local",
            metadata={"draft_id": draft_id},
        )
        attach_workspace(ws_store, workspace_id, f"compile:{draft_id}")
        prepare_workspace(ws_store, workspace_id)

        artifact_store = ArtifactStore(self.store_root, f"cap-{draft_id}")
        files: list[ArtifactFile] = []
        rendered = _render_package(draft)
        for name in sorted(rendered):
            target = artifact_store.path(f"package/{draft.capability_id}/{name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered[name], encoding="utf-8")
            ref = artifact_store.register(
                f"package/{draft.capability_id}/{name}", kind="file", producer="authoring"
            )
            files.append(
                ArtifactFile(path=name, sha256=ref.sha256 or "", size_bytes=ref.size_bytes or 0)
            )
        artifact_store.write_manifest()
        package_hash = _sha256_text("".join(f"{item.path}:{item.sha256}\n" for item in files))
        prior_versions = [
            int(item.get("version") or 0)
            for item in state["artifacts"].values()
            if isinstance(item, dict) and item.get("capability_id") == draft.capability_id
        ]
        resolved_version = version if version is not None else (max(prior_versions or [0]) + 1)
        artifact = CandidateArtifact(
            artifact_id=f"{draft.capability_id}-v{resolved_version}",
            draft_id=draft_id,
            capability_id=draft.capability_id,
            version=resolved_version,
            package_hash=package_hash,
            files=tuple(files),
            workspace_id=workspace_id,
            supersedes=supersedes,
        )
        self._register_candidate(draft, artifact)
        state = self._load_state()
        state["artifacts"][artifact.artifact_id] = artifact.to_dict()
        raw_draft = state["drafts"][draft_id]
        raw_draft["status"] = "compiled"
        state["drafts"][draft_id] = raw_draft
        self._save_state(state)
        self._event("capability.compiled", artifact.to_dict())
        return artifact

    def _register_candidate(self, draft: CapabilityDraft, artifact: CandidateArtifact) -> None:
        from server.capability_model import CapabilitySpec

        registry = self._registry()
        registry.register_candidate(
            CapabilitySpec(
                capability_id=draft.capability_id,
                domain=draft.domain,
                description=draft.requirement,
                skills=list(draft.skills),
                evaluators=list(draft.evaluators),
                benchmark_suite=draft.benchmark_suite,
                provenance=f"capability_authoring:{artifact.artifact_id}",
            )
        )

    # -- 4. qualification -----------------------------------------------------
    async def qualify_capability(self, capability_id: str) -> dict[str, Any]:
        """Qualify the latest compiled artifact (no promotion).

        Checks, in order: registry candidate present, artifact hash matches
        the files on disk (tamper evidence), evaluators/benchmark declared,
        referenced skills pass the D6 materialize gate, then registry
        ``verify()``. Failures keep the candidate plus evidence.
        """
        from server.skill_distribution import SkillDistribution

        registry = self._registry()
        spec = registry.get(capability_id)
        if spec is None:
            return {
                "capability_id": capability_id,
                "qualified": False,
                "reasons": ["not registered"],
            }
        state = self._load_state()
        artifacts = [
            CandidateArtifact.from_dict(item)
            for item in state["artifacts"].values()
            if isinstance(item, dict) and item.get("capability_id") == capability_id
        ]
        if not artifacts:
            return {
                "capability_id": capability_id,
                "qualified": False,
                "reasons": ["no compiled artifact"],
            }
        artifact = max(artifacts, key=lambda item: item.version)
        tamper = self._verify_artifact_files(artifact)
        if tamper:
            self._record_qualification(artifact, False, [f"tamper: {tamper}"])
            self._event(
                "capability.qualified",
                {
                    "capability_id": capability_id,
                    "artifact_id": artifact.artifact_id,
                    "qualified": False,
                    "reasons": [f"tamper: {tamper}"],
                },
            )
            return {
                "capability_id": capability_id,
                "qualified": False,
                "reasons": [f"tamper: {tamper}"],
                "artifact_id": artifact.artifact_id,
            }
        if not spec.evaluators or not spec.benchmark_suite:
            self._record_qualification(artifact, False, ["evaluators/benchmark missing"])
            return {
                "capability_id": capability_id,
                "qualified": False,
                "reasons": ["evaluators/benchmark missing"],
                "artifact_id": artifact.artifact_id,
            }
        distribution = SkillDistribution(
            store_root=self.store_root / "skills", registry=self._skill_registry()
        )
        untrusted: list[str] = []
        for skill_id in spec.skills:
            try:
                await distribution.materialize(
                    skill_id, "capability", capability_id, backend="registry"
                )
            except Exception as exc:
                untrusted.append(f"{skill_id}: {exc}")
        if untrusted:
            self._record_qualification(artifact, False, untrusted)
            self._event(
                "capability.qualified",
                {
                    "capability_id": capability_id,
                    "artifact_id": artifact.artifact_id,
                    "qualified": False,
                    "reasons": untrusted,
                },
            )
            return {
                "capability_id": capability_id,
                "qualified": False,
                "reasons": untrusted,
                "artifact_id": artifact.artifact_id,
            }
        if not registry.verify(capability_id):
            self._record_qualification(artifact, False, ["registry verify refused"])
            return {
                "capability_id": capability_id,
                "qualified": False,
                "reasons": ["registry verify refused"],
                "artifact_id": artifact.artifact_id,
            }
        self._record_qualification(artifact, True, ["verified"])
        self._event(
            "capability.qualified",
            {
                "capability_id": capability_id,
                "artifact_id": artifact.artifact_id,
                "qualified": True,
                "reasons": ["verified"],
            },
        )
        return {
            "capability_id": capability_id,
            "qualified": True,
            "reasons": ["verified"],
            "artifact_id": artifact.artifact_id,
        }

    def _verify_artifact_files(self, artifact: CandidateArtifact) -> str:
        """Re-hash artifact files; return '' when intact, else a reason."""
        import hashlib as _hashlib

        for item in artifact.files:
            found = self._artifact_file_path(artifact, item.path)
            if not found.is_file():
                return f"missing file {item.path!r}"
            digest = _hashlib.sha256(found.read_bytes()).hexdigest()
            if digest != item.sha256:
                return f"hash mismatch {item.path!r}"
        return ""

    def _record_qualification(
        self, artifact: CandidateArtifact, passed: bool, reasons: list[str]
    ) -> None:
        state = self._load_state()
        stored = state["artifacts"].get(artifact.artifact_id)
        if isinstance(stored, dict):
            stored["qualification"] = {"passed": passed, "reasons": reasons, "at": _now_iso()}
            state["artifacts"][artifact.artifact_id] = stored
            self._save_state(state)

    # -- 5. explicit promotion --------------------------------------------------
    async def promote_capability(
        self, capability_id: str, *, by: str = "operator"
    ) -> dict[str, Any]:
        """Explicitly promote the qualified artifact to runtime-visible.

        Re-checks everything at promotion time: registry still VERIFIED,
        artifact hash still matches, referenced skills still materializable.
        Any tamper or drift BLOCKS promotion — never auto-advances.
        Referenced skills are bound to the capability scope first, so the
        materialize gate below enforces the D6 binding path end to end.
        """
        registry = self._registry()
        spec = registry.get(capability_id)
        if spec is None or spec.status != "verified":
            raise ValueError(
                f"promotion refused: {capability_id!r} is not VERIFIED "
                f"({spec.status if spec else 'missing'})"
            )
        state = self._load_state()
        artifacts = [
            CandidateArtifact.from_dict(item)
            for item in state["artifacts"].values()
            if isinstance(item, dict) and item.get("capability_id") == capability_id
        ]
        if not artifacts:
            raise ValueError(f"promotion refused: no artifact for {capability_id!r}")
        artifact = max(artifacts, key=lambda item: item.version)
        qualified = artifact.qualification.get("passed") is True
        if not qualified:
            raise ValueError(
                f"promotion refused: artifact {artifact.artifact_id!r} never qualified"
            )
        tamper = self._verify_artifact_files(artifact)
        if tamper:
            raise ValueError(f"promotion refused: tamper detected ({tamper})")
        from server.skill_distribution import SkillDistribution

        distribution = SkillDistribution(
            store_root=self.store_root / "skills", registry=self._skill_registry()
        )
        for skill_id in spec.skills:
            await distribution.bind(skill_id, "capability", capability_id, backend="registry")
            await distribution.materialize(
                skill_id, "capability", capability_id, backend="registry"
            )
        state = self._load_state()
        state["promotions"][capability_id] = {
            "artifact_id": artifact.artifact_id,
            "version": artifact.version,
            "package_hash": artifact.package_hash,
            "promoted_at": _now_iso(),
            "promoted_by": by,
            "revoked_at": None,
        }
        self._save_state(state)
        self._event(
            "capability.promoted",
            {
                "capability_id": capability_id,
                "artifact_id": artifact.artifact_id,
                "version": artifact.version,
            },
        )
        return {
            "capability_id": capability_id,
            "status": "promoted",
            "artifact_id": artifact.artifact_id,
            "version": artifact.version,
        }

    def reject_capability(self, capability_id: str, *, reason: str = "rejected") -> dict[str, Any]:
        """Reject: deprecate in registry, keep candidate + evidence."""

        self._registry().deprecate(capability_id)
        self._event(
            "capability.rejected",
            {
                "capability_id": capability_id,
                "reason": reason,
            },
        )
        return {"capability_id": capability_id, "status": "rejected", "reason": reason}

    def revoke_capability(self, capability_id: str, *, reason: str = "revoked") -> dict[str, Any]:
        """Revoke a promotion: unbind skills, mark promotion revoked, keep history."""
        from server.skill_distribution import SkillDistribution

        self._registry().deprecate(capability_id)
        distribution = SkillDistribution(
            store_root=self.store_root / "skills", registry=self._skill_registry()
        )
        for binding in distribution.bindings_for_scope("capability", capability_id):
            distribution.unbind(binding.skill_id, "capability", capability_id)
        state = self._load_state()
        promotion = state["promotions"].get(capability_id)
        if isinstance(promotion, dict):
            promotion["revoked_at"] = _now_iso()
            promotion["revoke_reason"] = reason
            state["promotions"][capability_id] = promotion
            self._save_state(state)
        self._event(
            "capability.revoked",
            {
                "capability_id": capability_id,
                "reason": reason,
            },
        )
        return {"capability_id": capability_id, "status": "revoked", "reason": reason}

    # -- 6. runtime lookup --------------------------------------------------------
    async def runtime_capabilities(self) -> list[dict[str, Any]]:
        """Re-validated runtime-visible capabilities (refs, never bodies).

        Every entry is re-checked live: promotion not revoked, registry
        still VERIFIED, artifact hash intact, skills still materializable.
        Stale entries are excluded (never deleted).
        """
        from server.skill_distribution import SkillDistribution

        registry = self._registry()
        distribution = SkillDistribution(
            store_root=self.store_root / "skills", registry=self._skill_registry()
        )
        visible: list[dict[str, Any]] = []
        state = self._load_state()
        for capability_id, promotion in state["promotions"].items():
            if not isinstance(promotion, dict) or promotion.get("revoked_at"):
                continue
            spec = registry.get(capability_id)
            if spec is None or spec.status != "verified":
                continue
            artifact_id = promotion.get("artifact_id")
            raw = state["artifacts"].get(artifact_id) if artifact_id else None
            if not isinstance(raw, dict):
                continue
            artifact = CandidateArtifact.from_dict(raw)
            if self._verify_artifact_files(artifact):
                continue
            try:
                for skill_id in spec.skills:
                    await distribution.materialize(
                        skill_id, "capability", capability_id, backend="registry"
                    )
            except Exception:
                continue
            visible.append(
                {
                    "capability_id": capability_id,
                    "version": promotion.get("version"),
                    "artifact_id": artifact.artifact_id,
                    "package_hash": artifact.package_hash,
                    "skills": list(spec.skills),
                }
            )
        return visible
