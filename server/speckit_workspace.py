"""Spec Kit (SDD) Phase 0: ``.speckit/`` workspace bootstrap (default off).

The ``.speckit/`` directory is the review contract between the human and the
model (docs/speckit-integration.md section 6): every Spec Kit artifact lives
there in plain Markdown, under Git version control.

This module only creates the directory and seeds the constitution.  The
production consumer already exists — ``server.goal_run.planner._g1_from_speckit``
reads ``.speckit/{tasks,constitution}.md`` and
``server.spec_pack.export_speckit`` writes them — so this module deliberately
does not re-implement reading, compiling, or exporting.

``init_workspace`` is idempotent and never overwrites an existing file.
Everything short-circuits on ``VEYA_SPECKIT``; with the flag off no directory is
created and no file is written.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from server.speckit_phases import (
    ARTIFACT_CHECKLIST,
    ARTIFACT_CONSTITUTION,
    ARTIFACT_PLAN,
    ARTIFACT_SPEC,
    ARTIFACT_TASKS,
    FLAG_NAME,
    SPECKIT_DIR,
    TEMPLATE_ARTIFACTS_DIR,
    enabled,
)

#: Artifacts a full SDD run leaves behind, in pipeline order.
ARTIFACTS: tuple[str, ...] = (
    ARTIFACT_CONSTITUTION,
    ARTIFACT_SPEC,
    ARTIFACT_PLAN,
    ARTIFACT_TASKS,
    ARTIFACT_CHECKLIST,
)

#: The two artifacts the existing goal_run consumer requires as its SSOT pair.
SSOT_ARTIFACTS: tuple[str, ...] = (ARTIFACT_CONSTITUTION, ARTIFACT_TASKS)


def repo_root() -> Path:
    """Repository root that carries ``templates/speckit/``."""
    return Path(__file__).resolve().parent.parent


def template_path(name: str) -> Path:
    """Absolute path of a vendored Spec Kit template."""
    return repo_root() / TEMPLATE_ARTIFACTS_DIR / name


def speckit_dir(project_root: str | Path) -> Path:
    """``.speckit/`` under a project root."""
    return Path(project_root).expanduser() / SPECKIT_DIR


def init_workspace(project_root: str | Path) -> dict[str, Any]:
    """Create ``.speckit/`` and seed the constitution; idempotent.

    Returns ``{"ok": bool, ...}``.  Existing files are never overwritten, so a
    second call on a populated workspace only reports what is already there.
    """
    if not enabled():
        return {"ok": False, "error": f"flag disabled ({FLAG_NAME})"}
    root = Path(project_root).expanduser()
    directory = root / SPECKIT_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"cannot create {directory}: {exc}"}

    created: list[str] = []
    existing: list[str] = []
    constitution = directory / ARTIFACT_CONSTITUTION
    if (
        constitution.is_file()
        and constitution.read_text(encoding="utf-8", errors="replace").strip()
    ):
        existing.append(ARTIFACT_CONSTITUTION)
    else:
        source = template_path("constitution-template.md")
        body = ""
        if source.is_file():
            body = source.read_text(encoding="utf-8", errors="replace")
        constitution.write_text(body or "# Project Constitution\n", encoding="utf-8")
        created.append(ARTIFACT_CONSTITUTION)
    return {
        "ok": True,
        "path": str(directory),
        "created": created,
        "existing": existing,
        "artifacts": list(ARTIFACTS),
        "ssot_ready": all((directory / name).is_file() for name in SSOT_ARTIFACTS),
    }


def status(project_root: str | Path) -> dict[str, Any]:
    """Read-only projection of a ``.speckit/`` directory.

    Readable with the flag off: this function touches nothing, so an operator
    can inspect a workspace without enabling the integration.
    """
    directory = speckit_dir(project_root)
    artifacts = {
        name: (directory / name).is_file()
        and bool((directory / name).read_text(encoding="utf-8", errors="replace").strip())
        for name in ARTIFACTS
    }
    return {
        "name": "speckit_workspace",
        "available": enabled(),
        "reason": "ready" if enabled() else f"flag disabled ({FLAG_NAME})",
        "path": str(directory),
        "exists": directory.is_dir(),
        "artifacts": artifacts,
        "ssot_ready": all(artifacts.get(name) for name in SSOT_ARTIFACTS),
        "next_artifact": next((name for name in ARTIFACTS if not artifacts[name]), ""),
    }


def copy_template(name: str, destination: str | Path) -> dict[str, Any]:
    """Copy one vendored template into a workspace (never overwrites)."""
    if not enabled():
        return {"ok": False, "error": f"flag disabled ({FLAG_NAME})"}
    source = template_path(name)
    if not source.is_file():
        return {"ok": False, "error": f"template not found: {name}"}
    target = Path(destination).expanduser()
    if target.is_file():
        return {"ok": True, "copied": False, "path": str(target)}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except OSError as exc:
        return {"ok": False, "error": f"cannot copy to {target}: {exc}"}
    return {"ok": True, "copied": True, "path": str(target)}


__all__ = [
    "ARTIFACTS",
    "FLAG_NAME",
    "SSOT_ARTIFACTS",
    "copy_template",
    "init_workspace",
    "repo_root",
    "speckit_dir",
    "status",
    "template_path",
]
