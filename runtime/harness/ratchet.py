"""Evidence-backed Ratchet candidates for non-repeating coding failures."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path

from runtime.coding.workspace_detect import detect_workspace

from .models import RatchetCandidate, Sensor
from .sensors import persist_sensor


class RatchetError(ValueError):
    """A ratchet transition or application was rejected."""


_STORE_LOCK = threading.RLock()


def _store_path(root: str | Path) -> Path:
    return Path(root).expanduser().resolve() / ".veya" / "harness" / "ratchet_candidates.json"


def _candidate_from_dict(raw: dict[str, object]) -> RatchetCandidate:
    sensor_raw = raw.get("proposed_sensor")
    sensor = Sensor(**sensor_raw) if isinstance(sensor_raw, dict) else None
    evidence_raw = raw.get("evidence_ids")
    evidence_ids = evidence_raw if isinstance(evidence_raw, list) else []
    return RatchetCandidate(
        id=str(raw["id"]),
        workspace_id=str(raw["workspace_id"]),
        source_task_id=str(raw["source_task_id"]),
        failure_class=str(raw["failure_class"]),
        observed_failure=str(raw["observed_failure"]),
        proposed_fix_layer=raw["proposed_fix_layer"],  # type: ignore[arg-type]
        proposed_rule=str(raw["proposed_rule"]) if raw.get("proposed_rule") is not None else None,
        proposed_sensor=sensor,
        evidence_ids=[str(item) for item in evidence_ids],
        status=raw.get("status", "candidate"),  # type: ignore[arg-type]
        applied_path=str(raw["applied_path"]) if raw.get("applied_path") else None,
    )


class RatchetStore:
    """Small task-scoped JSON store; it is not a replacement for durable runtime state."""

    def __init__(self, workspace_root: str | Path):
        self.root = Path(workspace_root).expanduser().resolve()
        self.path = _store_path(self.root)

    def list(self) -> list[RatchetCandidate]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RatchetError(f"invalid Ratchet store: {self.path}: {exc}") from exc
        if not isinstance(raw, list):
            raise RatchetError(f"Ratchet store must contain a list: {self.path}")
        return [_candidate_from_dict(item) for item in raw if isinstance(item, dict)]

    def save(self, candidates: Iterable[RatchetCandidate]) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(
                [candidate.to_dict() for candidate in candidates],
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(self.path)
        return self.path


def create_ratchet_candidate(
    *,
    workspace_id: str,
    source_task_id: str,
    failure_class: str,
    observed_failure: str,
    proposed_fix_layer: str,
    evidence_ids: Iterable[str],
    proposed_rule: str | None = None,
    proposed_sensor: Sensor | None = None,
) -> RatchetCandidate:
    evidence = [str(item).strip() for item in evidence_ids if str(item).strip()]
    if not evidence:
        raise RatchetError("RatchetCandidate requires at least one failure evidence id")
    if proposed_fix_layer not in {"guide", "sensor", "permission", "tool", "test"}:
        raise RatchetError(f"unsupported Ratchet fix layer: {proposed_fix_layer}")
    if not failure_class.strip() or not observed_failure.strip():
        raise RatchetError("failure_class and observed_failure are required")
    if proposed_fix_layer == "guide" and not (proposed_rule or "").strip():
        raise RatchetError("guide Ratchet candidates require proposed_rule")
    if proposed_fix_layer == "sensor" and proposed_sensor is None:
        raise RatchetError("sensor Ratchet candidates require proposed_sensor")
    return RatchetCandidate(
        id="ratchet-" + uuid.uuid4().hex[:16],
        workspace_id=workspace_id,
        source_task_id=source_task_id,
        failure_class=failure_class.strip(),
        observed_failure=observed_failure.strip()[:4000],
        proposed_fix_layer=proposed_fix_layer,  # type: ignore[arg-type]
        proposed_rule=proposed_rule.strip() if proposed_rule else None,
        proposed_sensor=proposed_sensor,
        evidence_ids=evidence,
    )


def record_candidate(root: str | Path, candidate: RatchetCandidate) -> RatchetCandidate:
    store = RatchetStore(root)
    with _STORE_LOCK:
        candidates = [item for item in store.list() if item.id != candidate.id]
        candidates.append(candidate)
        store.save(candidates)
    return candidate


def get_candidate(root: str | Path, candidate_id: str) -> RatchetCandidate:
    for candidate in RatchetStore(root).list():
        if candidate.id == candidate_id:
            return candidate
    raise RatchetError(f"Ratchet candidate not found: {candidate_id}")


def transition_candidate(root: str | Path, candidate_id: str, status: str) -> RatchetCandidate:
    if status not in {"approved", "rejected"}:
        raise RatchetError(f"invalid Ratchet transition: {status}")
    store = RatchetStore(root)
    with _STORE_LOCK:
        candidate = get_candidate(root, candidate_id)
        if candidate.status != "candidate":
            raise RatchetError(f"only candidate status can transition: {candidate.status}")
        candidate.status = status  # type: ignore[assignment]
        from datetime import UTC, datetime

        candidate.updated_at = datetime.now(UTC)
        store.save([candidate if item.id == candidate_id else item for item in store.list()])
    return candidate


def _append_guide_rule(root: Path, candidate: RatchetCandidate) -> Path:
    path = root / ".veya" / "GUIDES.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    marker = f"<!-- veya-ratchet:{candidate.id} -->"
    if marker not in existing:
        rule_lines = (candidate.proposed_rule or "").replace("\r", "").splitlines()
        safe_lines = [line.strip() for line in rule_lines if line.strip()]
        if not safe_lines:
            raise RatchetError("approved guide candidate has no rule text")
        block = (
            "\n## Ratchet rules\n"
            + marker
            + "\n"
            + "\n".join(f"- {line}" for line in safe_lines)
            + "\n"
        )
        path.write_text(existing.rstrip() + block, encoding="utf-8")
    return path


def apply_candidate(root: str | Path, candidate_id: str) -> RatchetCandidate:
    project_root = Path(root).expanduser().resolve()
    workspace = detect_workspace(project_root)
    store = RatchetStore(project_root)
    with _STORE_LOCK:
        candidate = get_candidate(project_root, candidate_id)
        if candidate.status != "approved":
            raise RatchetError(f"only approved Ratchet candidates can apply: {candidate.status}")
        if candidate.workspace_id != workspace.id:
            raise RatchetError("Ratchet candidate belongs to a different workspace")
        if candidate.proposed_fix_layer == "guide":
            applied_path = _append_guide_rule(project_root, candidate)
        elif candidate.proposed_fix_layer == "sensor" and candidate.proposed_sensor:
            applied_path = persist_sensor(project_root, candidate.proposed_sensor)
        else:
            raise RatchetError(
                f"PR harness apply supports guide/sensor only; candidate layer is {candidate.proposed_fix_layer}"
            )
        candidate.status = "applied"
        candidate.applied_path = str(applied_path)
        from datetime import UTC, datetime

        candidate.updated_at = datetime.now(UTC)
        store.save([candidate if item.id == candidate_id else item for item in store.list()])
    return candidate


def revert_candidate(root: str | Path, candidate_id: str) -> RatchetCandidate:
    """Revert a guide application by its marker; sensor rollback is explicit later."""
    project_root = Path(root).expanduser().resolve()
    store = RatchetStore(project_root)
    with _STORE_LOCK:
        candidate = get_candidate(project_root, candidate_id)
        if candidate.status != "applied" or not candidate.applied_path:
            raise RatchetError("only an applied candidate can be reverted")
        path = Path(candidate.applied_path).resolve()
        if path == (project_root / ".veya" / "GUIDES.md").resolve():
            lines = path.read_text(encoding="utf-8").splitlines()
            marker = f"<!-- veya-ratchet:{candidate.id} -->"
            filtered = [
                line
                for line in lines
                if line != marker
                and not line.strip().startswith("- " + (candidate.proposed_rule or ""))
            ]
            path.write_text("\n".join(filtered).rstrip() + "\n", encoding="utf-8")
        else:
            raise RatchetError(
                "sensor rollback is not implicit; remove the approved sensor explicitly"
            )
        candidate.status = "approved"
        candidate.applied_path = None
        store.save([candidate if item.id == candidate_id else item for item in store.list()])
    return candidate


__all__ = [
    "RatchetError",
    "RatchetStore",
    "apply_candidate",
    "create_ratchet_candidate",
    "get_candidate",
    "record_candidate",
    "revert_candidate",
    "transition_candidate",
]
