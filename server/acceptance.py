"""Deterministic Acceptance Contract primitives (P2-07)."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

CriterionType = Literal["command", "test", "file_exists", "schema", "manual", "llm_review"]
CriterionStatus = Literal["pending", "passed", "failed", "blocked"]


@dataclass
class AcceptanceCriterion:
    id: str
    description: str
    type: CriterionType = "manual"
    required: bool = True
    evidence: list[str] = field(default_factory=list)
    status: CriterionStatus = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_criteria(items: list[AcceptanceCriterion | dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize API/task input without evaluating or inventing criteria."""
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items or [], start=1):
        if isinstance(item, AcceptanceCriterion):
            criterion = item
        else:
            criterion = AcceptanceCriterion(
                id=str(item.get("id") or f"criterion_{index}"),
                description=str(item.get("description") or ""),
                type=item.get("type", "manual"),
                required=bool(item.get("required", True)),
                evidence=list(item.get("evidence") or []),
                status=item.get("status", "pending"),
            )
        result.append(criterion.to_dict())
    return result


def evaluate_criterion(
    criterion: AcceptanceCriterion | dict[str, Any],
    *,
    workspace: str | Path = ".",
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Evaluate only deterministic evidence; manual/LLM criteria stay blocked."""
    item = criterion.to_dict() if isinstance(criterion, AcceptanceCriterion) else dict(criterion)
    kind = item.get("type", "manual")
    root = Path(workspace).expanduser().resolve()
    evidence = list(item.get("evidence") or [])
    if kind == "file_exists":
        path = Path(str(item.get("path") or item.get("description") or ""))
        target = path if path.is_absolute() else root / path
        try:
            target = target.resolve()
            root_relative = target == root or root in target.parents
        except OSError:
            root_relative = False
        if not root_relative:
            item.update(
                status="failed",
                evidence=[*evidence, "path is outside the acceptance workspace"],
            )
            return item
        passed = target.exists()
        if passed:
            evidence.append(str(target))
        item.update(status="passed" if passed else "failed", evidence=evidence)
        return item
    if kind in {"command", "test"}:
        command = str(item.get("command") or item.get("description") or "")
        try:
            completed = subprocess.run(
                shlex.split(command),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            item.update(status="failed", evidence=[*evidence, str(exc)])
            return item
        summary = (completed.stdout or completed.stderr or "").strip()[-1000:]
        if summary:
            evidence.append(summary)
        item.update(status="passed" if completed.returncode == 0 else "failed", evidence=evidence)
        return item
    if kind == "schema":
        target = item.get("value")
        required_keys = item.get("required_keys") or []
        passed = isinstance(target, dict) and all(key in target for key in required_keys)
        item.update(status="passed" if passed else "failed", evidence=evidence)
        return item
    item.update(status="blocked", evidence=evidence)
    return item


def evaluate_acceptance(
    criteria: list[AcceptanceCriterion | dict[str, Any]] | None,
    *,
    workspace: str | Path = ".",
    timeout_s: float = 30.0,
) -> list[dict[str, Any]]:
    """Evaluate every criterion without allowing LLM review to become a pass."""
    return [
        evaluate_criterion(item, workspace=workspace, timeout_s=timeout_s)
        for item in (criteria or [])
    ]
