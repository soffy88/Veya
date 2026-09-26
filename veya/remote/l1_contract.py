"""Truthful L1 task contracts, effect receipts, and finalization.

Workers may provide narration and telemetry, but this module decides whether a
coding execution actually produced the effect its contract requires.  It is a
finalization boundary, not a planner or a second execution state machine.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .execution_worktree import (
    CanonicalPromotionService,
    ExecutionWorktreeError,
    ExecutionWorktreeRegistry,
    ExecutionWorktreeState,
)


class TaskKind(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    TEST = "TEST"
    BUILD = "BUILD"
    REVIEW = "REVIEW"


class EffectRequirement(StrEnum):
    NONE = "NONE"
    READ_ONLY = "READ_ONLY"
    FILES_CHANGED = "FILES_CHANGED"
    COMMAND_EXECUTED = "COMMAND_EXECUTED"
    TEST_EXECUTED = "TEST_EXECUTED"
    BUILD_EXECUTED = "BUILD_EXECUTED"


class CommitRequirement(StrEnum):
    NONE = "NONE"
    REQUIRED = "REQUIRED"


class PromotionPolicy(StrEnum):
    NONE = "NONE"
    MANUAL = "MANUAL"
    AUTO_AFTER_VERIFY = "AUTO_AFTER_VERIFY"


@dataclass
class L1TaskContract:
    task_kind: str = str(TaskKind.READ)
    effect_requirement: str = str(EffectRequirement.NONE)
    verification_requirement: str = "NONE"
    commit_requirement: str = str(CommitRequirement.NONE)
    promotion_policy: str = str(PromotionPolicy.NONE)
    allowed_files: list[str] = field(default_factory=list)
    allowed_roots: list[str] = field(default_factory=list)
    allow_noop: bool = False
    verification_command: str | None = None

    @classmethod
    def write(
        cls,
        *,
        promotion_policy: PromotionPolicy = PromotionPolicy.MANUAL,
        verification_command: str | None = None,
        allowed_files: list[str] | None = None,
    ) -> L1TaskContract:
        return cls(
            task_kind=str(TaskKind.WRITE),
            effect_requirement=str(EffectRequirement.FILES_CHANGED),
            verification_requirement="REQUIRED",
            commit_requirement=str(CommitRequirement.REQUIRED),
            promotion_policy=str(promotion_policy),
            verification_command=verification_command,
            allowed_files=list(allowed_files or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> L1TaskContract:
        known = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in (payload or {}).items() if key in known})


@dataclass
class VerificationEvidence:
    command: str
    cwd: str
    exit_code: int | None
    stdout_digest: str
    stderr_digest: str
    duration: float
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EffectReceipt:
    execution_id: str
    worker_runtime_id: str | None
    worker_type: str
    repo_identity: str
    worktree_path: str
    task_kind: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    shell_calls: list[dict[str, Any]] = field(default_factory=list)
    file_reads: list[str] = field(default_factory=list)
    file_writes: list[str] = field(default_factory=list)
    file_patches: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    diff_digest: str = ""
    tests_run: list[dict[str, Any]] = field(default_factory=list)
    builds_run: list[dict[str, Any]] = field(default_factory=list)
    base_sha: str = ""
    head_sha_before: str = ""
    head_sha_after: str = ""
    verification: dict[str, Any] | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkerResult:
    textual_summary: str
    process_exit_code: int | None
    effect_receipt: EffectReceipt


@dataclass
class FinalizationResult:
    status: str
    failure_class: str | None
    receipt: EffectReceipt
    commit_sha: str | None = None
    promotion_status: str = "NONE"
    verification: VerificationEvidence | None = None
    commit_evidence: dict[str, Any] | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"PROMOTABLE", "PROMOTED", "COMPLETED"}

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["receipt"] = self.receipt.to_dict()
        payload["verification"] = self.verification.to_dict() if self.verification else None
        return payload


def _run(root: Path, args: list[str], *, check: bool = True) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise ExecutionWorktreeError("GIT_FAILED", (proc.stderr or proc.stdout).strip())
    return proc.returncode, proc.stdout, proc.stderr


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _status_files(root: Path) -> list[str]:
    _, output, _ = _run(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    files: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip() if len(line) >= 4 else line.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return sorted(set(files))


def _diff_digest(root: Path, files: list[str]) -> str:
    _, diff, _ = _run(root, ["diff", "HEAD", "--", *files], check=False)
    parts = [diff]
    for relative in files:
        path = root / relative
        if path.is_file() and relative not in diff:
            parts.append(relative)
            parts.append(path.read_bytes().decode("utf-8", "replace"))
    return _digest("\0".join(parts))


def _within_allowed(path: str, contract: L1TaskContract, root: Path) -> bool:
    if contract.allowed_files and path not in set(contract.allowed_files):
        return False
    if contract.allowed_roots:
        candidate = (root / path).resolve()
        roots = [(root / item).resolve() for item in contract.allowed_roots]
        if not any(candidate == allowed or allowed in candidate.parents for allowed in roots):
            return False
    return True


class L1ExecutionFinalizer:
    """The single effect/verification/commit boundary for L1 executions."""

    def __init__(
        self, registry: ExecutionWorktreeRegistry, side_effect_ledger: Any | None = None
    ) -> None:
        self.registry = registry
        self.side_effect_ledger = side_effect_ledger

    def finalize(
        self,
        *,
        execution_id: str,
        repo: str | Path,
        task_contract: L1TaskContract,
        worker_result: WorkerResult,
        verification_command: str | None = None,
    ) -> FinalizationResult:
        binding = self.registry.resolve(execution_id, repo)
        root = Path(binding.worktree_path)
        before = _run(root, ["rev-parse", "HEAD"])[1].strip()
        changed = _status_files(root)
        receipt = worker_result.effect_receipt
        receipt.execution_id = execution_id
        receipt.repo_identity = binding.repo_identity
        receipt.worktree_path = str(root)
        receipt.task_kind = task_contract.task_kind
        receipt.base_sha = binding.base_sha
        receipt.head_sha_before = before
        receipt.changed_files = changed
        receipt.diff_digest = _diff_digest(root, changed)

        if binding.commit_sha and binding.state in {
            str(ExecutionWorktreeState.PROMOTABLE),
            str(ExecutionWorktreeState.PROMOTED),
        }:
            receipt.head_sha_after = before
            return FinalizationResult(
                status=(
                    "PROMOTED"
                    if binding.state == str(ExecutionWorktreeState.PROMOTED)
                    else "PROMOTABLE"
                ),
                failure_class=None,
                receipt=receipt,
                commit_sha=binding.commit_sha,
                promotion_status=(
                    "PROMOTED" if binding.state == str(ExecutionWorktreeState.PROMOTED) else "NONE"
                ),
                message="finalization already committed",
            )

        if worker_result.process_exit_code not in (0, None):
            return self._failed(
                receipt, "PROCESS_FAILED", "worker process did not exit successfully"
            )
        if task_contract.allowed_files or task_contract.allowed_roots:
            unexpected = [
                path for path in changed if not _within_allowed(path, task_contract, root)
            ]
            if unexpected:
                return self._failed(receipt, "EFFECT_MISMATCH", f"unexpected files: {unexpected}")
        if (
            task_contract.effect_requirement == str(EffectRequirement.FILES_CHANGED)
            and not changed
            and not task_contract.allow_noop
        ):
            return self._failed(receipt, "NO_EFFECT", "write task produced no changed files")
        if (
            task_contract.effect_requirement == str(EffectRequirement.TEST_EXECUTED)
            and not receipt.tests_run
        ):
            return self._failed(receipt, "VERIFICATION_MISSING", "no test receipt")
        if (
            task_contract.effect_requirement == str(EffectRequirement.BUILD_EXECUTED)
            and not receipt.builds_run
        ):
            return self._failed(receipt, "VERIFICATION_MISSING", "no build receipt")

        verification: VerificationEvidence | None = None
        if task_contract.verification_requirement == "REQUIRED":
            command = verification_command or task_contract.verification_command
            if not command:
                return self._failed(
                    receipt, "VERIFICATION_MISSING", "verification command is required"
                )
            started = time.monotonic()
            proc = subprocess.run(
                command, shell=True, cwd=str(root), capture_output=True, text=True
            )
            verification = VerificationEvidence(
                command=command,
                cwd=str(root),
                exit_code=proc.returncode,
                stdout_digest=_digest(proc.stdout),
                stderr_digest=_digest(proc.stderr),
                duration=time.monotonic() - started,
                stdout_tail=proc.stdout[-4000:],
                stderr_tail=proc.stderr[-4000:],
            )
            receipt.verification = verification.to_dict()
            if not verification.passed:
                result = self._failed(receipt, "VERIFICATION_FAILED", "verification command failed")
                result.verification = verification
                return result

        commit_sha: str | None = None
        commit_evidence: dict[str, Any] | None = None
        if task_contract.commit_requirement == str(CommitRequirement.REQUIRED):
            if not changed:
                return self._failed(receipt, "COMMIT_MISSING", "no files available for task commit")
            _run(root, ["add", "--", *changed])
            staged = _run(root, ["diff", "--cached", "--name-only"])[1].splitlines()
            if sorted(staged) != sorted(changed):
                return self._failed(
                    receipt, "EFFECT_MISMATCH", "staged files differ from validated diff"
                )
            marker = f"Veya-Execution: {execution_id}"
            existing_message = _run(root, ["log", "-1", "--format=%B"])[1]
            if before != binding.base_sha and marker in existing_message:
                commit_sha = before
            else:
                _run(root, ["commit", "-m", f"veya: finalize {execution_id}", "-m", marker])
                commit_sha = _run(root, ["rev-parse", "HEAD"])[1].strip()
            parent_sha = _run(root, ["rev-parse", f"{commit_sha}^"])[1].strip()
            if parent_sha != binding.base_sha and before == binding.base_sha:
                return self._failed(
                    receipt, "COMMIT_LINEAGE_MISMATCH", "commit parent is not base_sha"
                )
            commit_evidence = {
                "base_sha": binding.base_sha,
                "commit_sha": commit_sha,
                "parent_sha": parent_sha,
                "files": sorted(changed),
                "message": existing_message.strip() if before != binding.base_sha else marker,
            }
            binding.commit_sha = commit_sha
            binding.current_head_sha = commit_sha

        receipt.head_sha_after = _run(root, ["rev-parse", "HEAD"])[1].strip()
        self.registry.mark(binding, ExecutionWorktreeState.PROMOTABLE)
        result = FinalizationResult(
            status="PROMOTABLE",
            failure_class=None,
            receipt=receipt,
            commit_sha=commit_sha,
            verification=verification,
            commit_evidence=commit_evidence,
            message="effect verified and finalized",
        )
        if task_contract.promotion_policy == str(PromotionPolicy.AUTO_AFTER_VERIFY):
            if not commit_sha:
                return self._failed(receipt, "COMMIT_MISSING", "auto promotion requires commit")
            evidence = CanonicalPromotionService(self.registry, self.side_effect_ledger).promote(
                execution_id,
                repo,
                expected_base_sha=binding.base_sha,
                verification_evidence=verification.to_dict() if verification else {},
            )
            result.status = "PROMOTED"
            result.promotion_status = "PROMOTED"
            result.receipt.verification = {
                **(result.receipt.verification or {}),
                "promotion": evidence.to_dict(),
            }
        return result

    @staticmethod
    def _failed(receipt: EffectReceipt, failure_class: str, message: str) -> FinalizationResult:
        return FinalizationResult(
            status="FAILED",
            failure_class=failure_class,
            receipt=receipt,
            message=message,
        )


__all__ = [
    "CommitRequirement",
    "EffectReceipt",
    "EffectRequirement",
    "FinalizationResult",
    "L1ExecutionFinalizer",
    "L1TaskContract",
    "PromotionPolicy",
    "TaskKind",
    "VerificationEvidence",
    "WorkerResult",
]
