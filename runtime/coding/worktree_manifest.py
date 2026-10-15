"""Worktree shared paths manifest (.veya/worktree.toml) parsing and application."""

from __future__ import annotations

import logging
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger("veya.worktree_manifest")

MANIFEST_REL_PATH = ".veya/worktree.toml"


@dataclass(frozen=True)
class WorktreeManifest:
    """Parsed worktree manifest with copy and share entries."""

    copy: tuple[str, ...] = field(default_factory=tuple)
    share: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def empty(cls) -> WorktreeManifest:
        return cls(copy=(), share=())

    def to_dict(self) -> dict[str, list[str]]:
        return {"copy": list(self.copy), "share": list(self.share)}


def _validate_relative_path(path: str) -> str:
    """Validate a path is relative, non-empty, no '..', no absolute, no NUL."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    value = path.strip()
    if "\x00" in value:
        raise ValueError("path contains NUL byte")
    pure = PurePosixPath(value)
    if pure.is_absolute():
        raise ValueError(f"path must be relative, not absolute: {value!r}")
    if value.startswith("-") or ".." in pure.parts:
        raise ValueError(f"path must not traverse upwards: {value!r}")
    return value


def _validate_manifest_dict(data: dict[str, Any]) -> WorktreeManifest:
    """Validate and construct manifest from parsed TOML dict."""
    copy_list = data.get("copy", [])
    share_list = data.get("share", [])

    if not isinstance(copy_list, list):
        raise ValueError("manifest 'copy' must be a list")
    if not isinstance(share_list, list):
        raise ValueError("manifest 'share' must be a list")

    validated_copy = tuple(_validate_relative_path(p) for p in copy_list)
    validated_share = tuple(_validate_relative_path(p) for p in share_list)

    return WorktreeManifest(copy=validated_copy, share=validated_share)


def load_manifest(repo_root: Path) -> WorktreeManifest:
    """Load and parse the worktree manifest from repo root."""
    manifest_path = (repo_root / MANIFEST_REL_PATH).resolve()
    try:
        if not manifest_path.is_file():
            return WorktreeManifest.empty()
        raw = manifest_path.read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
        return _validate_manifest_dict(data)
    except tomllib.TOMLDecodeError as exc:
        logger.warning("Failed to parse worktree manifest %s: %s", manifest_path, exc)
        return WorktreeManifest.empty()
    except OSError:
        return WorktreeManifest.empty()
    except ValueError as exc:
        logger.warning("Invalid worktree manifest %s: %s", manifest_path, exc)
        return WorktreeManifest.empty()


def write_manifest(repo_root: Path, manifest: WorktreeManifest) -> None:
    """Write manifest to .veya/worktree.toml atomically."""
    manifest_path = (repo_root / MANIFEST_REL_PATH).resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    if manifest.copy:
        copy_lines = ", ".join(f'"{p}"' for p in manifest.copy)
        lines.append(f"copy = [{copy_lines}]")
    else:
        lines.append("copy = []")
    if manifest.share:
        share_lines = ", ".join(f'"{p}"' for p in manifest.share)
        lines.append(f"share = [{share_lines}]")
    else:
        lines.append("share = []")

    content = "\n".join(lines) + "\n"
    tmp = manifest_path.with_name(f".{manifest_path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(manifest_path)


def apply_manifest(
    repo_root: Path,
    worktree_path: Path,
    manifest: WorktreeManifest,
) -> dict[str, Any]:
    """Apply manifest to a newly created worktree.

    - copy: copy files from repo_root to worktree_path (skip missing)
    - share: symlink directories from repo_root to worktree_path

    Idempotent: overwrites existing symlinks for share, skips missing copy sources.
    Logs operations without file contents.
    """
    result: dict[str, list[str]] = {
        "copied": [],
        "shared": [],
        "skipped_copy": [],
        "skipped_share": [],
        "errors": [],
    }

    # Apply copy entries
    for rel_path in manifest.copy:
        src = repo_root / rel_path
        dst = worktree_path / rel_path
        try:
            if not src.exists():
                logger.info("worktree copy skip missing: %s", rel_path)
                result["skipped_copy"].append(rel_path)
                continue
            if src.is_dir():
                logger.warning("worktree copy skip directory (files only): %s", rel_path)
                result["skipped_copy"].append(rel_path)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            logger.info("worktree copy applied: %s", rel_path)
            result["copied"].append(rel_path)
        except Exception as exc:
            logger.warning("worktree copy failed for %s: %s", rel_path, exc)
            result["errors"].append(f"copy {rel_path}: {exc}")

    # Apply share entries
    for rel_path in manifest.share:
        src = repo_root / rel_path
        dst = worktree_path / rel_path
        try:
            if not src.exists():
                logger.info("worktree share skip missing: %s", rel_path)
                result["skipped_share"].append(rel_path)
                continue
            if not src.is_dir():
                logger.warning("worktree share skip non-directory: %s", rel_path)
                result["skipped_share"].append(rel_path)
                continue
            # Remove existing symlink or directory at destination
            if dst.is_symlink() or dst.exists():
                if dst.is_dir() and not dst.is_symlink():
                    # Don't remove real directories, just skip
                    logger.warning("worktree share skip existing real directory: %s", rel_path)
                    result["skipped_share"].append(rel_path)
                    continue
                dst.unlink(missing_ok=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src, target_is_directory=True)
            logger.info("worktree share applied: %s -> %s", rel_path, src)
            result["shared"].append(rel_path)
        except Exception as exc:
            logger.warning("worktree share failed for %s: %s", rel_path, exc)
            result["errors"].append(f"share {rel_path}: {exc}")

    return result


__all__ = [
    "MANIFEST_REL_PATH",
    "WorktreeManifest",
    "_validate_relative_path",
    "apply_manifest",
    "load_manifest",
    "write_manifest",
]
