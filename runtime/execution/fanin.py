"""Structured fan-in for complete and incomplete delegated work."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .models import ArtifactRef, Assertion, DelegateResult, Evidence


def _fingerprint(value: object) -> str:
    if isinstance(value, str):
        raw = value
    else:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _evidence_key(item: Evidence) -> str:
    return item.sha256 or _fingerprint({"source": item.source, "content": item.content})


def _assertion_key(item: Assertion) -> str:
    return _fingerprint(item.statement.strip().lower())


def _artifact_key(item: ArtifactRef) -> str:
    return item.sha256 or item.path


@dataclass
class FanInBatch:
    results: list[DelegateResult] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    assertions: list[Assertion] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    complete_count: int = 0
    partial_count: int = 0
    failed_count: int = 0
    paused_count: int = 0
    cancelled_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [item.to_dict() for item in self.results],
            "evidence": [item.to_dict() for item in self.evidence],
            "assertions": [item.to_dict() for item in self.assertions],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "complete_count": self.complete_count,
            "partial_count": self.partial_count,
            "failed_count": self.failed_count,
            "paused_count": self.paused_count,
            "cancelled_count": self.cancelled_count,
        }


def fan_in(results: list[DelegateResult]) -> FanInBatch:
    """Aggregate all useful child output, including output from failures."""
    batch = FanInBatch(results=list(results))
    evidence_by_key: dict[str, Evidence] = {}
    assertion_by_key: dict[str, Assertion] = {}
    artifact_by_key: dict[str, ArtifactRef] = {}
    for result in results:
        if result.status == "complete":
            batch.complete_count += 1
        elif result.status == "partial":
            batch.partial_count += 1
        elif result.status == "failed":
            batch.failed_count += 1
        elif result.status == "paused":
            batch.paused_count += 1
        elif result.status == "cancelled":
            batch.cancelled_count += 1
        for item in result.evidence:
            evidence_by_key.setdefault(_evidence_key(item), item)
        for item in result.assertions:
            assertion_by_key.setdefault(_assertion_key(item), item)
        for item in result.artifacts:
            artifact_by_key.setdefault(_artifact_key(item), item)
    batch.evidence = list(evidence_by_key.values())
    batch.assertions = list(assertion_by_key.values())
    batch.artifacts = list(artifact_by_key.values())
    return batch


def reconcile_multi_bot_results(
    results: list[DelegateResult],
    *,
    owner_bot_id: str,
    owner_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect cross-bot conflicts and require the requesting owner to resolve.

    This extends the existing fan-in projection; it is not a consensus or
    election protocol.  Every conflicting assertion, proposed action, and
    side-effect intent is retained.  A resolution is valid only when supplied
    by ``owner_bot_id`` (Bot A's MasterAgent), and is never treated as an
    acceptance verdict.
    """
    if not owner_bot_id:
        raise ValueError("owner_bot_id is required")
    batch = fan_in(results)
    conflicts: list[dict[str, Any]] = []

    def compare_group(kind: str, values: list[tuple[str, Any]]) -> None:
        grouped: dict[str, list[tuple[str, Any]]] = {}
        for key, value in values:
            grouped.setdefault(key, []).append(value)
        for key, members in grouped.items():
            fingerprints = {
                _fingerprint(member.to_dict() if hasattr(member, "to_dict") else member)
                for member in members
            }
            if len(fingerprints) > 1:
                conflicts.append(
                    {
                        "kind": kind,
                        "key": key,
                        "proposals": [
                            member.to_dict() if hasattr(member, "to_dict") else member
                            for member in members
                        ],
                    }
                )

    compare_group(
        "assertion",
        [(str(item.id), item) for result in results for item in result.assertions],
    )
    compare_group(
        "action",
        [
            (str(item.get("logical_operation") or item.get("action_id") or _fingerprint(item)), item)
            for result in results
            for item in result.proposed_actions
        ],
    )
    compare_group(
        "evidence",
        [
            (str(item.id), item)
            for result in results
            for item in result.evidence
        ],
    )
    compare_group(
        "side_effect",
        [
            (
                str(item.get("operation_key") or item.get("logical_operation") or _fingerprint(item)),
                item,
            )
            for result in results
            for item in result.side_effect_intents
        ],
    )
    resolution = dict(owner_resolution or {})
    if conflicts and resolution and str(resolution.get("owner_bot_id")) != owner_bot_id:
        raise ValueError("only the requesting MasterAgent may resolve conflicts")
    return {
        "batch": batch,
        "conflicts": conflicts,
        "conflict_detected": bool(conflicts),
        "resolved": not conflicts or bool(resolution),
        "resolved_by_bot_id": owner_bot_id if conflicts and resolution else None,
        "owner_resolution": resolution,
        "acceptance_authority": 0,
        "master_agent_authority": 1,
    }
