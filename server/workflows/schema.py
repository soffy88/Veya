"""YAML workflow schema and validation.

Design adapted from block/buzz (Apache-2.0, Copyright 2026 Block, Inc.):
unified trigger/step vocabulary and safe `{{...}}` templates, expressed on
Veya's existing RoutineRegistry trigger topics, ApprovalStore and tool/gate
surfaces.  No Buzz Rust code is copied.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

TRIGGER_SKINDS = frozenset({"schedule", "event", "webhook"})
STEP_KINDS = frozenset({"tool", "agent", "approval", "notify", "delay"})
MAX_STEPS = 64
MAX_TEMPLATE_CHARS = 4096
_TEMPLATE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_PATH_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class WorkflowValidationError(ValueError):
    """Raised by loader/schema on untrusted YAML."""


@dataclass(frozen=True)
class WorkflowTrigger:
    kind: str
    topic: str = ""
    cron: str = ""

    def matches(self, *, kind: str, topic: str = "") -> bool:
        return self.kind == kind and (not self.topic or self.topic == topic)


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    kind: str
    tool: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    text: str = ""
    seconds: float = 0.0
    owner_only: bool = False


@dataclass(frozen=True)
class WorkflowDef:
    id: str
    trigger: WorkflowTrigger
    steps: tuple[WorkflowStep, ...]
    version: int = 1
    enabled: bool = True


def workflows_enabled() -> bool:
    """``VEYA_WORKFLOWS`` gates the plane; default ON (no files = no behavior)."""

    return os.environ.get("VEYA_WORKFLOWS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "",
    }


def _path_value(root: Mapping[str, Any], path: str) -> Any:
    parts = path.split(".")
    if not parts or any(not _PATH_PART.match(part) for part in parts):
        raise WorkflowValidationError(f"unsafe template path: {path}")
    value: Any = root
    for part in parts:
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def render_template(template: Any, context: Mapping[str, Any]) -> Any:
    """Safe `{{trigger.*}}` / `{{steps.*}}` substitution; no eval/exec/imports.

    Unknown paths stay literal (Buzz-compatible). A value is never coerced
    into code, and dangerous Python names are rejected as path segments.
    """

    if isinstance(template, Mapping):
        return {str(key): render_template(value, context) for key, value in template.items()}
    if isinstance(template, (list, tuple)):
        return [render_template(value, context) for value in template]
    if not isinstance(template, str) or "{{" not in template:
        return template
    if len(template) > MAX_TEMPLATE_CHARS:
        raise WorkflowValidationError("template too long")

    def replace(match: re.Match[str]) -> str:
        path = match.group(1).strip()
        if not (path.startswith("trigger.") or path.startswith("steps.")):
            return match.group(0)
        value = _path_value(context, path)
        if value is None:
            return match.group(0)
        return str(value)

    return _TEMPLATE.sub(replace, template)


def parse_workflow(data: Any) -> WorkflowDef:
    """Validate a decoded YAML mapping into a frozen definition."""

    if not isinstance(data, Mapping):
        raise WorkflowValidationError("workflow must be a mapping")
    workflow_id = str(data.get("id") or "")
    if not _PATH_PART.match(workflow_id):
        raise WorkflowValidationError("workflow id must be an identifier")
    raw_trigger = data.get("on") or data.get("trigger")
    if not isinstance(raw_trigger, Mapping):
        raise WorkflowValidationError("workflow requires an 'on' trigger mapping")
    kind = str(raw_trigger.get("kind") or "")
    if kind not in TRIGGER_SKINDS:
        raise WorkflowValidationError(f"unknown trigger kind: {kind}")
    if kind == "schedule" and not str(raw_trigger.get("cron") or ""):
        raise WorkflowValidationError("schedule triggers require cron")
    if kind in {"event", "webhook"} and not str(raw_trigger.get("topic") or ""):
        raise WorkflowValidationError(f"{kind} triggers require topic")
    trigger = WorkflowTrigger(
        kind=kind,
        topic=str(raw_trigger.get("topic") or ""),
        cron=str(raw_trigger.get("cron") or ""),
    )

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        raise WorkflowValidationError("workflow requires a non-empty steps list")
    if len(raw_steps) > MAX_STEPS:
        raise WorkflowValidationError(f"workflow exceeds {MAX_STEPS} steps")
    steps: list[WorkflowStep] = []
    seen: set[str] = set()
    for raw in raw_steps:
        if not isinstance(raw, Mapping):
            raise WorkflowValidationError("each step must be a mapping")
        step_id = str(raw.get("id") or "")
        if not _PATH_PART.match(step_id) or step_id in seen:
            raise WorkflowValidationError(f"invalid or duplicate step id: {step_id}")
        seen.add(step_id)
        step_kind = str(raw.get("kind") or "")
        if step_kind not in STEP_KINDS:
            raise WorkflowValidationError(f"unknown step kind: {step_kind}")
        args = raw.get("args") or {}
        if not isinstance(args, Mapping):
            raise WorkflowValidationError("step args must be a mapping")
        if step_kind == "tool" and not str(raw.get("tool") or ""):
            raise WorkflowValidationError("tool steps require tool")
        if step_kind == "delay":
            try:
                seconds = float(raw.get("seconds") or 0)
            except (TypeError, ValueError):
                raise WorkflowValidationError("delay.seconds must be numeric") from None
            if seconds < 0:
                raise WorkflowValidationError("delay.seconds must be non-negative")
        else:
            seconds = 0.0
        steps.append(
            WorkflowStep(
                id=step_id,
                kind=step_kind,
                tool=str(raw.get("tool") or ""),
                args=dict(args),
                text=str(raw.get("text") or ""),
                seconds=seconds,
                owner_only=bool(raw.get("owner_only") or False),
            )
        )
    version = int(data.get("version") or 1)
    if version < 1:
        raise WorkflowValidationError("version must be >= 1")
    return WorkflowDef(
        id=workflow_id,
        trigger=trigger,
        steps=tuple(steps),
        version=version,
        enabled=bool(data.get("enabled", True)),
    )
