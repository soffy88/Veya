"""Pre-existing dirty state and execution-attribution snapshots for GoalRun.

Execution evidence has two independent views over the same execution:

* ``git_delta``        — the path-level change of the Git worktree.
* ``filesystem_delta`` — the state of the *declared action targets* on disk.

Both are required because Git is blind to ignored paths: a project file under a
gitignored directory (``.veya/``) is a real execution effect that ``git status``
can never report. Git ignore status must not affect execution truth.

The filesystem view is deliberately **bounded**: only the target paths declared
by the run's own actions are observed, never a recursive walk of the repository.
The Git baseline/evidence subsystem remains the single authority for execution
attribution; this module only extends what that authority can see.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

# Argument names that name a filesystem target in a canonical action.
_PATH_ARGUMENT_KEYS = ("path", "filepath", "file_path", "workspace_path", "target")


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


def _head_blobs(root: Path) -> dict[str, str]:
    rows = _git(str(root), ["ls-tree", "-r", "--full-tree", "HEAD"]).splitlines()
    blobs: dict[str, str] = {}
    for row in rows:
        meta, separator, path = row.partition("\t")
        fields = meta.split()
        if separator and len(fields) >= 3 and fields[1] == "blob":
            blobs[path] = fields[2]
    return blobs


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
        "head_blobs": _head_blobs(root),
        "captured_at": time.time(),
    }
    payload["fingerprint"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return payload


# ── bounded filesystem evidence ───────────────────────────────────────────
# Git cannot see ignored paths, so the declared action targets are also observed
# directly. The observation is bounded to those targets and never walks the tree.


def _resolve_target(project_root: str, raw: str) -> str | None:
    """Project-relative form of a declared target, or None when out of scope.

    A target outside the run's workspace is not this run's execution evidence,
    so it is refused rather than observed.
    """
    root = Path(project_root).expanduser().resolve()
    candidate = Path(str(raw)).expanduser()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved == root or root in resolved.parents:
        return str(resolved.relative_to(root))
    return None


def declared_target_paths(project_root: str, actions: list[dict[str, Any]] | None) -> list[str]:
    """Bounded set of filesystem targets declared by this run's actions.

    Derived from the run's own action arguments, so the observed surface is
    exactly what the run declared it would touch. Pathless or unknown actions
    add nothing rather than widening the observation.
    """
    targets: list[str] = []
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        arguments = action.get("arguments")
        if not isinstance(arguments, dict):
            continue
        for key in _PATH_ARGUMENT_KEYS:
            raw = arguments.get(key)
            if not isinstance(raw, str) or not raw.strip():
                continue
            resolved = _resolve_target(project_root, raw)
            if resolved is not None and resolved not in targets:
                targets.append(resolved)
    return sorted(targets)


def capture_filesystem_state(project_root: str, targets: list[str] | None) -> dict[str, Any]:
    """Observe only the declared targets. Never walks the repository."""
    root = Path(project_root).expanduser().resolve()
    observed: dict[str, Any] = {}
    for relative in targets or []:
        path = root / relative
        try:
            stat = path.stat()
            size: int | None = stat.st_size
            mtime_ns: int | None = stat.st_mtime_ns
        except OSError:
            size, mtime_ns = None, None
        if path.is_dir():
            kind = "dir"
        elif path.exists():
            kind = "file"
        else:
            kind = "missing"
        observed[relative] = {
            "exists": kind != "missing",
            "type": kind,
            "content_sha256": _sha256(path) if kind == "file" else None,
            "size": size,
            "mtime_ns": mtime_ns,
        }
    payload: dict[str, Any] = {
        "root": str(root),
        "targets": observed,
        "captured_at": time.time(),
    }
    payload["fingerprint"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return payload


def filesystem_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """Derive created/modified/deleted/unchanged over the declared targets.

    Git ignore status is irrelevant here: this view is computed from the
    filesystem, so an ignored path that appears, changes, or disappears is
    attributed to the execution exactly like a tracked one.
    """
    before_targets = dict((before or {}).get("targets") or {})
    after_targets = dict((after or {}).get("targets") or {})
    created: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    unchanged: list[str] = []
    observed: dict[str, Any] = {}
    for relative in sorted(set(before_targets) | set(after_targets)):
        start = before_targets.get(relative)
        finish = after_targets.get(relative)
        observed[relative] = {"before": start, "after": finish}
        if start is None:
            # Not part of the declared baseline: never claim it as an effect.
            continue
        existed = bool(start.get("exists"))
        exists = bool(finish and finish.get("exists"))
        if not existed and exists:
            created.append(relative)
        elif existed and not exists:
            deleted.append(relative)
        elif not existed and not exists:
            unchanged.append(relative)
        else:
            finish = finish or {}
            same = (start.get("type") == finish.get("type")) and (
                start.get("content_sha256") == finish.get("content_sha256")
            )
            (unchanged if same else modified).append(relative)
    return {
        "created": created,
        "modified": modified,
        "deleted": deleted,
        "unchanged": unchanged,
        "observed": observed,
        "target_count": len(observed),
        "before_fingerprint": (before or {}).get("fingerprint"),
        "after_fingerprint": (after or {}).get("fingerprint"),
    }


def cleanup_filesystem_delta(
    execution_end: dict[str, Any] | None,
    after_cleanup: dict[str, Any] | None,
    *,
    execution_delta_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Observe the cleanup phase without erasing the execution it follows.

    ``execution_delta`` stays exactly as execution left it. This records only
    what cleanup removed, plus the creation list copied forward from the
    execution delta, so the two facts coexist: the execution created the
    artifact and cleanup deleted it. A final absence must not overwrite the
    proof that the work really happened.
    """
    delta = filesystem_delta(execution_end, after_cleanup)
    created_by_execution = list((execution_delta_snapshot or {}).get("created") or [])
    return {
        "phase": "cleanup",
        "deleted": delta["deleted"],
        "modified": delta["modified"],
        "created": delta["created"],
        "unchanged": delta["unchanged"],
        "execution_created": sorted(set(created_by_execution)),
        "observed": delta["observed"],
        "target_count": delta["target_count"],
    }


def execution_delta(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    before_filesystem: dict[str, Any] | None = None,
    after_filesystem: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before_files = dict(before.get("files") or {})
    after_files = dict(after.get("files") or {})
    before_paths = set(before_files)
    after_paths = set(after_files)
    created = sorted(after_paths - before_paths)
    deleted = []
    committed_preexisting = []
    after_head_blobs = dict(after.get("head_blobs") or {})
    for path in sorted(before_paths - after_paths):
        before_record = before_files[path]
        # A worker may commit a pre-existing dirty file as part of startup or
        # checkpointing. Its worktree status then disappears, but the bytes
        # still exist in the new HEAD; that is not an execution deletion.
        if (
            before_record.get("tracked")
            and before_record.get("worktree_blob")
            and after_head_blobs.get(path) == before_record.get("worktree_blob")
        ):
            committed_preexisting.append(path)
        else:
            deleted.append(path)
    modified = sorted(
        path for path in before_paths & after_paths if before_files[path] != after_files[path]
    )

    fs_delta = (
        filesystem_delta(before_filesystem, after_filesystem)
        if before_filesystem is not None or after_filesystem is not None
        else None
    )
    # A path observable by both views is ONE effect, not two. The union keeps
    # the attribution count honest while both views stay separately reportable.
    attributed = set(created) | set(modified) | set(deleted)
    if fs_delta is not None:
        attributed |= (
            set(fs_delta["created"]) | set(fs_delta["modified"]) | set(fs_delta["deleted"])
        )

    delta: dict[str, Any] = {
        "preexisting_dirty": sorted(before_paths),
        "execution_created": created,
        "execution_modified": modified,
        "execution_deleted": deleted,
        "preexisting_committed": committed_preexisting,
        "before_fingerprint": before.get("fingerprint"),
        "after_fingerprint": after.get("fingerprint"),
        "before_head": before.get("head"),
        "after_head": after.get("head"),
        "attributed_paths": sorted(attributed),
        "attributed_path_count": len(attributed),
        "git_delta": {
            "created": created,
            "modified": modified,
            "deleted": deleted,
            "preexisting_dirty": sorted(before_paths),
            "preexisting_committed": committed_preexisting,
            "before_head": before.get("head"),
            "after_head": after.get("head"),
        },
    }
    if fs_delta is not None:
        delta["filesystem_delta"] = fs_delta
    return delta


__all__ = [
    "capture_filesystem_state",
    "capture_git_state",
    "cleanup_filesystem_delta",
    "declared_target_paths",
    "execution_delta",
    "filesystem_delta",
]
