"""Task-scoped Git worktree isolation for local coding runs."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

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

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "branch_name": self.branch_name,
            "path": self.path,
            "repo_root": self.repo_root,
            "clean": self.clean,
            "changed_files": list(self.changed_files),
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

    def _registered_paths(self) -> set[Path]:
        records: set[Path] = set()
        current_path: Path | None = None
        output = _git_command(self.repo_root, ["worktree", "list", "--porcelain"])
        for line in output.splitlines():
            if line.startswith("worktree "):
                if current_path is not None:
                    records.add(current_path.resolve())
                current_path = Path(line.removeprefix("worktree ").strip())
        if current_path is not None:
            records.add(current_path.resolve())
        return records

    def _assert_registered(self, path: Path) -> None:
        if path not in self._registered_paths():
            raise WorktreeError(f"path is not a registered Git worktree: {path}")

    @staticmethod
    def _changed_files(status_output: str) -> list[str]:
        changed: list[str] = []
        for line in status_output.splitlines():
            if not line:
                continue
            value = line[3:] if len(line) > 3 else line
            changed.append(value)
        return changed

    def status(
        self, task_id: str | None = None, *, path: str | Path | None = None
    ) -> WorktreeRecord:
        if (task_id is None) == (path is None):
            raise WorktreeError("provide exactly one of task_id or path")
        target = self._path_for(task_id) if task_id is not None else self._assert_owned_path(path)
        if not target.is_dir():
            raise WorktreeError(f"worktree does not exist: {target}")
        self._assert_registered(target)
        branch = _git_command(target, ["branch", "--show-current"]).strip() or "(detached)"
        status = _git_command(target, ["status", "--short"])
        resolved_task_id = target.name.removeprefix("task-")
        return WorktreeRecord(
            task_id=resolved_task_id,
            branch_name=branch,
            path=str(target),
            repo_root=str(self.repo_root),
            clean=not bool(status.strip()),
            changed_files=self._changed_files(status),
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
        _git_command(
            self.repo_root,
            ["worktree", "add", "-b", branch, str(target), start_ref],
        )
        return self.status(path=target)

    def list(self) -> list[WorktreeRecord]:
        records: list[WorktreeRecord] = []
        for path in sorted(self._registered_paths()):
            if self.base_dir in path.parents and path.is_dir():
                records.append(self.status(path=path))
        return records

    def diff(
        self, task_id: str | None = None, *, path: str | Path | None = None
    ) -> dict[str, str | list[str]]:
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

    def discard(self, task_id: str, *, force: bool = False) -> WorktreeRecord:
        target = self._path_for(task_id)
        record = self.status(path=target)
        if not record.clean and not force:
            raise WorktreeError("worktree has uncommitted changes; pass force=True to discard")
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(target))
        _git_command(self.repo_root, args)
        return record


def repo_root_for_worktree(path: str | Path) -> Path:
    """Resolve the repository root for a standard ``.veya/worktrees`` path."""
    candidate = Path(path).expanduser().resolve()
    for ancestor in (candidate, *candidate.parents):
        if ancestor.name == "worktrees" and ancestor.parent.name == ".veya":
            root = ancestor.parent.parent
            if (root / ".git").exists() and ancestor in candidate.parents:
                return root
    raise WorktreeError(f"cannot resolve a Veya worktree path: {candidate}")


__all__ = [
    "WorktreeError",
    "WorktreeManager",
    "WorktreeRecord",
    "branch_name_for",
    "repo_root_for_worktree",
    "validate_task_id",
]
