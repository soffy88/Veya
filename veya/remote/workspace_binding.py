"""Canonical workspace resolution + binding contract for the remote gateway (P0-A/B/M).

One contract, no guessing:

    explicit requested workspace
        -> canonicalize(realpath)
        -> validate exists / directory
        -> validate allowed root
        -> resolve git repo identity
        -> (adapter) create/reuse task worktree for THAT repo
        -> verify worktree.repo_root == requested repo
        -> execute

An explicit workspace is authoritative. The resolver never falls back to the
MCP server cwd, a previous session workspace, the default workspace, a
previously bound repo or an arbitrary active worktree. A mismatch is a
fail-closed ``BLOCKED`` — never a "closest" guess.

Path canonicalization only; discovering the Git repo root walks the filesystem
for a ``.git`` entry (no ``git`` subprocess here). The stronger
``verify_worktree_repo_identity`` check uses the canonical worktree record that
``coding_worktree_create`` already returns, so the adapter still never spawns
Git itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

_GITDIR_RE = re.compile(r"^gitdir:\s*(.+)$")


class WorkspaceBindingError(Exception):
    """Binding failed; ``code`` is a remote error code, ``reason`` is stable."""

    def __init__(self, code: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class RepoResolution:
    """Canonical target/repository selection for one remote tool operation.

    ``workspace_root`` is the authorization boundary supplied by the client or
    session.  ``target_path`` is the realpath of the requested path inside that
    boundary.  ``repo_root`` is the nearest Git repository containing the
    target, if one exists.  Worktree-backed callers must require a repository;
    read-only callers may deliberately receive ``repo_root=None`` for ordinary
    directories.
    """

    workspace_root: str
    requested_path: str
    target_path: str
    repo_root: str | None
    repo_identity: str | None
    operation: str
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def is_git_repo(self) -> bool:
        return self.repo_root is not None

    def to_public(self) -> dict[str, object]:
        return {
            "canonical_workspace_root": self.workspace_root,
            "requested_path": self.requested_path,
            "canonical_target_path": self.target_path,
            "resolved_repo_root": self.repo_root,
            "repo_identity": self.repo_identity,
            "operation": self.operation,
            "evidence": dict(self.evidence),
        }


def canonical(path: str | Path) -> str:
    """Canonical (realpath) form used for every workspace identity comparison."""

    return str(Path(path).expanduser().resolve())


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def git_repo_root(path: str | Path) -> Path | None:
    """Nearest ancestor containing a ``.git`` entry (dir or file), else ``None``."""

    candidate = Path(path).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for ancestor in (candidate, *candidate.parents):
        if (ancestor / ".git").exists():
            return ancestor
    return None


def _git_common_dir(repo_root: Path) -> Path | None:
    dotgit = repo_root / ".git"
    if dotgit.is_dir():
        return dotgit.resolve()
    if dotgit.is_file():
        try:
            text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:  # pragma: no cover - defensive
            return None
        match = _GITDIR_RE.match(text)
        if not match:
            return None
        target = Path(match.group(1))
        if not target.is_absolute():
            target = repo_root / target
        target = target.resolve()
        # Linked worktree gitdir is ``<main>/.git/worktrees/<name>``; the shared
        # repository identity is the common ``<main>/.git`` directory.
        if target.parent.name == "worktrees":
            return target.parent.parent
        return target
    return None


def git_repo_identity(repo_root: str | Path) -> str:
    """Stable repository identity based on the canonical repo root / common dir."""

    root = Path(repo_root).expanduser().resolve()
    common = _git_common_dir(root)
    if common is not None:
        return f"git:{common}"
    return f"path:{root}"


@dataclass(frozen=True)
class WorkspaceBinding:
    """Resolved identity for one explicit requested workspace."""

    requested_path: str
    requested_realpath: str
    repo_root: str
    repo_identity: str
    is_git_repo: bool
    worktree_path: str | None = None
    worktree_repo_root: str | None = None
    worktree_repo_identity: str | None = None
    canonical_target_path: str | None = None
    resolution_evidence: dict[str, object] = field(default_factory=dict)
    status: str = "PASS"
    reason: str | None = None

    def with_worktree(self, worktree_path: str, worktree_repo_root: str) -> WorkspaceBinding:
        return replace(
            self,
            worktree_path=str(Path(worktree_path).resolve()),
            worktree_repo_root=str(Path(worktree_repo_root).resolve()),
            worktree_repo_identity=git_repo_identity(worktree_repo_root),
        )

    def with_resolution(self, resolution: RepoResolution) -> WorkspaceBinding:
        """Return this binding with the operation's canonical repository."""

        if resolution.repo_root is None or resolution.repo_identity is None:
            raise WorkspaceBindingError(
                "WORKSPACE_DENIED",
                "NOT_A_GIT_REPOSITORY",
                f"operation requires a Git repository: {resolution.target_path}",
            )
        return replace(
            self,
            # The session binding remains the containment/security root.  This
            # operation binding reports the canonical selector target so
            # downstream worktree/Hicode records cannot look as if they ran
            # at the parent root when a nested repo was selected.
            requested_path=resolution.target_path,
            requested_realpath=resolution.target_path,
            repo_root=resolution.repo_root,
            repo_identity=resolution.repo_identity,
            is_git_repo=True,
            canonical_target_path=resolution.target_path,
            resolution_evidence=dict(resolution.evidence),
        )

    def to_public(self) -> dict[str, object]:
        return {
            "requested_path": self.requested_path,
            "requested_realpath": self.requested_realpath,
            "resolved_repo_root": self.repo_root,
            "repo_identity": self.repo_identity,
            "execution_worktree": self.worktree_path,
            "worktree_repo_root": self.worktree_repo_root,
            "worktree_repo_identity": self.worktree_repo_identity,
            "canonical_workspace_root": self.requested_realpath,
            "canonical_target_path": self.canonical_target_path,
            "resolution_evidence": dict(self.resolution_evidence),
            "workspace_binding": self.status,
            "reason": self.reason,
        }


def resolve_repo_target(
    bound_workspace_root: str | Path,
    requested_path: str | Path | None = None,
    *,
    operation: str,
    allowed_roots: tuple[str, ...] | list[str] = (),
    require_repo: bool = False,
) -> RepoResolution:
    """Resolve one operation target and its owning nested Git repository.

    This is the sole repo-selection primitive used by remote tools.  It never
    trusts the process cwd, a prior worktree, or command text.  The optional
    command compatibility path in the adapter must provide a normal
    ``requested_path`` before calling this function.
    """

    bound_text = str(bound_workspace_root or "").strip()
    if not bound_text:
        raise WorkspaceBindingError(
            "INVALID_ARGUMENT", "MISSING_WORKSPACE", "an explicit workspace is required"
        )
    bound = Path(bound_text).expanduser().resolve()
    if not bound.exists():
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED", "WORKSPACE_NOT_FOUND", f"workspace does not exist: {bound}"
        )
    if not bound.is_dir():
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED",
            "WORKSPACE_NOT_A_DIRECTORY",
            f"workspace is not a directory: {bound}",
        )

    canonical_roots = [Path(root).expanduser().resolve() for root in allowed_roots if root]
    if canonical_roots and not any(_is_within(bound, root) for root in canonical_roots):
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED",
            "WORKSPACE_NOT_ALLOWED",
            f"workspace is not authorized: {bound}",
        )

    requested_text = str(requested_path or ".")
    if "\x00" in requested_text:
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED", "PATH_CONTAINS_NUL", "requested path contains NUL byte"
        )
    candidate = Path(requested_text).expanduser()
    if not candidate.is_absolute():
        candidate = bound / candidate
    target = candidate.resolve(strict=False)
    if not _is_within(target, bound):
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED",
            "PATH_ESCAPES_WORKSPACE",
            f"path escapes workspace root: {requested_text!r}",
        )

    repo = git_repo_root(target)
    if repo is not None and not _is_within(repo, bound):
        # This should only be reachable through unusual symlink/gitdir layouts;
        # keep the second containment check explicit and fail closed.
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED",
            "REPO_ESCAPES_WORKSPACE",
            f"resolved repository escapes workspace root: {repo}",
        )
    repo_identity = git_repo_identity(repo) if repo is not None else None
    discovery = (
        "bound_workspace_root"
        if repo is not None and repo == bound
        else "nested_git_repository"
        if repo is not None
        else "no_git_repository"
    )
    evidence: dict[str, object] = {
        "operation": operation,
        "bound_workspace_root": str(bound),
        "requested_path": requested_text,
        "canonical_target_path": str(target),
        "repo_discovery": discovery,
        "target_within_workspace": True,
        "repo_within_workspace": repo is None or _is_within(repo, bound),
    }
    if repo is not None:
        evidence["resolved_repo_root"] = str(repo)
        evidence["repo_identity"] = repo_identity
    if require_repo and repo is None:
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED",
            "NOT_A_GIT_REPOSITORY",
            f"operation requires a nested Git repository: {target}",
        )
    return RepoResolution(
        workspace_root=str(bound),
        requested_path=requested_text,
        target_path=str(target),
        repo_root=str(repo) if repo is not None else None,
        repo_identity=repo_identity,
        operation=operation,
        evidence=evidence,
    )


def resolve_requested_workspace(
    requested: str | Path,
    *,
    allowed_roots: tuple[str, ...] | list[str] = (),
    require_git: bool = False,
) -> WorkspaceBinding:
    """Resolve one explicit requested workspace, fail-closed.

    ``allowed_roots`` is the token/session authorization set; when it is
    non-empty the canonical requested path must sit inside one of them.
    """

    text = str(requested or "").strip()
    if not text:
        raise WorkspaceBindingError(
            "INVALID_ARGUMENT", "MISSING_WORKSPACE", "an explicit workspace is required"
        )
    resolution = resolve_repo_target(
        text,
        ".",
        operation="workspace.bind",
        allowed_roots=allowed_roots,
        require_repo=require_git,
    )
    real = Path(resolution.workspace_root)
    repo = Path(resolution.repo_root) if resolution.repo_root else None
    if repo is None:
        if require_git:
            raise WorkspaceBindingError(
                "WORKSPACE_DENIED",
                "NOT_A_GIT_REPOSITORY",
                f"workspace is not inside a Git repository: {real}",
            )
        return WorkspaceBinding(
            requested_path=text,
            requested_realpath=str(real),
            repo_root=str(real),
            repo_identity=f"path:{real}",
            is_git_repo=False,
            canonical_target_path=resolution.target_path,
            resolution_evidence=dict(resolution.evidence),
        )
    return WorkspaceBinding(
        requested_path=text,
        requested_realpath=str(real),
        repo_root=str(repo),
        repo_identity=git_repo_identity(repo),
        is_git_repo=True,
        canonical_target_path=resolution.target_path,
        resolution_evidence=dict(resolution.evidence),
    )


def verify_worktree_repo_identity(
    binding: WorkspaceBinding,
    *,
    worktree_path: str,
    worktree_repo_root: str,
) -> WorkspaceBinding:
    """Enforce ``canonical(worktree_repo_root) == canonical(resolved repo)``.

    ``worktree_repo_root`` is the ``repo_root`` the canonical
    ``coding_worktree_create`` recorded for this worktree — the executor must
    not run when it names a different repository than the requested workspace.
    """

    expected = Path(binding.repo_root).resolve()
    actual = Path(worktree_repo_root).resolve()
    if actual != expected:
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED",
            "WORKTREE_REPO_IDENTITY_MISMATCH",
            (
                "task worktree belongs to a different repository "
                f"(worktree_repo_root={actual}, requested_repo_root={expected})"
            ),
        )
    wt = Path(worktree_path).resolve()
    if not wt.exists():
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED",
            "WORKTREE_NOT_FOUND",
            f"task worktree does not exist: {wt}",
        )
    bound = Path(binding.requested_realpath).resolve()
    if not _is_within(wt, bound):
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED",
            "WORKTREE_ESCAPES_WORKSPACE",
            f"task worktree escapes the bound workspace: {wt}",
        )
    return binding.with_worktree(str(wt), str(actual))


__all__ = [
    "RepoResolution",
    "WorkspaceBinding",
    "WorkspaceBindingError",
    "canonical",
    "git_repo_identity",
    "git_repo_root",
    "resolve_repo_target",
    "resolve_requested_workspace",
    "verify_worktree_repo_identity",
]
