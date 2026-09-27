"""Pre-existing dirty state and execution-attribution snapshots for GoalRun."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _git(root: str, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _path_record(root: Path, relative: str, status: str) -> dict[str, Any]:
    path = root / relative
    index_blob = _git(str(root), ["ls-files", "--stage", "--", relative]).strip()
    worktree_blob = (
        _git(str(root), ["hash-object", "--", relative]).strip() if path.exists() else ""
    )
    return {
        "path": relative,
        "status": status,
        "tracked": bool(_git(str(root), ["ls-files", "--error-unmatch", "--", relative]).strip()),
        "content_sha256": _sha256(path),
        "index_blob": index_blob.split()[1] if len(index_blob.split()) > 1 else None,
        "worktree_blob": worktree_blob or None,
    }


def capture_git_state(project_root: str) -> dict[str, Any]:
    """Capture a deterministic, path-level snapshot without mutating Git."""

    root = Path(project_root).expanduser().resolve()
    status = _git(str(root), ["status", "--porcelain=v1", "--untracked-files=all"])
    records: dict[str, dict[str, Any]] = {}
    for line in status.splitlines():
        if len(line) < 4:
            continue
        code, relative = line[:2], line[3:]
        if " -> " in relative:
            relative = relative.split(" -> ", 1)[1]
        records[relative] = _path_record(root, relative, code)
    payload: dict[str, Any] = {
        "head": _git(str(root), ["rev-parse", "HEAD"]).strip(),
        "files": records,
        "captured_at": time.time(),
    }
    payload["fingerprint"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return payload


def execution_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_files = dict(before.get("files") or {})
    after_files = dict(after.get("files") or {})
    before_paths = set(before_files)
    after_paths = set(after_files)
    created = sorted(after_paths - before_paths)
    deleted = sorted(before_paths - after_paths)
    modified = sorted(
        path for path in before_paths & after_paths if before_files[path] != after_files[path]
    )
    return {
        "preexisting_dirty": sorted(before_paths),
        "execution_created": created,
        "execution_modified": modified,
        "execution_deleted": deleted,
        "before_fingerprint": before.get("fingerprint"),
        "after_fingerprint": after.get("fingerprint"),
        "before_head": before.get("head"),
        "after_head": after.get("head"),
        "attributed_path_count": len(created) + len(modified) + len(deleted),
    }


__all__ = ["capture_git_state", "execution_delta"]
