"""Data contracts for the local coding product surface.

These are product-layer records.  They are intentionally independent from
``runtime.execution.models`` so PR-01..04 cannot accidentally change the
Execution Runtime ABI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


def utc_now() -> datetime:
    return datetime.now(UTC)


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    return value


@dataclass
class CodingWorkspace:
    id: str
    owner_user_id: str
    name: str
    root_path: str
    repo_url: str | None
    provider: Literal["local", "github", "gitlab", "unknown"]
    default_branch: str | None
    current_branch: str | None
    language_hints: list[str] = field(default_factory=list)
    package_manager: str | None = None
    test_commands: list[str] = field(default_factory=list)
    lint_commands: list[str] = field(default_factory=list)
    typecheck_commands: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    sandbox_profile_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class CodingTask:
    id: str
    workspace_id: str
    goal_run_id: str | None
    source: Literal["chat", "cli", "github_issue", "github_pr", "manual"]
    objective: str
    issue_ref: str | None = None
    pr_ref: str | None = None
    status: Literal[
        "draft",
        "planned",
        "running",
        "waiting_approval",
        "completed",
        "partial_completed",
        "failed",
        "cancelled",
    ] = "draft"
    branch_name: str | None = None
    worktree_path: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class CodingRun:
    id: str
    task_id: str
    goal_run_id: str
    sandbox_id: str | None
    worktree_path: str
    started_at: datetime
    completed_at: datetime | None = None
    status: str = "running"
    finalization_status: str | None = None
    artifacts: list[str] = field(default_factory=list)
    verification_report_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class CommandResult:
    command: str
    argv: list[str]
    cwd: str
    profile: str
    status: Literal["passed", "failed", "timeout", "denied", "approval_required"]
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    timed_out: bool = False
    artifact_path: str | None = None
    requires_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class VerificationReport:
    id: str
    task_id: str
    run_id: str
    commands_run: list[CommandResult] = field(default_factory=list)
    sensor_results: list[dict[str, Any]] = field(default_factory=list)
    tests_passed: bool | None = None
    lint_passed: bool | None = None
    typecheck_passed: bool | None = None
    build_passed: bool | None = None
    acceptance_passed: bool = False
    failed_checks: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class PatchArtifact:
    id: str
    task_id: str
    run_id: str
    kind: Literal["diff", "commit", "branch", "pr_draft", "review_comments", "test_report"]
    path: str | None = None
    git_sha: str | None = None
    diff_summary: str | None = None
    verified: bool = False
    verification_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass
class ToolResult:
    """Stable product-tool envelope used by PR-04 coding functions."""

    status: Literal["ok", "partial", "failed"]
    evidence: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    command_results: list[CommandResult] = field(default_factory=list)
    side_effect: bool = False
    requires_approval: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["command_results"] = [item.to_dict() for item in self.command_results]
        return _serialize(value)
