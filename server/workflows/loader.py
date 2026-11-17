"""Load workflow definitions from workspace `.veya/workflows/*.yaml`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .schema import WorkflowDef, WorkflowValidationError, parse_workflow, workflows_enabled


def workflows_dir(workspace: str | Path | None = None) -> Path:
    root = Path(workspace or os.environ.get("VEYA_WORKSPACE") or Path.cwd()).expanduser()
    return root / ".veya" / "workflows"


def load_workflows(workspace: str | Path | None = None) -> list[WorkflowDef]:
    """Return validated definitions; missing directory/flag off = empty list."""

    if not workflows_enabled():
        return []
    root = workflows_dir(workspace)
    if not root.is_dir():
        return []
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise WorkflowValidationError("PyYAML is required to load workflows") from exc
    definitions: list[WorkflowDef] = []
    for path in sorted(root.glob("*.yaml")):
        try:
            data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WorkflowValidationError(f"invalid workflow file {path}: {exc}") from exc
        definition = parse_workflow(data)
        if definition.id != path.stem:
            raise WorkflowValidationError(f"workflow id {definition.id!r} != file {path.stem!r}")
        definitions.append(definition)
    return definitions
