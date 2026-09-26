"""Execution-scoped worktrees, leases, and canonical promotion.

This module is deliberately a resource substrate.  It does not own GoalRun or
Execution lifecycle; callers supply the execution identity and remain the
durable execution authority.  The one invariant enforced here is:

    (execution_id, repo_identity) -> one persistent worktree

The registry is JSON-backed so a gateway restart can reattach to an existing
worktree without silently creating a replacement and losing dirty state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from runtime.coding.worktree import WorktreeError, WorktreeManager

from .workspace_binding import canonical, git_repo_identity, git_repo_root


class ExecutionWorktreeState(StrEnum):
    ACTIVE = "ACTIVE"
    FINALIZING = "FINALIZING"
    PROMOTABLE = "PROMOTABLE"
    PROMOTED = "PROMOTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ROLLED_BACK = "ROLLED_BACK"
    ORPHANED = "ORPHANED"
    CLEANED = "CLEANED"


class ExecutionWorktreeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ExecutionWorktreeBinding:
    execution_id: str
    goal_run_id: str | None
    repo_identity: str
    canonical_repo_root: str
    worktree_path: str
    worktree_id: str
    base_sha: str
    current_head_sha: str
    target_branch: str
    execution_branch: str
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    state: str = str(ExecutionWorktreeState.ACTIVE)
    commit_sha: str | None = None
    promotion_evidence: dict[str, Any] | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.execution_id, self.repo_identity

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExecutionWorktreeBinding:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in known})


@dataclass
class ExecutionRepoWriteLease:
    execution_id: str
    repo_identity: str
    holder_worker_id: str
    acquired_at: float
    heartbeat_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PromotionEvidence:
    execution_id: str
    repo_identity: str
    base_sha: str
    execution_commit_sha: str
    canonical_before_sha: str
    canonical_after_sha: str | None
    target_branch: str
    promotion_mode: str
    verification_evidence: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ExecutionWorktreeError("GIT_FAILED", f"git {' '.join(args)} failed: {detail[:1000]}")
    return proc.stdout.strip()


def _slug(value: str) -> str:
    raw = "".join(char if char.isalnum() else "-" for char in value.lower())
    return raw.strip("-")[:32] or "execution"


def _target_branch(repo_root: Path) -> str:
    branch = _git(repo_root, "branch", "--show-current")
    if branch:
        return branch
    return (
        _git(repo_root, "symbolic-ref", "refs/remotes/origin/HEAD", check=False).removeprefix(
            "refs/remotes/origin/"
        )
        or "main"
    )


class ExecutionWorktreeRegistry:
    """Durable registry and sole resolver for execution worktree resources."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root is not None else None
        self._lock = threading.RLock()
        self._bindings: dict[tuple[str, str], ExecutionWorktreeBinding] = {}
        self._leases: dict[tuple[str, str], ExecutionRepoWriteLease] = {}
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._load()

    def _path(self, execution_id: str, repo_identity: str) -> Path:
        digest = hashlib.sha256(f"{execution_id}\0{repo_identity}".encode()).hexdigest()[:32]
        return self.root / f"binding-{digest}.json"  # type: ignore[operator]

    def _persist(self, binding: ExecutionWorktreeBinding) -> None:
        if self.root is None:
            return
        target = self._path(binding.execution_id, binding.repo_identity)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(binding.to_dict(), sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)

    def _load(self) -> None:
        assert self.root is not None
        for path in self.root.glob("binding-*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                binding = ExecutionWorktreeBinding.from_dict(payload)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            self._bindings[binding.key] = binding

    @staticmethod
    def _repo(repo: str | Path) -> tuple[Path, str]:
        requested = Path(repo).expanduser().resolve()
        root = git_repo_root(requested)
        if root is None:
            raise ExecutionWorktreeError("NOT_A_GIT_REPOSITORY", str(requested))
        return root, git_repo_identity(root)

    def get(self, execution_id: str, repo_identity: str) -> ExecutionWorktreeBinding | None:
        with self._lock:
            return self._bindings.get((execution_id, repo_identity))

    def resolve(self, execution_id: str, repo: str | Path) -> ExecutionWorktreeBinding:
        root, identity = self._repo(repo)
        binding = self.get(execution_id, identity)
        if binding is None:
            raise ExecutionWorktreeError(
                "WORKTREE_NOT_BOUND", f"no binding for {execution_id}:{identity}"
            )
        if not Path(binding.worktree_path).is_dir():
            binding.state = str(ExecutionWorktreeState.ORPHANED)
            self._persist(binding)
            raise ExecutionWorktreeError("WORKTREE_LOST", binding.worktree_path)
        if canonical(binding.canonical_repo_root) != canonical(root):
            raise ExecutionWorktreeError("REPOSITORY_IDENTITY_MISMATCH", identity)
        actual = git_repo_identity(binding.worktree_path)
        if actual != identity:
            raise ExecutionWorktreeError("WORKTREE_IDENTITY_MISMATCH", actual)
        binding.last_used_at = time.time()
        self._persist(binding)
        return binding

    def get_or_create(
        self,
        execution_id: str,
        repo: str | Path,
        *,
        goal_run_id: str | None = None,
        objective: str = "execution",
        target_branch: str | None = None,
    ) -> ExecutionWorktreeBinding:
        root, identity = self._repo(repo)
        key = (execution_id, identity)
        with self._lock:
            existing = self._bindings.get(key)
            if existing is not None:
                return self.resolve(execution_id, root)
            base_sha = _git(root, "rev-parse", "HEAD")
            target = target_branch or _target_branch(root)
            branch = f"veya/execution/{_slug(execution_id)}/{_slug(root.name)}"
            task_id = f"execution-{_slug(execution_id)}-{hashlib.sha1(identity.encode()).hexdigest()[:10]}"
            try:
                record = WorktreeManager(root).create(
                    task_id=task_id,
                    objective=objective,
                    base_ref=base_sha,
                    branch_name=branch,
                )
            except WorktreeError as exc:
                raise ExecutionWorktreeError("WORKTREE_CREATE_FAILED", str(exc)) from exc
            binding = ExecutionWorktreeBinding(
                execution_id=execution_id,
                goal_run_id=goal_run_id,
                repo_identity=identity,
                canonical_repo_root=str(root),
                worktree_path=record.path,
                worktree_id=task_id,
                base_sha=base_sha,
                current_head_sha=_git(Path(record.path), "rev-parse", "HEAD"),
                target_branch=target,
                execution_branch=branch,
            )
            self._bindings[key] = binding
            self._persist(binding)
            return binding

    def mark(self, binding: ExecutionWorktreeBinding, state: ExecutionWorktreeState) -> None:
        with self._lock:
            binding.state = str(state)
            binding.last_used_at = time.time()
            self._persist(binding)

    def acquire_lease(
        self, execution_id: str, repo_identity: str, worker_id: str, *, ttl_s: float = 60.0
    ) -> ExecutionRepoWriteLease:
        now = time.time()
        key = (execution_id, repo_identity)
        with self._lock:
            current = self._leases.get(key)
            if current and current.expires_at > now and current.holder_worker_id != worker_id:
                raise ExecutionWorktreeError("WRITE_LEASE_HELD", current.holder_worker_id)
            lease = ExecutionRepoWriteLease(
                execution_id, repo_identity, worker_id, now, now, now + max(1.0, ttl_s)
            )
            self._leases[key] = lease
            return lease

    def heartbeat_lease(self, lease: ExecutionRepoWriteLease, *, ttl_s: float = 60.0) -> None:
        with self._lock:
            current = self._leases.get((lease.execution_id, lease.repo_identity))
            if current is None or current.holder_worker_id != lease.holder_worker_id:
                raise ExecutionWorktreeError("WRITE_LEASE_NOT_HELD", lease.holder_worker_id)
            now = time.time()
            current.heartbeat_at = now
            current.expires_at = now + max(1.0, ttl_s)

    def release_lease(self, lease: ExecutionRepoWriteLease) -> None:
        with self._lock:
            key = (lease.execution_id, lease.repo_identity)
            current = self._leases.get(key)
            if current and current.holder_worker_id == lease.holder_worker_id:
                self._leases.pop(key, None)

    def reap_leases(self) -> int:
        now = time.time()
        with self._lock:
            expired = [key for key, lease in self._leases.items() if lease.expires_at <= now]
            for key in expired:
                self._leases.pop(key, None)
            return len(expired)


class PromotionConflict(ExecutionWorktreeError):
    pass


class CanonicalPromotionService:
    """The only promotion authority for execution worktree commits."""

    def __init__(
        self, registry: ExecutionWorktreeRegistry, side_effect_ledger: Any | None = None
    ) -> None:
        self.registry = registry
        self.side_effect_ledger = side_effect_ledger

    def promote(
        self,
        execution_id: str,
        repo: str | Path,
        *,
        expected_base_sha: str,
        verification_evidence: dict[str, Any] | None = None,
        side_effect_ledger: Any | None = None,
    ) -> PromotionEvidence:
        binding = self.registry.resolve(execution_id, repo)
        if binding.base_sha != expected_base_sha:
            raise PromotionConflict("BASE_SHA_MISMATCH", binding.base_sha)
        if binding.state not in {
            str(ExecutionWorktreeState.PROMOTABLE),
            str(ExecutionWorktreeState.PROMOTED),
        }:
            raise ExecutionWorktreeError("NOT_PROMOTABLE", binding.state)
        if not binding.commit_sha:
            raise ExecutionWorktreeError("COMMIT_MISSING", "execution has no finalized commit")
        if not verification_evidence and binding.state != str(ExecutionWorktreeState.PROMOTED):
            raise ExecutionWorktreeError(
                "VERIFICATION_MISSING", "promotion requires verification evidence"
            )
        source = Path(binding.worktree_path)
        canonical_root = Path(binding.canonical_repo_root)
        commit_sha = _git(source, "rev-parse", "HEAD")
        if commit_sha != binding.commit_sha:
            raise ExecutionWorktreeError(
                "COMMIT_MISMATCH", f"binding={binding.commit_sha} head={commit_sha}"
            )
        if binding.state == str(ExecutionWorktreeState.PROMOTED) and binding.commit_sha:
            current = _git(
                canonical_root,
                "rev-parse",
                f"refs/heads/{binding.target_branch}",
            )
            if current == binding.commit_sha:
                prior = binding.promotion_evidence or {}
                stored_evidence = PromotionEvidence(
                    execution_id=execution_id,
                    repo_identity=binding.repo_identity,
                    base_sha=binding.base_sha,
                    execution_commit_sha=binding.commit_sha,
                    canonical_before_sha=str(prior.get("canonical_before_sha") or current),
                    canonical_after_sha=str(prior.get("canonical_after_sha") or current),
                    target_branch=str(prior.get("target_branch") or binding.target_branch),
                    promotion_mode=str(prior.get("promotion_mode") or "fast_forward_ref_cas"),
                    verification_evidence=dict(
                        prior.get("verification_evidence") or verification_evidence or {}
                    ),
                    timestamp=float(prior.get("timestamp") or time.time()),
                )
                # Keep replay evidence byte-for-byte equivalent to the first
                # promotion when a durable ledger is attached.  The ledger's
                # request hash is the idempotency guard, so replacing the
                # original verification payload with an empty retry payload
                # would incorrectly look like a conflicting operation.
                self._ledger_promotion(
                    stored_evidence, side_effect_ledger, goal_run_id=binding.goal_run_id
                )
                return PromotionEvidence(
                    **{
                        **stored_evidence.to_dict(),
                        "promotion_mode": "IDEMPOTENT_ALREADY_PROMOTED",
                    }
                )
        if commit_sha == binding.base_sha:
            raise ExecutionWorktreeError("NOT_FINALIZED", "execution has no commit")
        canonical_before = _git(canonical_root, "rev-parse", f"refs/heads/{binding.target_branch}")
        if canonical_before != binding.base_sha:
            raise PromotionConflict("PROMOTION_CONFLICT", f"canonical advanced: {canonical_before}")
        # CAS update-ref changes repository history only.  It does not reset,
        # clean, stash, or overwrite unrelated dirty files in the checkout.
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(canonical_root),
                "update-ref",
                f"refs/heads/{binding.target_branch}",
                commit_sha,
                canonical_before,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise PromotionConflict("PROMOTION_RACE", (proc.stderr or proc.stdout).strip())
        after = _git(canonical_root, "rev-parse", f"refs/heads/{binding.target_branch}")
        evidence = PromotionEvidence(
            execution_id=execution_id,
            repo_identity=binding.repo_identity,
            base_sha=binding.base_sha,
            execution_commit_sha=commit_sha,
            canonical_before_sha=canonical_before,
            canonical_after_sha=after,
            target_branch=binding.target_branch,
            promotion_mode="fast_forward_ref_cas",
            verification_evidence=dict(verification_evidence or {}),
        )
        binding.commit_sha = commit_sha
        binding.current_head_sha = commit_sha
        binding.state = str(ExecutionWorktreeState.PROMOTED)
        binding.promotion_evidence = evidence.to_dict()
        self.registry._persist(binding)
        self._ledger_promotion(evidence, side_effect_ledger, goal_run_id=binding.goal_run_id)
        return evidence

    def _ledger_promotion(
        self,
        evidence: PromotionEvidence,
        ledger: Any | None,
        *,
        goal_run_id: str | None = None,
    ) -> None:
        """Persist promotion truth through the existing SideEffectLedger.

        The Git CAS is completed before this call.  If durable ledger
        persistence fails, the exception is intentionally propagated: the
        canonical ref may already have advanced, but the caller must enter
        durable recovery rather than report ordinary success.
        """

        ledger = ledger or self.side_effect_ledger
        if ledger is None:
            return
        operation_key = hashlib.sha256(
            "\0".join(
                (
                    evidence.execution_id,
                    evidence.repo_identity,
                    evidence.execution_commit_sha,
                    evidence.target_branch,
                )
            ).encode()
        ).hexdigest()
        verification_digest = hashlib.sha256(
            json.dumps(evidence.verification_evidence, sort_keys=True, default=str).encode()
        ).hexdigest()

        async def record() -> Any:
            return await ledger.execute(
                goal_run_id=goal_run_id or evidence.execution_id,
                work_item_id=evidence.execution_id,
                operation_key=operation_key,
                operation_type="CANONICAL_GIT_PROMOTION",
                target_ref=f"{evidence.repo_identity}:{evidence.target_branch}",
                request={
                    "execution_id": evidence.execution_id,
                    "repo_identity": evidence.repo_identity,
                    "source_commit_sha": evidence.execution_commit_sha,
                    "base_sha": evidence.base_sha,
                    "target_branch": evidence.target_branch,
                    "canonical_before_sha": evidence.canonical_before_sha,
                    "canonical_after_sha": evidence.canonical_after_sha,
                    "promotion_mode": evidence.promotion_mode,
                    "verification_evidence": evidence.verification_evidence,
                    "verification_digest": verification_digest,
                    "timestamp": evidence.timestamp,
                    "idempotency_key": operation_key,
                },
                provider=lambda: evidence.to_dict(),
                capability="idempotency_key",
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(record())
            return

        # The promotion API is intentionally synchronous because Git CAS and
        # the binding update are synchronous authorities.  When a caller
        # invokes it from an async MCP handler, run only the async ledger
        # bridge on a short-lived non-default thread; never nest ``asyncio.run``
        # on the handler loop and never leave its default executor behind.
        result: list[Any] = []
        failure: list[BaseException] = []

        def bridge() -> None:
            try:
                result.append(asyncio.run(record()))
            except BaseException as exc:  # pragma: no cover - propagated below
                failure.append(exc)

        thread = threading.Thread(target=bridge, name="veya-promotion-ledger", daemon=True)
        thread.start()
        thread.join()
        if failure:
            raise failure[0]


__all__ = [
    "CanonicalPromotionService",
    "ExecutionRepoWriteLease",
    "ExecutionWorktreeBinding",
    "ExecutionWorktreeError",
    "ExecutionWorktreeRegistry",
    "ExecutionWorktreeState",
    "PromotionConflict",
    "PromotionEvidence",
]
