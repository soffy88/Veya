"""Canonical Git Promotion Substrate for Veya Local2 (P0-D through P0-K).

Enables safe, auditable promotion of verified modifications from worker worktrees
(isolated or existing) into the canonical working tree.

Core guarantees:
- 3-way merge conflict detection (SAFE, MERGE_REQUIRED, CONFLICT).
- Strict dirty preservation: unrelated canonical dirty files are NEVER touched or stashed.
- Scoped rollback: if post-promotion verification fails, restores ONLY promotion-owned files.
- Promotion != Commit: changes remain in working tree/index; NO auto-commit, NO auto-push.
- Zero git reset --hard, zero git clean, zero git stash.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from veya.remote.models import RemoteErrorCode
from veya.remote.workspace_binding import git_repo_identity


class PromotionError(Exception):
    def __init__(self, code: RemoteErrorCode | str, message: str) -> None:
        super().__init__(message)
        self.code = code if isinstance(code, RemoteErrorCode) else RemoteErrorCode.EXECUTION_FAILED
        self.message = message


@dataclass
class PromotionPreflight:
    source_worktree: str
    source_base_sha: str
    canonical_root: str
    canonical_head: str
    canonical_status: list[str]
    source_diff: str
    target_files: list[str]
    unrelated_dirty_files: list[str]
    conflicting_dirty_files: list[str]
    classification: str  # SAFE, MERGE_REQUIRED, CONFLICT
    safe_to_promote: bool
    rejection_reason: str | None = None
    inspected_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PromotionResult:
    status: str  # PROMOTED, BLOCKED, VERIFY_FAILED_ROLLED_BACK, VERIFY_FAILED
    preflight: PromotionPreflight
    promoted_files: list[str]
    unrelated_dirty_preserved: bool
    verified: bool
    verify_output: str | None = None
    rollback_performed: bool = False
    message: str = ""
    completed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_git(cwd: Path | str, args: list[str], *, check: bool = True) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise PromotionError(
            RemoteErrorCode.EXECUTION_FAILED,
            f"git -C {cwd} {' '.join(args)} failed (exit {proc.returncode}): {err}",
        )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_porcelain_files(status_output: str) -> list[str]:
    files: list[str] = []
    for line in status_output.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        # Porcelain v1 has 2 characters for status, a space, then file path
        # e.g. " M file.py", "?? new.py", "R  old -> new"
        raw_path = line[3:].strip()
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ")[1].strip()
        if raw_path:
            files.append(raw_path)
    return sorted(set(files))


def preflight_promotion(
    source_worktree: str | Path,
    canonical_root: str | Path,
    *,
    files: list[str] | None = None,
    expected_base_sha: str | None = None,
) -> PromotionPreflight:
    """Preflight check before promoting changes into canonical.

    Performs file-level conflict analysis against canonical dirty state without
    altering any files.
    """
    src = Path(source_worktree).resolve()
    can = Path(canonical_root).resolve()

    if not src.is_dir():
        raise PromotionError(RemoteErrorCode.NOT_FOUND, f"source worktree does not exist: {src}")
    if not can.is_dir():
        raise PromotionError(RemoteErrorCode.NOT_FOUND, f"canonical root does not exist: {can}")

    # 1. Verify repository identity
    try:
        src_id = git_repo_identity(str(src))
        can_id = git_repo_identity(str(can))
    except Exception as exc:
        raise PromotionError(
            RemoteErrorCode.WORKSPACE_DENIED, f"failed to verify repo identity: {exc}"
        ) from exc

    if src_id != can_id:
        raise PromotionError(
            RemoteErrorCode.WORKSPACE_DENIED,
            f"repository identity mismatch: source={src_id}, canonical={can_id}",
        )

    # 2. Canonical HEAD and status
    _, can_head, _ = _run_git(can, ["rev-parse", "HEAD"])
    can_head = can_head.strip()

    _, can_status_raw, _ = _run_git(can, ["status", "--porcelain=v1"])
    can_dirty = _parse_porcelain_files(can_status_raw)

    # 3. Source HEAD and merge base
    _, src_head, _ = _run_git(src, ["rev-parse", "HEAD"])
    src_head = src_head.strip()

    code, mb, _ = _run_git(can, ["merge-base", can_head, src_head], check=False)
    source_base_sha = mb.strip() if code == 0 and mb.strip() else can_head

    if expected_base_sha and expected_base_sha.strip() != source_base_sha:
        return PromotionPreflight(
            source_worktree=str(src),
            source_base_sha=source_base_sha,
            canonical_root=str(can),
            canonical_head=can_head,
            canonical_status=can_dirty,
            source_diff="",
            target_files=[],
            unrelated_dirty_files=can_dirty,
            conflicting_dirty_files=[],
            classification="CONFLICT",
            safe_to_promote=False,
            rejection_reason=f"expected_base_sha mismatch: expected {expected_base_sha}, got {source_base_sha}",
        )

    # 4. Discover source modified / untracked files
    _, src_status_raw, _ = _run_git(src, ["status", "--porcelain=v1"])
    src_dirty = _parse_porcelain_files(src_status_raw)

    _, src_diff, _ = _run_git(src, ["diff", source_base_sha], check=False)

    if files is not None:
        target_files = sorted([f for f in files if f in src_dirty or (src / f).exists()])
    else:
        target_files = src_dirty

    if not target_files:
        return PromotionPreflight(
            source_worktree=str(src),
            source_base_sha=source_base_sha,
            canonical_root=str(can),
            canonical_head=can_head,
            canonical_status=can_dirty,
            source_diff=src_diff,
            target_files=[],
            unrelated_dirty_files=can_dirty,
            conflicting_dirty_files=[],
            classification="SAFE",
            safe_to_promote=True,
            rejection_reason="no changes to promote",
        )

    # 5. File-level conflict classification
    unrelated_dirty = [f for f in can_dirty if f not in target_files]
    conflicting_dirty = [f for f in can_dirty if f in target_files]

    if not conflicting_dirty:
        classification = "SAFE"
        safe_to_promote = True
        rejection_reason = None
    else:
        # Check each conflicting file for 3-way mergeability
        merge_failed_files: list[str] = []
        for f in conflicting_dirty:
            src_f = src / f
            can_f = can / f
            if not src_f.exists() or not can_f.exists():
                merge_failed_files.append(f)
                continue
            if src_f.read_bytes() == can_f.read_bytes():
                continue

            # Attempt clean 3-way merge test
            with tempfile.TemporaryDirectory() as td:
                base_tmp = Path(td) / "base"
                can_tmp = Path(td) / "can"
                src_tmp = Path(td) / "src"

                # Extract base content from git
                code, base_content, _ = _run_git(
                    can, ["show", f"{source_base_sha}:{f}"], check=False
                )
                if code != 0:
                    merge_failed_files.append(f)
                    continue

                base_tmp.write_text(base_content, encoding="utf-8")
                can_tmp.write_bytes(can_f.read_bytes())
                src_tmp.write_bytes(src_f.read_bytes())

                # git merge-file -p <can> <base> <src>
                proc = subprocess.run(
                    ["git", "merge-file", "-p", str(can_tmp), str(base_tmp), str(src_tmp)],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode != 0:
                    merge_failed_files.append(f)

        if not merge_failed_files:
            classification = "MERGE_REQUIRED"
            safe_to_promote = True
            rejection_reason = None
        else:
            classification = "CONFLICT"
            safe_to_promote = False
            rejection_reason = f"conflicting files cannot be cleanly merged: {merge_failed_files}"

    return PromotionPreflight(
        source_worktree=str(src),
        source_base_sha=source_base_sha,
        canonical_root=str(can),
        canonical_head=can_head,
        canonical_status=can_dirty,
        source_diff=src_diff,
        target_files=target_files,
        unrelated_dirty_files=unrelated_dirty,
        conflicting_dirty_files=conflicting_dirty,
        classification=classification,
        safe_to_promote=safe_to_promote,
        rejection_reason=rejection_reason,
    )


def apply_promotion(
    preflight: PromotionPreflight,
    *,
    verify_after: bool = True,
    verify_command: str | None = None,
    rollback_on_failure: bool = True,
    runtime_profile: Any | None = None,
) -> PromotionResult:
    """Apply verified changes from preflight to the canonical working tree.

    Features:
    - Never uses reset --hard, clean, or stash.
    - Saves a pre-promotion baseline for target files only.
    - Preserves all pre-existing unrelated dirty changes.
    - Runs post-verification on canonical.
    - Scoped rollback of owned changes on verification failure.
    """
    if not preflight.safe_to_promote:
        return PromotionResult(
            status="BLOCKED",
            preflight=preflight,
            promoted_files=[],
            unrelated_dirty_preserved=True,
            verified=False,
            message=preflight.rejection_reason or "promotion blocked by preflight",
        )

    if not preflight.target_files:
        return PromotionResult(
            status="PROMOTED",
            preflight=preflight,
            promoted_files=[],
            unrelated_dirty_preserved=True,
            verified=True,
            message="no files to promote",
        )

    can = Path(preflight.canonical_root).resolve()
    src = Path(preflight.source_worktree).resolve()

    # 1. Capture baseline for target files only (scoped rollback safety)
    baseline: dict[str, dict[str, Any]] = {}
    for f in preflight.target_files:
        target_path = can / f
        if target_path.exists():
            baseline[f] = {
                "exists": True,
                "content": target_path.read_bytes(),
                "mode": target_path.stat().st_mode,
            }
        else:
            baseline[f] = {"exists": False}

    # 2. Apply modifications to target files
    promoted: list[str] = []
    try:
        for f in preflight.target_files:
            src_file = src / f
            can_file = can / f

            if (
                f in preflight.conflicting_dirty_files
                and preflight.classification == "MERGE_REQUIRED"
            ):
                # Perform 3-way file merge
                with tempfile.TemporaryDirectory() as td:
                    base_tmp = Path(td) / "base"
                    _, base_content, _ = _run_git(can, ["show", f"{preflight.source_base_sha}:{f}"])
                    base_tmp.write_text(base_content, encoding="utf-8")
                    # git merge-file <can_file> <base_tmp> <src_file>
                    proc = subprocess.run(
                        ["git", "merge-file", str(can_file), str(base_tmp), str(src_file)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if proc.returncode != 0:
                        raise PromotionError(
                            RemoteErrorCode.EXECUTION_FAILED,
                            f"3-way merge failed on {f}: {proc.stderr}",
                        )
                promoted.append(f)
            elif src_file.exists():
                can_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, can_file)
                promoted.append(f)
            elif can_file.exists():
                can_file.unlink()
                promoted.append(f)
    except Exception as exc:
        # Rollback immediately on write error
        rollback_promotion(can, baseline)
        return PromotionResult(
            status="BLOCKED",
            preflight=preflight,
            promoted_files=[],
            unrelated_dirty_preserved=True,
            verified=False,
            rollback_performed=True,
            message=f"failed during application, rolled back: {exc}",
        )

    # 3. Post-promotion verification
    verify_output: str | None = None
    if verify_after:
        verify_cmd = verify_command
        if not verify_cmd:
            if runtime_profile and runtime_profile.pytest:
                py = runtime_profile.python_bin or "python"
                verify_cmd = f"{py} -m pytest -q"
            else:
                verify_cmd = "git diff --stat"

        env = dict(os.environ)
        if runtime_profile and getattr(runtime_profile, "path_entries", None):
            env["PATH"] = (
                os.pathsep.join(runtime_profile.path_entries) + os.pathsep + env.get("PATH", "")
            )
        if runtime_profile and getattr(runtime_profile, "venv_root", None):
            env["VIRTUAL_ENV"] = str(runtime_profile.venv_root)

        proc = subprocess.run(
            verify_cmd,
            shell=True,
            cwd=str(can),
            capture_output=True,
            text=True,
            env=env,
        )
        verify_output = (proc.stdout + proc.stderr).strip()[-4000:]
        if proc.returncode != 0:
            if rollback_on_failure:
                rollback_promotion(can, baseline)
                return PromotionResult(
                    status="VERIFY_FAILED_ROLLED_BACK",
                    preflight=preflight,
                    promoted_files=[],
                    unrelated_dirty_preserved=True,
                    verified=False,
                    verify_output=verify_output,
                    rollback_performed=True,
                    message=f"canonical post-verification failed (exit {proc.returncode}); scoped rollback applied",
                )
            return PromotionResult(
                status="VERIFY_FAILED",
                preflight=preflight,
                promoted_files=promoted,
                unrelated_dirty_preserved=True,
                verified=False,
                verify_output=verify_output,
                rollback_performed=False,
                message=f"canonical post-verification failed (exit {proc.returncode})",
            )

    return PromotionResult(
        status="PROMOTED",
        preflight=preflight,
        promoted_files=promoted,
        unrelated_dirty_preserved=True,
        verified=True,
        verify_output=verify_output,
        rollback_performed=False,
        message=f"successfully promoted {len(promoted)} files to canonical working tree",
    )


def rollback_promotion(canonical_root: str | Path, baseline: dict[str, dict[str, Any]]) -> None:
    """Scoped rollback restoring ONLY baseline-recorded files.

    Preserves all unrelated canonical dirty changes untouched.
    """
    can = Path(canonical_root).resolve()
    for rel_path, meta in baseline.items():
        target = can / rel_path
        if meta["exists"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(meta["content"])
            target.chmod(meta["mode"])
        else:
            if target.exists():
                target.unlink()


__all__ = [
    "PromotionError",
    "PromotionPreflight",
    "PromotionResult",
    "apply_promotion",
    "preflight_promotion",
    "rollback_promotion",
]
