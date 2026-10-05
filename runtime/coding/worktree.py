"""Task-scoped Git worktree isolation for local coding runs."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .models import CodingWorkspace


class WorktreeError(RuntimeError):
    """A worktree operation was rejected or failed."""


@dataclass(frozen=True)
class WorktreeRecord:
    task_id: str
    branch_name: str
    path: str
    repo_root: str
    clean: bool
    changed_files: list[str]
    locked: bool = False
    lock_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "branch_name": self.branch_name,
            "path": self.path,
            "repo_root": self.repo_root,
            "clean": self.clean,
            "changed_files": list(self.changed_files),
            "locked": self.locked,
            "lock_reason": self.lock_reason,
        }


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,150}$")


def validate_task_id(task_id: str) -> str:
    if not _SAFE_TASK_ID.fullmatch(task_id or "") or task_id in {".", ".."}:
        raise WorktreeError("task_id must be a single safe path component")
    return task_id


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", value.lower()).strip("-")
    return value[:48] or "coding-task"


def branch_name_for(task_id: str, objective: str) -> str:
    validate_task_id(task_id)
    return f"veya/{_slug(objective)}-{task_id[:12]}"


def _validate_branch_name(branch_name: str) -> str:
    if (
        not _SAFE_BRANCH.fullmatch(branch_name or "")
        or branch_name.startswith(("/", "-"))
        or ".." in branch_name
        or "//" in branch_name
        or branch_name.endswith(("/", ".lock"))
    ):
        raise WorktreeError(f"invalid branch name: {branch_name!r}")
    return branch_name


def _git_command(root: Path, args: list[str], *, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git command timed out: git {' '.join(args)}") from exc
    except OSError as exc:
        raise WorktreeError(f"git is unavailable: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise WorktreeError(f"git {' '.join(args)} failed: {detail[:1000]}")
    return result.stdout


def _git_no_index_diff(root: Path, relative_path: str, *, stat: bool = False) -> str:
    args = ["git", "-C", str(root), "diff", "--no-ext-diff", "--binary", "--no-index"]
    if stat:
        args.append("--stat")
    args.extend(["--", "/dev/null", relative_path])
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git diff timed out for untracked file: {relative_path}") from exc
    except OSError as exc:
        raise WorktreeError(f"git is unavailable: {exc}") from exc
    # git diff --no-index uses exit code 1 when the files differ, which is the
    # expected result for an untracked file compared with /dev/null.
    if result.returncode not in {0, 1}:
        detail = (result.stderr or result.stdout).strip()
        raise WorktreeError(f"git diff failed for {relative_path}: {detail[:1000]}")
    return result.stdout


class WorktreeManager:
    """Create and manage worktrees below one repository-owned directory."""

    def __init__(self, workspace: CodingWorkspace | str | Path):
        if isinstance(workspace, CodingWorkspace):
            root = Path(workspace.root_path)
        else:
            root = Path(workspace).expanduser()
        self.repo_root = root.resolve()
        self.base_dir = (self.repo_root / ".veya" / "worktrees").resolve()
        self._assert_repository()

    def _assert_repository(self) -> None:
        if not (self.repo_root / ".git").exists():
            raise WorktreeError(f"not a Git repository: {self.repo_root}")
        if _git_command(self.repo_root, ["rev-parse", "--is-inside-work-tree"]).strip() != "true":
            raise WorktreeError(f"not a Git worktree: {self.repo_root}")

    def _path_for(self, task_id: str) -> Path:
        validate_task_id(task_id)
        candidate = (self.base_dir / f"task-{task_id}").resolve()
        if candidate == self.base_dir or self.base_dir not in candidate.parents:
            raise WorktreeError("worktree path escapes the workspace worktree directory")
        return candidate

    def _assert_owned_path(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser().resolve()
        if candidate == self.base_dir or self.base_dir not in candidate.parents:
            raise WorktreeError(
                f"worktree path must be below the workspace worktree directory: {candidate}"
            )
        return candidate

    def _registered_worktree_meta(self) -> dict[Path, dict[str, Any]]:
        records: dict[Path, dict[str, Any]] = {}
        current_path: Path | None = None
        current_meta: dict[str, Any] = {}
        output = _git_command(self.repo_root, ["worktree", "list", "--porcelain"])
        for line in output.splitlines():
            line_str = line.strip()
            if not line_str:
                if current_path is not None:
                    records[current_path.resolve()] = current_meta
                    current_path = None
                    current_meta = {}
                continue
            if line_str.startswith("worktree "):
                if current_path is not None:
                    records[current_path.resolve()] = current_meta
                current_path = Path(line_str.removeprefix("worktree ").strip())
                current_meta = {"locked": False, "lock_reason": None}
            elif line_str.startswith("locked"):
                reason = line_str.removeprefix("locked").strip()
                current_meta["locked"] = True
                current_meta["lock_reason"] = reason or None
            elif line_str.startswith("branch "):
                current_meta["branch"] = line_str.removeprefix("branch ").strip()
        if current_path is not None:
            records[current_path.resolve()] = current_meta
        return records

    def _registered_paths(self) -> set[Path]:
        return set(self._registered_worktree_meta().keys())

    def _assert_registered(self, path: Path) -> None:
        if path not in self._registered_paths():
            raise WorktreeError(f"path is not a registered Git worktree: {path}")

    @staticmethod
    def _changed_files(status_output: str) -> list[str]:
        changed: list[str] = []
        for line in status_output.splitlines():
            if not line.strip():
                continue
            value = line[3:].strip() if len(line) > 3 else line.strip()
            if " -> " in value:
                value = value.split(" -> ", 1)[1]
            changed.append(value)
        return changed

    def status(
        self, task_id: str | None = None, *, path: str | Path | None = None
    ) -> WorktreeRecord:
        if (task_id is None) == (path is None):
            raise WorktreeError("provide exactly one of task_id or path")
        if task_id is not None:
            target = self._path_for(task_id)
        else:
            assert path is not None
            target = self._assert_owned_path(path)
        if not target.is_dir():
            raise WorktreeError(f"worktree does not exist: {target}")
        self._assert_registered(target)
        meta = self._registered_worktree_meta().get(target.resolve(), {})
        locked = meta.get("locked", False)
        lock_reason = meta.get("lock_reason")
        branch = _git_command(target, ["branch", "--show-current"]).strip() or "(detached)"
        status = _git_command(
            target,
            ["status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
        )
        resolved_task_id = target.name.removeprefix("task-")
        return WorktreeRecord(
            task_id=resolved_task_id,
            branch_name=branch,
            path=str(target),
            repo_root=str(self.repo_root),
            clean=not bool(status.strip()),
            changed_files=self._changed_files(status),
            locked=locked,
            lock_reason=lock_reason,
        )

    def create(
        self,
        task_id: str,
        objective: str,
        *,
        base_ref: str | None = None,
        branch_name: str | None = None,
    ) -> WorktreeRecord:
        validate_task_id(task_id)
        target = self._path_for(task_id)
        if target.exists():
            raise WorktreeError(f"worktree already exists: {target}")
        branch = _validate_branch_name(branch_name or branch_name_for(task_id, objective))
        start_ref = (
            base_ref or _git_command(self.repo_root, ["branch", "--show-current"]).strip() or "HEAD"
        )
        _git_command(self.repo_root, ["rev-parse", "--verify", start_ref])
        self.base_dir.mkdir(parents=True, exist_ok=True)
        existing_branch = _git_command(self.repo_root, ["branch", "--list", branch]).strip()
        if existing_branch:
            _git_command(
                self.repo_root,
                ["worktree", "add", str(target), branch],
            )
        else:
            _git_command(
                self.repo_root,
                ["worktree", "add", "-b", branch, str(target), start_ref],
            )
        self._provision_submodules(target)
        return self.status(path=target)

    def _provision_submodules(self, target: Path) -> None:
        if not (target / ".gitmodules").exists():
            return
        try:
            _git_command(target, ["submodule", "init"])
            git_modules_dir = self.repo_root / ".git" / "modules"
            if git_modules_dir.exists():
                lines = _git_command(
                    target,
                    ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
                ).splitlines()
                for line in lines:
                    parts = line.strip().split(None, 1)
                    if len(parts) == 2:
                        key, _subpath = parts
                        sub_name = key.removeprefix("submodule.").removesuffix(".path")
                        local_repo = git_modules_dir / sub_name
                        if local_repo.exists():
                            _git_command(
                                target,
                                ["config", f"submodule.{sub_name}.url", str(local_repo)],
                            )
            _git_command(
                target,
                [
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "update",
                    "--init",
                    "--recursive",
                    "--checkout",
                    "--no-fetch",
                ],
            )
        except Exception:
            pass

    def list(self) -> list[WorktreeRecord]:
        records: list[WorktreeRecord] = []
        for path in sorted(self._registered_paths()):
            if self.base_dir in path.parents and path.is_dir():
                records.append(self.status(path=path))
        return records

    def diff(self, task_id: str | None = None, *, path: str | Path | None = None) -> dict[str, Any]:
        record = self.status(task_id, path=path)
        target = Path(record.path)
        patch = _git_command(target, ["diff", "--no-ext-diff", "--binary", "HEAD"])
        stat = _git_command(target, ["diff", "--no-ext-diff", "--stat", "HEAD"])
        untracked = _git_command(target, ["ls-files", "--others", "--exclude-standard", "-z"])
        for relative_path in filter(None, untracked.split("\0")):
            patch_piece = _git_no_index_diff(target, relative_path)
            stat_piece = _git_no_index_diff(target, relative_path, stat=True)
            if patch_piece:
                patch = f"{patch.rstrip()}\n{patch_piece}" if patch else patch_piece
            if stat_piece:
                stat = f"{stat.rstrip()}\n{stat_piece}" if stat else stat_piece
        return {
            "path": record.path,
            "branch_name": record.branch_name,
            "clean": record.clean,
            "changed_files": record.changed_files,
            "stat": stat,
            "patch": patch,
        }

    def _owned_target(self, worktree_path: str | Path) -> Path:
        """Resolve a caller-supplied worktree and prove this manager owns it.

        Reused by every mutation below. ``_assert_owned_path`` refuses the
        workspace root itself, which is what keeps a commit out of the canonical
        tree: the canonical directory is not below the worktree directory, so it
        can never satisfy this.
        """
        target = self._assert_owned_path(worktree_path)
        self._assert_registered(target)
        return target

    @staticmethod
    def _relative_paths(paths: Any) -> list[str]:
        """Validate caller paths as worktree-relative, in-target, non-absolute.

        A path that is absolute or walks upwards is refused rather than
        normalised, because either could name a file outside the worktree and
        the caller would have no way to tell from the result.
        """
        if paths is None:
            return []
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, (list, tuple)):
            raise WorktreeError("paths must be a string or a list of strings")
        clean: list[str] = []
        for item in paths:
            if not isinstance(item, str) or not item.strip():
                raise WorktreeError("each path must be a non-empty string")
            value = item.strip()
            pure = PurePosixPath(value)
            if pure.is_absolute() or value.startswith("-") or ".." in pure.parts:
                raise WorktreeError(f"path must stay inside the worktree: {value!r}")
            if "\x00" in value:
                raise WorktreeError("path contains NUL byte")
            clean.append(value)
        return clean

    def stage(self, worktree_path: str | Path, *, paths: Any = None) -> dict[str, Any]:
        """Stage paths inside one owned worktree. Never the canonical tree."""
        target = self._owned_target(worktree_path)
        relative = self._relative_paths(paths)
        # No paths means "everything in this worktree", still bounded by the
        # worktree that _owned_target has already proven is not the canonical
        # tree. "-A" is a flag, so it must not sit behind the "--" that turns the
        # other entries into pathspecs.
        argv = ["add", "--", *relative] if relative else ["add", "-A"]
        output = _git_command(target, argv)
        staged = [
            line
            for line in _git_command(target, ["diff", "--cached", "--name-only"]).splitlines()
            if line.strip()
        ]
        return {
            "path": str(target),
            "branch_name": _git_command(target, ["rev-parse", "--abbrev-ref", "HEAD"]).strip(),
            "requested_paths": relative,
            "staged_paths": staged,
            "output": output.strip(),
        }

    def commit(
        self,
        worktree_path: str | Path,
        message: str,
        *,
        expect_paths: Any = None,
    ) -> dict[str, Any]:
        """Commit the staged state of one owned worktree.

        Fails closed rather than committing something unexpected: an empty index
        is refused, and when the caller declares the paths it expects, an index
        holding anything else is refused too. The returned SHA is read back from
        git, never assembled here.
        """
        target = self._owned_target(worktree_path)
        if not isinstance(message, str) or not message.strip():
            raise WorktreeError("commit message must be a non-empty string")
        staged = [
            line
            for line in _git_command(target, ["diff", "--cached", "--name-only"]).splitlines()
            if line.strip()
        ]
        if not staged:
            raise WorktreeError("nothing staged; refusing to create an empty commit")
        expected = self._relative_paths(expect_paths)
        if expected and sorted(expected) != sorted(staged):
            raise WorktreeError(
                f"staged paths do not match the expected set: staged={sorted(staged)} "
                f"expected={sorted(expected)}"
            )
        parent = _git_command(target, ["rev-parse", "HEAD"]).strip()
        _git_command(target, ["commit", "-m", message.strip()])
        commit_sha = _git_command(target, ["rev-parse", "HEAD"]).strip()
        if not commit_sha or commit_sha == parent:
            raise WorktreeError("git reported no new commit")
        tree = _git_command(target, ["rev-parse", "HEAD^{tree}"]).strip()
        return {
            "path": str(target),
            "branch_name": _git_command(target, ["rev-parse", "--abbrev-ref", "HEAD"]).strip(),
            "commit_sha": commit_sha,
            "parent_sha": parent,
            "tree": tree,
            "staged_paths": staged,
            "message": message.strip(),
        }

    def verify(
        self,
        worktree_path: str | Path,
        commit_sha: str,
        *,
        expect_paths: Any = None,
    ) -> dict[str, Any]:
        """Verify that a commit really exists in this worktree and says what it claims.

        Read-only, and deliberately not a second opinion about the commit: it
        reports what git holds, so a caller can check a SHA it was given instead
        of taking one on trust. ``verified`` is computed here from those reads —
        it is never asserted.

        Fails closed. A SHA that does not resolve to a commit in this repository,
        a commit whose parent or tree cannot be read, or a changed-path set that
        differs from what the caller declared, all raise. A ref expression
        resolves but is reported as unverified rather than raised, so the caller
        can see which commit was actually inspected.
        """
        target = self._owned_target(worktree_path)
        if not isinstance(commit_sha, str) or len(commit_sha.strip()) < 7:
            raise WorktreeError("commit_sha must be a git object name")

        wanted = commit_sha.strip()
        # Resolve inside this repository only. ``cat-file -e`` fails for a SHA
        # that exists elsewhere, which is what keeps another repository's commit
        # from being accepted here.
        probe = subprocess.run(
            ["git", "-C", str(target), "cat-file", "-e", f"{wanted}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if probe.returncode != 0:
            raise WorktreeError(f"commit not found in this repository: {wanted}")

        resolved = _git_command(target, ["rev-parse", f"{wanted}^{{commit}}"]).strip()
        if not resolved:
            raise WorktreeError(f"commit {wanted} did not resolve to a commit")
        if not resolved.startswith(wanted):
            # A ref expression (HEAD, a branch, a ``~``) resolves to a real
            # commit but is not the SHA the caller claimed. Verifying it would
            # answer about a commit they never named, so report it as refused
            # rather than as a passing verification.
            return {
                "path": str(target),
                "repo_root": str(self.repo_root),
                "ref_expression": wanted,
                "resolved_sha": resolved,
                "verified": False,
            }

        # ``_git_command`` raises on any non-zero exit, so an unreadable parent
        # or tree fails closed here without a separate emptiness check — a commit
        # with no readable parent, or a corrupt one, cannot return a result.
        parent = _git_command(target, ["rev-parse", f"{resolved}^"]).strip()
        tree = _git_command(target, ["rev-parse", f"{resolved}^{{tree}}"]).strip()

        changed = [
            line
            for line in _git_command(
                target,
                ["diff-tree", "--no-commit-id", "--name-only", "-r", resolved],
            ).splitlines()
            if line.strip()
        ]
        expected = self._relative_paths(expect_paths)
        if expected and sorted(expected) != sorted(changed):
            raise WorktreeError(
                f"commit paths do not match the expected set: commit={sorted(changed)} "
                f"expected={sorted(expected)}"
            )

        return {
            "path": str(target),
            "repo_root": str(self.repo_root),
            "branch_name": _git_command(target, ["rev-parse", "--abbrev-ref", "HEAD"]).strip(),
            "commit_sha": resolved,
            "parent_sha": parent,
            "tree": tree,
            "changed_paths": changed,
            "expected_paths": sorted(expected),
            "verified": True,
        }

    def discard(
        self,
        task_id: str | None = None,
        *,
        path: str | Path | None = None,
        force: bool = False,
    ) -> WorktreeRecord:
        if (task_id is None) == (path is None):
            raise WorktreeError("provide exactly one of task_id or path")
        if task_id is not None:
            target = self._path_for(task_id)
        else:
            assert path is not None
            target = self._assert_owned_path(path)
        record = self.status(path=target)
        if record.locked and not force:
            raise WorktreeError(f"worktree is locked: {record.lock_reason or 'locked'}")
        if not record.clean and not force:
            raise WorktreeError("worktree has uncommitted changes; pass force=True to discard")
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
            if record.locked:
                args.append("--force")
        args.append(str(target))
        _git_command(self.repo_root, args)
        return record

    def prune(self, *, dry_run: bool = False, verbose: bool = True) -> str:
        """Explicit maintenance sweep to prune stale worktree metadata."""
        args = ["worktree", "prune"]
        if dry_run:
            args.append("--dry-run")
        if verbose:
            args.append("-v")
        return _git_command(self.repo_root, args)


def repo_root_for_worktree(path: str | Path) -> Path:
    """Resolve the repository root for a standard ``.veya/worktrees`` path."""
    candidate = Path(path).expanduser().resolve()
    for ancestor in (candidate, *candidate.parents):
        if ancestor.name == "worktrees" and ancestor.parent.name == ".veya":
            root = ancestor.parent.parent
            if (root / ".git").exists() and ancestor in candidate.parents:
                return root
    raise WorktreeError(f"cannot resolve a Veya worktree path: {candidate}")


# Every terminal phase that authorises reclaiming the worktree.  TIMED_OUT and
# TERMINATED were missing here, so a timeout or an externally terminated
# execution retained its worktree forever and leaked one per run.  BLOCKED is
# deliberately excluded: a blocked execution is retryable, so its worktree is
# live state until it either resumes or reaches a real terminal phase.
_ALLOWED_TERMINAL_STATES: frozenset[str] = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "TERMINATED"}
)
_TEARDOWN_LOCKS: dict[str, threading.Lock] = {}
_TEARDOWN_LOCKS_MUTEX = threading.Lock()


def _get_teardown_lock(path_str: str) -> threading.Lock:
    with _TEARDOWN_LOCKS_MUTEX:
        return _TEARDOWN_LOCKS.setdefault(path_str, threading.Lock())


def _has_process_reference(target_path: Path) -> tuple[bool, str | None]:
    target_str = str(target_path.resolve())
    current_uid = os.getuid()
    try:
        proc = Path("/proc")
        if not proc.is_dir():
            return False, None
        for entry in os.scandir(proc):
            if not entry.name.isdigit():
                continue
            pid = entry.name
            try:
                st = entry.stat()
                if st.st_uid != current_uid:
                    continue
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue

            # 1. cwd
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
                if cwd == target_str or cwd.startswith(target_str + "/"):
                    return True, f"process {pid} has cwd in worktree: {cwd}"
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass

            # 2. exe
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
                if exe == target_str or exe.startswith(target_str + "/"):
                    return True, f"process {pid} exe in worktree: {exe}"
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass

            # 3. fd/*
            try:
                for fd_entry in os.scandir(f"/proc/{pid}/fd"):
                    try:
                        link = os.readlink(fd_entry.path)
                        if link == target_str or link.startswith(target_str + "/"):
                            return (
                                True,
                                f"process {pid} has open fd {fd_entry.name} in worktree: {link}",
                            )
                    except (FileNotFoundError, ProcessLookupError, PermissionError):
                        pass
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass

            # 4. maps
            try:
                with open(f"/proc/{pid}/maps", errors="ignore") as mf:
                    for line in mf:
                        if target_str in line:
                            return True, f"process {pid} has memory mapped file in worktree"
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass
    except Exception as exc:
        return True, f"process reference inspection failed: {exc}"
    return False, None


def teardown_worktree(
    path: str | Path,
    *,
    execution_status: str | None = None,
    force: bool = False,
    debug_retention: bool | None = None,
) -> dict[str, Any]:
    """Safely tear down a task worktree when its execution enters an authorized terminal state.

    Enforces fail-closed rules:
    - Retained if execution_status not in {'COMPLETED', 'FAILED', 'CANCELLED'} (fail-closed!)
    - Retained if debug_retention is enabled (or VEYA_WORKTREE_DEBUG_RETENTION env set)
    - Retained if active process references found in /proc (cwd, fd, exe, maps)
    - Retained if locked (unless force=True)
    - Retained if dirty / has uncommitted changes (unless force=True)
    - Discarded if clean, unlocked, and in an authorized terminal state
    - Idempotent: returns NOT_FOUND if already removed
    - Failsafe: never raises unhandled exceptions, returns FAILED with error detail
    """
    normalized_status = (execution_status or "").strip().upper()
    if normalized_status not in _ALLOWED_TERMINAL_STATES:
        status_label = normalized_status or "UNKNOWN"
        return {
            "cleaned": False,
            "status": f"{status_label}_PRESERVED",
            "path": str(path),
            "reason": f"execution state '{execution_status}' is not an authorized terminal state",
            "error": None,
            "record": None,
        }

    if debug_retention is None:
        debug_retention = os.environ.get("VEYA_WORKTREE_DEBUG_RETENTION", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if debug_retention:
        return {
            "cleaned": False,
            "status": "DEBUG_RETENTION_PRESERVED",
            "path": str(path),
            "reason": "worktree retained via debug retention policy",
            "error": None,
            "record": None,
        }

    target = Path(path).expanduser().resolve()
    lock = _get_teardown_lock(str(target))
    with lock:
        if not target.exists():
            return {
                "cleaned": False,
                "status": "NOT_FOUND",
                "path": str(target),
                "reason": "worktree does not exist",
                "error": None,
                "record": None,
            }

        try:
            repo_root = repo_root_for_worktree(target)
        except WorktreeError as exc:
            return {
                "cleaned": False,
                "status": "NOT_OWNED",
                "path": str(target),
                "reason": str(exc),
                "error": str(exc),
                "record": None,
            }

        has_ref, ref_reason = _has_process_reference(target)
        if has_ref:
            return {
                "cleaned": False,
                "status": "ACTIVE_PRESERVED",
                "path": str(target),
                "reason": ref_reason or "active process reference found in /proc",
                "error": None,
                "record": None,
            }

        try:
            manager = WorktreeManager(repo_root)
            record = manager.status(path=target)

            if record.locked and not force:
                return {
                    "cleaned": False,
                    "status": "LOCKED_PRESERVED",
                    "path": str(target),
                    "reason": f"worktree is locked: {record.lock_reason or 'locked'}",
                    "error": None,
                    "record": record.to_dict(),
                }

            if not record.clean and not force:
                return {
                    "cleaned": False,
                    "status": "DIRTY_PRESERVED",
                    "path": str(target),
                    "reason": f"worktree has uncommitted changes: {len(record.changed_files)} files modified",
                    "error": None,
                    "record": record.to_dict(),
                }

            manager.discard(path=target, force=force)
            return {
                "cleaned": True,
                "status": "CLEANED",
                "path": str(target),
                "reason": "clean worktree discarded",
                "error": None,
                "record": record.to_dict(),
            }
        except Exception as exc:
            return {
                "cleaned": False,
                "status": "FAILED",
                "path": str(target),
                "reason": str(exc),
                "error": str(exc),
                "record": None,
            }


@dataclass
class WorktreeMetrics:
    veya_worktrees_total: int = 0
    veya_worktrees_active: int = 0
    veya_worktrees_terminal_retained: int = 0
    veya_worktrees_dirty: int = 0
    veya_worktrees_cleanup_failed: int = 0
    veya_worktree_bytes: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "veya_worktrees_total": self.veya_worktrees_total,
            "veya_worktrees_active": self.veya_worktrees_active,
            "veya_worktrees_terminal_retained": self.veya_worktrees_terminal_retained,
            "veya_worktrees_dirty": self.veya_worktrees_dirty,
            "veya_worktrees_cleanup_failed": self.veya_worktrees_cleanup_failed,
            "veya_worktree_bytes": self.veya_worktree_bytes,
        }


def collect_worktree_metrics(
    workspace: CodingWorkspace | str | Path,
    *,
    store: Any = None,
) -> WorktreeMetrics:
    manager = WorktreeManager(workspace)
    records = manager.list()
    metrics = WorktreeMetrics(veya_worktrees_total=len(records))
    for rec in records:
        wt_path = Path(rec.path)
        try:
            total_b = sum(
                f.stat().st_size for f in wt_path.rglob("*") if f.is_file() and not f.is_symlink()
            )
            metrics.veya_worktree_bytes += total_b
        except Exception:
            pass

        if not rec.clean:
            metrics.veya_worktrees_dirty += 1

        has_ref, _ = _has_process_reference(wt_path)
        if has_ref or rec.locked:
            metrics.veya_worktrees_active += 1
        elif not rec.clean:
            metrics.veya_worktrees_terminal_retained += 1
    return metrics


def reap_stale_worktrees(
    workspace: CodingWorkspace | str | Path,
    *,
    execution_status_lookup: Callable[[str], str | None] | None = None,
    max_batch: int = 50,
) -> dict[str, Any]:
    """Reconcile and safely clean up terminal, clean, unlocked worktrees with no process references."""
    manager = WorktreeManager(workspace)
    records = manager.list()
    reaped: list[str] = []
    skipped: list[dict[str, str]] = []

    for rec in records:
        if len(reaped) >= max_batch:
            break
        wt_path = Path(rec.path)
        if not (wt_path.name.startswith("task-") or wt_path.name.startswith("remote-")):
            continue
        if rec.locked:
            skipped.append({"path": rec.path, "reason": "locked"})
            continue
        if not rec.clean:
            skipped.append({"path": rec.path, "reason": "dirty"})
            continue
        has_ref, ref_reason = _has_process_reference(wt_path)
        if has_ref:
            skipped.append({"path": rec.path, "reason": ref_reason or "active process reference"})
            continue

        exec_status = "COMPLETED"
        if execution_status_lookup is not None:
            exec_status = execution_status_lookup(rec.task_id) or "UNKNOWN"

        res = teardown_worktree(wt_path, execution_status=exec_status)
        if res.get("cleaned"):
            reaped.append(rec.path)
        else:
            skipped.append(
                {"path": rec.path, "reason": str(res.get("reason") or res.get("status") or "")}
            )

    return {
        "scanned": len(records),
        "reaped": len(reaped),
        "reaped_paths": reaped,
        "skipped": skipped,
    }


__all__ = [
    "WorktreeError",
    "WorktreeManager",
    "WorktreeMetrics",
    "WorktreeRecord",
    "branch_name_for",
    "collect_worktree_metrics",
    "reap_stale_worktrees",
    "repo_root_for_worktree",
    "teardown_worktree",
    "validate_task_id",
]
