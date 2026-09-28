"""MCP tool -> canonical Veya runtime / direct-host adapter (spec §2).

Two execution families, one workspace contract:

* **Fast path** (``workspace.*`` / ``file.read|search`` / ``git.*`` /
  ``artifact.*`` / ``file.write|patch``) executes a primitive synchronously and
  returns the result immediately. It never starts Hicode, MasterAgent, an agent
  loop or any LLM provider (``DIRECT_EXECUTION_USES_LLM=NO``).
* **Command path** (``shell.exec`` / ``test.run`` / ``build.run``) runs a real
  host subprocess with streaming stdout/stderr under the durable
  :class:`~veya.remote.execution.DurableJobManager`. A command that finishes
  inside the direct sync window returns inline; a longer one returns an
  ``execution_id`` immediately and is observable via ``process.status``.

LLM-backed coding (``hicode.execute``) remains a separate durable job on the
same substrate. The adapter never runs a keyword/semantic router.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from runtime.coding.command_runner import CommandPolicyError
from runtime.coding.worktree import WorktreeError, WorktreeManager
from runtime.execution.side_effects import SideEffectLedger
from veya.obase.async_utils import run_sync_in_daemon_thread
from veya.remote.skills import SkillPermission
from veya.supervision.task_memory import TaskMemory

from .action_gateway import ActionCategory, ActionGateway, parse_systemctl_command
from .direct_exec import (
    DEFAULT_DIRECT_TIMEOUT_S,
    DirectApprovalRequired,
    DirectDenied,
    direct_sync_window_s,
    run_direct_command,
    terminate_process_group,
    terminate_process_group_id,
)
from .execution import (
    DurableJobManager,
    ExecutionBlocked,
    ExecutionError,
    ExecutionPhase,
    ExecutionStatus,
    ExecutionStore,
    ExecutionType,
    ProgressReporter,
    use_reporter,
)
from .execution_context import (
    ExecutionCapabilityContext,
    ExecutionContextBuilder,
    context_budget,
    context_hash,
    render_execution_context,
    shared_capability_registry,
    shared_skill_registry,
)
from .execution_worktree import ExecutionWorktreeRegistry
from .executor_health import (
    ExecutorFailureClass,
    ExecutorHealthRegistry,
    classify_executor_failure,
    normalize_executor_name,
    resolve_executor,
)
from .executor_registry import ExecutorRuntimeIdentity, get_executor_registry
from .l1_contract import (
    EffectReceipt,
    L1ExecutionFinalizer,
    L1TaskContract,
    TaskKind,
    WorkerResult,
)
from .metrics import LatencyMetrics
from .models import (
    EffectClass,
    RemoteCallResult,
    RemoteErrorCode,
    RemoteSession,
    ToolBinding,
)
from .worker_runtime import capabilities_for
from .workspace_binding import (
    RepoResolution,
    WorkspaceBinding,
    WorkspaceBindingError,
    canonical,
    git_main_repo_root,
    git_repo_identity,
    git_repo_root,
    is_git_worktree,
    resolve_repo_target,
    resolve_requested_workspace,
    verify_worktree_repo_identity,
)
from .workspace_policy import WorkspacePolicy, WorkspacePolicyError

VeyaExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]

_DEFAULT_OUTPUT_LIMIT = 200_000
_TASK_OUTPUTS = ".veya/runs"

# P0-A: sync primitives. Never a job, never an LLM.
_FAST_READ_TOOLS = frozenset(
    {
        "workspace.list",
        "workspace.info",
        "file.read",
        "file.search",
        "artifact.list",
        "artifact.read",
        "runtime.profile",
        "runtime.capabilities",
        "runtime.probe",
    }
)
_FAST_GIT_TOOLS = frozenset({"git.status", "git.diff", "git.log", "git.promote"})
# P0-B: long-command tools -> DirectJobManager with a fast sync window.
_COMMAND_TOOLS = frozenset({"shell.exec", "test.run", "build.run"})


def _canonical_project_root(path: str | Path) -> str:
    """Return the canonical *main* repo root for any path, including linked worktrees.

    For a linked worktree (``path/.git`` is a file pointing into
    ``<main>/.git/worktrees/<name>``), this returns the main repo root, not the
    worktree itself.  For a normal repo or non-repo path, it returns the repo root
    discovered by ``git_repo_root``.

    Used so that runtime-profile discovery always finds the canonical project
    venv (e.g. ``/data/soffy/projects/veya/venv``) even when the CWD is an
    isolated worktree that has no local venv.
    """
    p = Path(path).expanduser().resolve()
    main = git_main_repo_root(p)
    if main is not None:
        return str(main)
    root = git_repo_root(p)
    return str(root) if root is not None else str(p)


def resolve_execution_target(
    workspace: str | Path,
    workspace_path: str | Path | None = None,
    requested_execution_target: str = "",
) -> str:
    """Canonical execution-target resolver (P0-B).

    Rules (in priority order):
    1. Explicit ``EXISTING_WORKTREE`` / ``CANONICAL_WORKTREE`` / ``HOST`` → honour.
    2. ``workspace`` itself is a linked worktree (``.git`` is a file pointing into
       ``<main>/.git/worktrees/*``) → ``EXISTING_WORKTREE``.
    3. ``workspace_path`` resolves to a directory inside ``.veya/worktrees/``
       of some parent repo → ``EXISTING_WORKTREE``.
    4. Otherwise → ``NEW_ISOLATED_WORKTREE``.

    This is the *only* place that maps a workspace/path to an execution target.
    All callers (shell.exec, test.run, build.run, file.write, file.patch) must
    use this function instead of guessing individually.
    """
    explicit = str(requested_execution_target or "").strip().upper()
    if explicit in ("CANONICAL_WORKTREE", "HOST"):
        return explicit
    if explicit == "EXISTING_WORKTREE":
        return "EXISTING_WORKTREE"
    if explicit == "EXECUTION_WORKTREE":
        return "EXECUTION_WORKTREE"
    if explicit == "NEW_ISOLATED_WORKTREE":
        return "NEW_ISOLATED_WORKTREE"

    ws = Path(workspace).expanduser().resolve()

    # Rule 2: workspace itself is a linked worktree
    if is_git_worktree(ws):
        return "EXISTING_WORKTREE"

    # Rule 3: workspace_path points inside a .veya/worktrees/* subtree
    if workspace_path:
        wp = Path(workspace_path).expanduser().resolve(strict=False)
        for anc in (wp, *wp.parents):
            if anc.parent.name == "worktrees" and anc.parent.parent.name == ".veya":
                return "EXISTING_WORKTREE"

    return "NEW_ISOLATED_WORKTREE"


def find_existing_worktree_root(target_path: str | Path) -> Path | None:
    """Return the existing linked-worktree root containing ``target_path``.

    Case 1: the path itself is a linked worktree root (``.git`` is a file
    pointing into ``<main>/.git/worktrees/*``). Case 2: the path sits inside
    a canonical ``.veya/worktrees/*`` subtree (the ancestor worktree root is
    returned). Otherwise ``None`` — the caller keeps its default
    NEW/CANONICAL/HOST policy instead of guessing.

    Shared by every execution-target-aware entry point (shell.exec, test.run,
    build.run, file.write, file.patch) so an existing worktree is always a
    terminal target and never sprouts ``existing/.veya/worktrees/*`` nesting.
    """

    try:
        target_p = Path(target_path).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return None
    if is_git_worktree(target_p):
        return target_p
    for anc in (target_p, *target_p.parents):
        try:
            if anc.parent.name == "worktrees" and anc.parent.parent.name == ".veya":
                return anc
        except (OSError, ValueError):
            continue
    return None


_SERVICE_CONTROL_UNITS = frozenset(
    {
        "veya-remote-mcp.service",
        "veya-openai-tunnel.service",
    }
)
_SERVICE_CONTROL_SELF_UNITS = frozenset(
    {
        "veya-remote-mcp.service",
    }
)
_SERVICE_CONTROL_UNIT_ACTIONS = frozenset(
    {
        "restart",
        "start",
        "stop",
        "is-active",
        "status",
    }
)
_SERVICE_CONTROL_GLOBAL_ACTIONS = frozenset({"daemon-reload"})


def _parse_service_control_command(command: str) -> tuple[str, str] | None:
    parsed = parse_systemctl_command(command)
    if parsed is None or parsed.scope != "user" or parsed.options:
        return None
    if parsed.action in _SERVICE_CONTROL_GLOBAL_ACTIONS and not parsed.unit:
        return parsed.action, ""
    if parsed.action in _SERVICE_CONTROL_UNIT_ACTIONS and parsed.unit in _SERVICE_CONTROL_UNITS:
        return parsed.action, parsed.unit
    return None


def _allowed_service_control_command(command: str) -> bool:
    return _parse_service_control_command(command) is not None


# L1 direct worker registry. Each worker keeps its own real runtime/model
# semantics; none of these is a wrapper around Hicode.
_WORKER_TYPES = {
    "antigravity": "ANTIGRAVITY",
    "opencode": "OPENCODE",
    "codex": "CODEX",
    "hicode": "HICODE",
    "pi": "PI",
    "grok": "GROK",
    "dsh": "DSH",
}
# Runtime blockers are evidence-driven and temporary. Never keep stale provider
# outage/quota snapshots after a worker has been re-qualified.
_WORKER_BLOCKERS: dict[str, str] = {}
# Compatibility projection for older callers.  It is intentionally derived
# from ExecutorRegistry and is never consulted as an authority.
_CLI_WORKERS = {
    worker: {
        "provider": get_executor_registry().identity(worker).provider or "unknown",
        "model": get_executor_registry().identity(worker).model or "unknown",
    }
    for worker in _WORKER_TYPES
    if worker != "hicode"
}
_TIMEOUT_SEPARATED_CLI_WORKERS = frozenset({"pi", "grok", "codex", "antigravity", "opencode"})
_DEFAULT_CLI_TIMEOUT_S = 600.0
_DSH_INACTIVITY_TIMEOUT_S = 120.0


def _cli_worker_timeout_budgets(worker: str, requested_timeout_s: float) -> tuple[float, float]:
    """Return ``(inactivity_timeout, hard_max_runtime)`` for a CLI worker.

    Pi and Grok can spend several minutes in provider-side tool activity before
    their headless CLI emits a final response. Their old single wall-clock
    timeout killed that active work. DSH stays bounded at its existing hard
    limit, but a quiet runaway is stopped earlier.
    """

    requested = max(0.0, float(requested_timeout_s))
    if worker == "dsh":
        return min(_DSH_INACTIVITY_TIMEOUT_S, requested), requested
    if worker not in _TIMEOUT_SEPARATED_CLI_WORKERS:
        return requested, requested
    if 0.0 < requested < _DEFAULT_CLI_TIMEOUT_S:
        return min(requested, 300.0), requested
    return max(requested, 900.0), max(requested, 1800.0)


class _CLIWorkerTimeout(TimeoutError):
    def __init__(self, *, kind: str, timeout_s: float, hard_max_s: float) -> None:
        self.kind = kind
        self.timeout_s = timeout_s
        self.hard_max_s = hard_max_s
        super().__init__(f"{kind} timeout: inactivity={timeout_s:.1f}s hard_max={hard_max_s:.1f}s")


async def _wait_for_cli_process(
    process: asyncio.subprocess.Process,
    tasks: set[asyncio.Task[Any]],
    *,
    activity_event: asyncio.Event,
    last_activity: list[float],
    inactivity_timeout_s: float,
    hard_max_runtime_s: float,
) -> None:
    """Wait for a CLI process while separating inactivity from total runtime."""

    started = time.monotonic()
    hard_deadline = started + hard_max_runtime_s
    pending = set(tasks)
    try:
        while pending:
            now = time.monotonic()
            hard_remaining = hard_deadline - now
            inactivity_remaining = (last_activity[0] + inactivity_timeout_s) - now
            if hard_remaining <= 0:
                raise _CLIWorkerTimeout(
                    kind="HARD_MAX_RUNTIME",
                    timeout_s=inactivity_timeout_s,
                    hard_max_s=hard_max_runtime_s,
                )
            if inactivity_remaining <= 0:
                raise _CLIWorkerTimeout(
                    kind="INACTIVITY_TIMEOUT",
                    timeout_s=inactivity_timeout_s,
                    hard_max_s=hard_max_runtime_s,
                )

            activity_wait = asyncio.create_task(activity_event.wait())
            try:
                done, pending = await asyncio.wait(
                    pending | {activity_wait},
                    timeout=min(hard_remaining, inactivity_remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not activity_wait.done():
                    activity_wait.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await activity_wait
                    pending.discard(activity_wait)

            if not done:
                kind = (
                    "HARD_MAX_RUNTIME"
                    if hard_remaining <= inactivity_remaining
                    else "INACTIVITY_TIMEOUT"
                )
                raise _CLIWorkerTimeout(
                    kind=kind,
                    timeout_s=inactivity_timeout_s,
                    hard_max_s=hard_max_runtime_s,
                )

            if activity_wait in done:
                activity_event.clear()
                done.remove(activity_wait)
            for completed in done:
                completed.result()
    finally:
        for pending_task in pending:
            pending_task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def _stage_dependency_artifacts(
    worktree: str, repo_root: str, artifacts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Copy mission-projected dependency artifacts into one child worktree."""

    artifact_root = (Path(repo_root).resolve() / ".veya" / "artifacts").resolve()
    target_root = Path(worktree).resolve()
    staged: list[dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise ExecutionBlocked(
                "INVALID_DEPENDENCY_ARTIFACT", "artifact descriptor is not an object"
            )
        source_value = item.get("materialized_path")
        relative_value = item.get("relative_path")
        if not source_value or not relative_value:
            raise ExecutionBlocked("INVALID_DEPENDENCY_ARTIFACT", "artifact path is incomplete")
        source = Path(str(source_value)).resolve()
        relative = Path(str(relative_value))
        subtask_id = Path(str(item.get("dependency_subtask_id") or ""))
        if (
            not subtask_id.name
            or subtask_id.is_absolute()
            or ".." in subtask_id.parts
            or len(subtask_id.parts) != 1
        ):
            raise ExecutionBlocked(
                "INVALID_DEPENDENCY_ARTIFACT", "dependency subtask id escapes staging root"
            )
        if source != artifact_root and artifact_root not in source.parents:
            raise ExecutionBlocked(
                "INVALID_DEPENDENCY_ARTIFACT", "artifact source is outside mission artifacts"
            )
        if relative.is_absolute() or ".." in relative.parts:
            raise ExecutionBlocked(
                "INVALID_DEPENDENCY_ARTIFACT", "artifact relative path escapes worktree"
            )
        target = (target_root / ".veya" / "dependencies" / subtask_id.name / relative).resolve()
        if target != target_root and target_root not in target.parents:
            raise ExecutionBlocked(
                "INVALID_DEPENDENCY_ARTIFACT", "artifact target escapes worktree"
            )
        if not source.is_file():
            raise ExecutionBlocked(
                "INVALID_DEPENDENCY_ARTIFACT", "materialized artifact is missing"
            )
        data = source.read_bytes()
        expected = str(item.get("hash") or "")
        digest = hashlib.sha256(data).hexdigest()
        if expected and digest != expected:
            raise ExecutionBlocked("INVALID_DEPENDENCY_ARTIFACT", "artifact hash mismatch")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ExecutionBlocked("INVALID_DEPENDENCY_ARTIFACT", "staged artifact hash mismatch")
        staged.append(
            {
                "source_execution_id": item.get("execution_id"),
                "source_subtask_id": item.get("dependency_subtask_id"),
                "source_worker": item.get("source_worker"),
                "source_path": item.get("source_path") or str(source),
                "materialized_path": str(source),
                "staged_path": str(target),
                "relative_path": str(relative),
                "sha256": digest,
                "size": len(data),
            }
        )
    return staged


class RemoteToolAdapterError(Exception):
    def __init__(self, code: RemoteErrorCode | str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = message


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


_STR = {"type": "string"}

EXECUTION_TARGETS = (
    "NEW_ISOLATED_WORKTREE",
    "EXECUTION_WORKTREE",
    "EXISTING_WORKTREE",
    "CANONICAL_WORKTREE",
    "HOST",
)
_EXECUTION_TARGET_SCHEMA = {"type": "string", "enum": list(EXECUTION_TARGETS)}

# Every repo-sensitive tool accepts this one canonical selector.  ``workspace``
# and legacy ``path`` remain accepted by the adapter for compatibility, but
# clients and the outward MCP contract use ``workspace_path``.
_WORKSPACE_PATH = {
    "type": "string",
    "description": "Canonical path selector, relative to the bound root or an absolute contained path.",
}


# ── the first-version remote tool surface (spec §2) ────────────────────
BINDINGS: tuple[ToolBinding, ...] = (
    ToolBinding(
        "workspace.list",
        "list_files",
        EffectClass.READ,
        "List files in the bound workspace (no VCS or dependency noise).",
        _obj({"path": _STR}),
    ),
    ToolBinding(
        "workspace.info",
        "coding_workspace_detect",
        EffectClass.READ,
        "Inspect the bound workspace: languages, package managers, known test/lint/build commands.",
        _obj({"path": _STR}),
    ),
    ToolBinding(
        "file.read",
        "read_hashline",
        EffectClass.READ,
        "Read a file with per-line LINE#hash tags (used by file.patch for stale-safe edits).",
        _obj({"path": _STR, "max_lines": {"type": "integer"}}, ["path"]),
    ),
    ToolBinding(
        "file.search",
        "grep",
        EffectClass.READ,
        "Search the workspace with ripgrep. Returns path:line matches.",
        _obj({"pattern": _STR, "glob": _STR, "path": _STR}, ["pattern"]),
    ),
    ToolBinding(
        "file.write",
        "write_file",
        EffectClass.WRITE,
        "Write text content to a file inside the isolated session worktree or canonical workspace.",
        _obj(
            {
                "path": _STR,
                "content": _STR,
                "overwrite": {"type": "boolean"},
                "execution_target": _EXECUTION_TARGET_SCHEMA,
                "target_scope": _STR,
                "allow_canonical": {"type": "boolean"},
            },
            ["path", "content"],
        ),
    ),
    ToolBinding(
        "file.patch",
        "edit_hashline",
        EffectClass.WRITE,
        "Replace a LINE#hash span in a file. Read the file first to obtain the tags.",
        _obj(
            {
                "path": _STR,
                "start_tag": _STR,
                "new_text": _STR,
                "end_tag": _STR,
                "execution_target": _EXECUTION_TARGET_SCHEMA,
                "target_scope": _STR,
                "allow_canonical": {"type": "boolean"},
            },
            ["path", "start_tag", "new_text"],
        ),
    ),
    ToolBinding(
        "approval.request",
        None,
        EffectClass.READ,
        "Create a server-issued approval request bound to one exact operation.",
        _obj(
            {
                "capability_id": _STR,
                "operation": _STR,
                "risk_class": _STR,
                "ttl_s": {"type": "number"},
            },
            ["capability_id", "operation", "risk_class"],
        ),
    ),
    ToolBinding(
        "approval.status",
        None,
        EffectClass.READ,
        "Inspect a server-issued approval request owned by this session principal.",
        _obj({"approval_id": _STR}, ["approval_id"]),
    ),
    ToolBinding(
        "approval.consume",
        None,
        EffectClass.WRITE,
        "Record the human decision for an approval request; execution consumes it once.",
        _obj({"approval_id": _STR, "decision": _STR}, ["approval_id", "decision"]),
    ),
    ToolBinding(
        "shell.exec",
        "coding_run_command",
        EffectClass.WRITE,
        "Run one argv command inside the isolated session worktree sandbox.",
        _obj(
            {
                "command": _STR,
                "workspace": _STR,
                "path": _STR,
                "timeout_s": {"type": "number"},
                "profile": _STR,
                "network": _STR,
                "approved": {"type": "boolean"},
                "wait": {"type": "boolean"},
                "execution_target": _EXECUTION_TARGET_SCHEMA,
                "execution_domain": {
                    "type": "string",
                    "enum": ["L0_WORKSPACE_FULL", "L0_ISOLATED", "L0_HOST"],
                },
                "runtime_profile": {
                    "type": "string",
                    "description": "Runtime profile selector (default auto: canonical "
                    "project runtime is discovered from the main repo root).",
                },
            },
            ["command"],
        ),
        long_running=True,
        needs_shell=True,
    ),
    ToolBinding(
        "process.status",
        None,
        EffectClass.READ,
        "Status of a previously started long-running remote execution.",
        _obj({"execution_id": _STR}, ["execution_id"]),
    ),
    ToolBinding(
        "process.cancel",
        None,
        EffectClass.WRITE,
        "Cancel a long-running remote execution. The durable job is interrupted explicitly.",
        _obj({"execution_id": _STR}, ["execution_id"]),
        needs_shell=True,
    ),
    ToolBinding(
        "worker.dispatch",
        None,
        EffectClass.WRITE,
        "Dispatch independent tasks to L1 direct workers (parallel). Mechanical aggregation only: "
        "no planning, routing, ranking, JEV or Fan-In — those belong to L2.",
        _obj(
            {
                "tasks": {
                    "type": "array",
                    "items": _obj(
                        {
                            "worker": _STR,
                            "task": _STR,
                            "timeout_sec": {"type": "integer"},
                            "task_kind": {
                                "type": "string",
                                "enum": ["READ", "WRITE", "TEST", "BUILD", "REVIEW"],
                            },
                            "effect_requirement": _STR,
                            "verification_requirement": _STR,
                            "commit_requirement": _STR,
                            "promotion_policy": _STR,
                            "allowed_files": {"type": "array", "items": _STR},
                            "allowed_roots": {"type": "array", "items": _STR},
                            "verification_command": _STR,
                        },
                        ["worker", "task"],
                    ),
                },
                "fail_fast": {"type": "boolean"},
                "workspace": _STR,
                "path": _STR,
            },
            ["tasks"],
        ),
        needs_shell=True,
    ),
    ToolBinding(
        "git.status",
        "coding_worktree_status",
        EffectClass.READ,
        "Branch, cleanliness and changed files of the session worktree.",
        _obj({"workspace": _STR, "path": _STR}),
        needs_git=True,
    ),
    ToolBinding(
        "git.diff",
        "coding_diff",
        EffectClass.READ,
        "Unified diff of the session worktree against HEAD.",
        _obj({"workspace": _STR, "path": _STR}),
        needs_git=True,
    ),
    ToolBinding(
        "git.log",
        "coding_run_command",
        EffectClass.READ,
        "Recent commit history of the session worktree.",
        _obj({"limit": {"type": "integer"}, "workspace": _STR, "path": _STR}),
        needs_git=True,
    ),
    ToolBinding(
        "git.promote",
        "git_promote",
        EffectClass.WRITE,
        "Promote a finalized execution commit to its canonical branch through the canonical promotion service.",
        _obj(
            {
                "execution_id": _STR,
                "repo_identity": _STR,
                "expected_base_sha": _STR,
            },
            ["execution_id"],
        ),
        needs_git=True,
    ),
    ToolBinding(
        "test.run",
        "coding_run_tests",
        EffectClass.WRITE,
        "Run the workspace test command in the session worktree and return evidence.",
        _obj(
            {
                "command": _STR,
                "workspace": _STR,
                "path": _STR,
                "timeout_s": {"type": "number"},
                "wait": {"type": "boolean"},
                "execution_target": _EXECUTION_TARGET_SCHEMA,
                "execution_domain": {
                    "type": "string",
                    "enum": ["L0_WORKSPACE_FULL", "L0_ISOLATED", "L0_HOST"],
                },
                "profile": _STR,
                "runtime_profile": {
                    "type": "string",
                    "description": "Runtime profile selector (default auto: canonical "
                    "project runtime is discovered from the main repo root).",
                },
            }
        ),
        long_running=True,
        needs_shell=True,
    ),
    ToolBinding(
        "build.run",
        "coding_build",
        EffectClass.WRITE,
        "Run the workspace build command in the session worktree and return evidence.",
        _obj(
            {
                "command": _STR,
                "workspace": _STR,
                "path": _STR,
                "timeout_s": {"type": "number"},
                "wait": {"type": "boolean"},
                "execution_target": _EXECUTION_TARGET_SCHEMA,
                "execution_domain": {
                    "type": "string",
                    "enum": ["L0_WORKSPACE_FULL", "L0_ISOLATED", "L0_HOST"],
                },
                "profile": _STR,
                "runtime_profile": {
                    "type": "string",
                    "description": "Runtime profile selector (default auto: canonical "
                    "project runtime is discovered from the main repo root).",
                },
            }
        ),
        long_running=True,
        needs_shell=True,
    ),
    ToolBinding(
        "runtime.profile",
        None,
        EffectClass.READ,
        "Get discovered workspace runtime profile (Python/Node/pytest/ruff/mypy/docker/service).",
        _obj({"workspace": _STR, "path": _STR, "force_refresh": {"type": "boolean"}}),
    ),
    ToolBinding(
        "runtime.capabilities",
        None,
        EffectClass.READ,
        "Get high-level summary of available tools/capabilities in the workspace.",
        _obj({"workspace": _STR, "path": _STR}),
    ),
    ToolBinding(
        "runtime.probe",
        None,
        EffectClass.READ,
        "Probe specific runtime binary/tool execution in the workspace context.",
        _obj(
            {
                "workspace": _STR,
                "path": _STR,
                "tool": _STR,
                "command": _STR,
            }
        ),
        needs_shell=True,
    ),
    ToolBinding(
        "artifact.list",
        "list_files",
        EffectClass.READ,
        "List task-scoped artifacts produced by finalization for this session.",
        _obj({}),
    ),
    ToolBinding(
        "artifact.read",
        "read_hashline",
        EffectClass.READ,
        "Read a task-scoped artifact by its path under the task output directory.",
        _obj({"path": _STR}, ["path"]),
    ),
    ToolBinding(
        "hicode.execute",
        "hicode_run",
        EffectClass.WRITE,
        "Delegate a real coding task to Veya's Hicode executor (reads code, edits, runs tests).",
        _obj(
            {
                "task": _STR,
                "workspace": _STR,
                "path": _STR,
                "max_steps": {"type": "integer"},
                "timeout_sec": {"type": "integer"},
                "execution_mode": {"type": "string", "enum": ["direct_hicode"]},
            },
            ["task"],
        ),
        long_running=True,
        needs_shell=True,
    ),
)

# High-level supervision surface (spec §8/§28): one Mission over the existing
# runtime. Same MCP server / session / auth / workspace policy / audit.
_SUPERVISION_BINDING_SPECS: tuple[tuple[str, str, EffectClass, str, bool], ...] = (
    (
        "veya.mission.create",
        "veya_mission_create",
        EffectClass.WRITE,
        "Create a Mission (external/internal/auto supervision).",
        False,
    ),
    (
        "veya.mission.inspect",
        "veya_mission_inspect",
        EffectClass.READ,
        "Inspect mission status, supervisor, lineage, latest report/review.",
        False,
    ),
    (
        "veya.mission.run",
        "veya_mission_run",
        EffectClass.WRITE,
        "Run one mission iteration via the canonical runtime and emit an ExecutionReport.",
        True,
    ),
    (
        "veya.mission.continue",
        "veya_mission_continue",
        EffectClass.WRITE,
        "Apply a supervisor review and retask (external reconnect entry).",
        False,
    ),
    (
        "veya.mission.cancel",
        "veya_mission_cancel",
        EffectClass.WRITE,
        "Cancel a mission without rewriting its records.",
        False,
    ),
    (
        "veya.report.latest",
        "veya_report_latest",
        EffectClass.READ,
        "Read the latest ExecutionReport (reviewer input).",
        False,
    ),
    (
        "veya.report.get",
        "veya_report_get",
        EffectClass.READ,
        "Read an ExecutionReport by iteration.",
        False,
    ),
    (
        "veya.review.apply",
        "veya_review_apply",
        EffectClass.WRITE,
        "Submit a SupervisorReview (ACCEPT/CONTINUE/REVISE/RETRY/ROLLBACK/ESCALATE/DONE).",
        False,
    ),
    (
        "veya.escalation.list",
        "veya_escalation_list",
        EffectClass.READ,
        "List owner-only escalation events for a mission.",
        False,
    ),
)


def _supervision_bindings() -> tuple[ToolBinding, ...]:
    """Bind the supervision surface using the canonical tool schemas.

    `server/supervision_tools.py` owns the schemas, so a remote client sees
    exactly the arguments the tool accepts (one source of truth — no hand-copied
    schema that can drift). `project_root` is injected from the bound workspace.
    """
    from server.supervision_tools import _TOOLS as _CANONICAL_TOOLS

    schemas = {func.__name__: schema for _n, _d, schema, func, _e in _CANONICAL_TOOLS}

    def remote_schema(veya_tool: str) -> dict[str, object]:
        schema = (
            json.loads(json.dumps(schemas[veya_tool])) if veya_tool in schemas else _obj({}, [])
        )
        properties = schema.setdefault("properties", {})
        if isinstance(properties, dict):
            properties["workspace"] = _STR
        return schema

    return tuple(
        ToolBinding(
            name,
            veya_tool,
            effect,
            description,
            remote_schema(veya_tool),
            long_running=long_running,
            needs_shell=long_running,
        )
        for name, veya_tool, effect, description, long_running in _SUPERVISION_BINDING_SPECS
    )


_AUTONOMOUS_BINDINGS: tuple[ToolBinding, ...] = (
    ToolBinding(
        "autonomous.status",
        None,
        EffectClass.READ,
        "Inspect mission autonomous state (spec §35).",
        _obj({"mission": _STR}, ["mission"]),
    ),
    ToolBinding(
        "autonomous.observations",
        None,
        EffectClass.READ,
        "Inspect journaled observations for mission (spec §35).",
        _obj({"mission": _STR, "limit": {"type": "integer"}}, ["mission"]),
    ),
    ToolBinding(
        "autonomous.decisions",
        None,
        EffectClass.READ,
        "Inspect decisions made by MasterAgent for mission (spec §35).",
        _obj({"mission": _STR, "limit": {"type": "integer"}}, ["mission"]),
    ),
    ToolBinding(
        "autonomous.progress",
        None,
        EffectClass.READ,
        "Inspect verified progress and coverage for mission (spec §35).",
        _obj({"mission": _STR}, ["mission"]),
    ),
    ToolBinding(
        "autonomous.waits",
        None,
        EffectClass.READ,
        "Inspect active and past wait conditions for mission (spec §35).",
        _obj({"mission": _STR}, ["mission"]),
    ),
    ToolBinding(
        "autonomous.escalations",
        None,
        EffectClass.READ,
        "Inspect human escalation records for mission (spec §35).",
        _obj({"mission": _STR}, ["mission"]),
    ),
    ToolBinding(
        "autonomous.explain",
        None,
        EffectClass.READ,
        "Explain justification and evidence for a decision (spec §35).",
        _obj({"decision_id": _STR}, ["decision_id"]),
    ),
    ToolBinding(
        "interrupt.reply",
        None,
        EffectClass.WRITE,
        "Reply to a human escalation and resume autonomous mission (spec §35).",
        _obj(
            {"mission": _STR, "escalation_id": _STR, "reply": _STR},
            ["mission", "escalation_id", "reply"],
        ),
    ),
    ToolBinding(
        "mission.revise",
        None,
        EffectClass.WRITE,
        "Revise mission objective and constraints via canonical revision path (spec §35).",
        _obj(
            {"mission": _STR, "new_objective": _STR, "reason": _STR},
            ["mission", "new_objective", "reason"],
        ),
    ),
)

BINDINGS = BINDINGS + _supervision_bindings() + _AUTONOMOUS_BINDINGS

# Execution-aware clients may carry the durable work unit explicitly.  Keep
# ``workspace``/legacy target spellings for compatibility, but expose the
# canonical execution target and binding selector on every repo-scoped tool.
_EXECUTION_SCOPED_TOOLS = {
    "file.read",
    "file.search",
    "file.write",
    "file.patch",
    "shell.exec",
    "test.run",
    "build.run",
    "git.status",
    "git.diff",
    "git.log",
    "git.promote",
    "hicode.execute",
    "worker.dispatch",
}
for _binding in BINDINGS:
    if _binding.name not in _EXECUTION_SCOPED_TOOLS:
        continue
    _properties = _binding.schema.setdefault("properties", {})
    if isinstance(_properties, dict):
        _properties.setdefault("execution_id", _STR)
        _properties.setdefault("goal_run_id", _STR)
        if _binding.name in {"shell.exec", "test.run", "build.run", "file.write", "file.patch"}:
            target_schema = _properties.get("execution_target")
            if isinstance(target_schema, dict):
                target_schema["enum"] = list(EXECUTION_TARGETS)

BINDING_INDEX: dict[str, ToolBinding] = {b.name: b for b in BINDINGS}


# ── long-running execution manager ─────────────────────────────────────
# The remote gateway owns ONE execution authority: a durable job manager over
# the canonical Veya tools (``veya.remote.execution.DurableJobManager``).
# Status/cancel are keyed by token ownership so an explicit reconnect can see
# the same execution, while a different principal stays denied.
RemoteJobManager = DurableJobManager


# ── adapter ────────────────────────────────────────────────────────────
class RemoteToolAdapter:
    def __init__(
        self,
        executor: VeyaExecutor | None = None,
        *,
        output_limit: int = _DEFAULT_OUTPUT_LIMIT,
        redact: Callable[[Any], Any] | None = None,
        default_timeout_s: float = 900.0,
        execution_store: ExecutionStore | None = None,
        heartbeat_interval_s: float = 5.0,
        heartbeat_timeout_s: float = 60.0,
        recovery_runner_factory: Callable[[Any], Any] | None = None,
        side_effect_ledger: SideEffectLedger | None = None,
    ) -> None:
        self._executor = executor
        self._output_limit = max(1024, int(output_limit))
        self._redact = redact or (lambda value: value)
        self._default_timeout_s = default_timeout_s
        self.metrics = LatencyMetrics()
        self._detect_cache: dict[str, tuple[float, dict[str, list[str]]]] = {}
        self._worktree_locks: dict[str, asyncio.Lock] = {}
        self.jobs = DurableJobManager(
            execution_store if execution_store is not None else ExecutionStore(None),
            heartbeat_interval_s=heartbeat_interval_s,
            heartbeat_timeout_s=heartbeat_timeout_s,
            recovery_runner_factory=recovery_runner_factory,
        )
        self._startup_lock = asyncio.Lock()
        self._startup_complete = False
        self._startup_error: BaseException | None = None
        self.startup_recovery_report: dict[str, Any] = {
            "started": False,
            "recovered": 0,
            "pending_recovery": 0,
            "recovery_degraded": False,
            "failures": [],
        }
        self.health_registry = ExecutorHealthRegistry()
        self.action_gateway = ActionGateway()
        # Execution-scoped resource authority.  The legacy session cache stays
        # available only for clients that have not yet supplied execution_id;
        # new execution-aware calls always resolve through this registry.
        binding_root = os.environ.get("VEYA_EXECUTION_WORKTREE_STORE")
        self.execution_worktrees = ExecutionWorktreeRegistry(binding_root)
        self.side_effect_ledger = side_effect_ledger
        self.l1_finalizer = L1ExecutionFinalizer(
            self.execution_worktrees, side_effect_ledger=self.side_effect_ledger
        )

    async def _ensure_promotion_ledger(self) -> SideEffectLedger:
        """Reuse the existing durable execution repository for promotions."""

        if self.side_effect_ledger is not None:
            return self.side_effect_ledger
        async with self._startup_lock:
            if self.side_effect_ledger is None:
                from runtime.execution.durable import DurableExecutionRepository
                from runtime.execution.runtime import get_durable_runtime

                durable = get_durable_runtime()
                if durable.config.enabled:
                    if not durable._started:
                        await durable.start()
                    repository = durable.repository
                else:
                    repository = DurableExecutionRepository(
                        sqlite_path=Path(
                            os.environ.get(
                                "VEYA_EXECUTION_SQLITE_PATH", ".veya/execution-runtime.sqlite3"
                            )
                        )
                    )
                    await repository.migrate()
                self.side_effect_ledger = SideEffectLedger(repository)
                self.l1_finalizer.side_effect_ledger = self.side_effect_ledger
        assert self.side_effect_ledger is not None
        return self.side_effect_ledger

    def _projection_is_live(self, record: Any, now: float) -> bool:
        """True when a worker still owns the non-terminal projection lease."""
        if record.is_terminal:
            return False
        if record.status == "QUEUED" and record.heartbeat_at is None:
            # Admitted but the worker has not written its first heartbeat yet.
            return True
        return (
            record.heartbeat_at is not None
            and now - record.heartbeat_at <= self.jobs.heartbeat_timeout_s
        )

    async def initialize(self) -> dict[str, Any]:
        """Initialize the projection and trigger canonical GoalRun recovery.

        This is lifecycle wiring only.  Durable recovery remains implemented
        by ``DurableJobManager.recover_unfinished`` and GoalRun; the adapter
        never resumes a provider or changes a business state itself.

        A non-terminal projection that cannot be auto-replayed is a *degraded*
        recovery state, not a fatal startup state.  The projection keeps its
        original identity and stays visible so the control plane
        (``process.status`` / ``process.cancel`` / explicit resume) can
        converge it.  Failing startup here would make the documented
        "explicit operator retry" impossible to perform and would take the
        whole data plane offline.
        """
        async with self._startup_lock:
            if self._startup_complete:
                if self._startup_error is not None:
                    raise RuntimeError("remote startup recovery failed") from self._startup_error
                return dict(self.startup_recovery_report)
            if self._startup_error is not None:
                raise RuntimeError("remote startup recovery failed") from self._startup_error
            try:
                reconciliation = self.jobs.reconcile_unfinished()
                records = self.jobs.unfinished_records()
                if records and self.jobs.recovery_runner_factory is None:
                    now = time.time()
                    live = sum(1 for record in records if self._projection_is_live(record, now))
                    pending = len(records) - live
                    self.startup_recovery_report = {
                        "started": True,
                        "recovered": 0,
                        "deferred_live": live,
                        "pending_recovery": pending,
                        "recovery_degraded": pending > 0,
                        "reconciliation": reconciliation,
                        "failures": [],
                    }
                    self._startup_complete = True
                    return dict(self.startup_recovery_report)
                recovered = await self.jobs.recover_unfinished()
                failures = list(self.jobs.recovery_failures)
                self.startup_recovery_report = {
                    "started": True,
                    "recovered": recovered,
                    "pending_recovery": len(failures),
                    "recovery_degraded": bool(failures),
                    "reconciliation": reconciliation,
                    "failures": failures,
                }
                self._startup_complete = True
                return dict(self.startup_recovery_report)
            except BaseException as exc:
                self._startup_error = exc
                self.startup_recovery_report = {
                    "started": False,
                    "recovered": 0,
                    "failures": list(self.jobs.recovery_failures),
                    "error": str(exc),
                }
                raise

    # ── discovery ───────────────────────────────────────────────────
    def list_tools(self) -> list[dict[str, Any]]:
        """MCP tool definitions.

        Every tool exposes an optional ``workspace`` argument so a client can
        target any workspace authorized for its token (ChatGPT only sends
        ``name`` + ``arguments``, so it cannot switch at the transport level).
        """

        tools: list[dict[str, Any]] = []
        for binding in BINDINGS:
            schema = json.loads(json.dumps(binding.schema))
            properties = schema.setdefault("properties", {})
            properties.setdefault(
                "workspace",
                {
                    "type": "string",
                    "description": "Authorized workspace root to bind for this call.",
                },
            )
            # ``workspace`` is the bound authorization root.  ``workspace_path``
            # is the canonical per-operation target selector and may point at
            # any nested repository below that root.  Keep the older ``path``
            # field in individual schemas as a compatibility alias.
            properties.setdefault(
                "workspace_path",
                dict(_WORKSPACE_PATH),
            )
            tools.append(
                {"name": binding.name, "description": binding.description, "inputSchema": schema}
            )
        return tools

    def binding(self, name: str) -> ToolBinding | None:
        return BINDING_INDEX.get(name)

    # ── execution ───────────────────────────────────────────────────
    async def call(
        self, session: RemoteSession, name: str, arguments: dict[str, Any] | None
    ) -> RemoteCallResult:
        """Instrumented entry point; ``_call_impl`` does the real work (P0-M)."""

        started = time.time()
        try:
            await self.initialize()
            result = await self._call_impl(session, name, arguments)
        except ExecutionError as exc:
            try:
                code = RemoteErrorCode(exc.code)
            except ValueError:
                code = RemoteErrorCode.EXECUTION_FAILED
            result = self._fail(name, session, code, exc.message)
        except Exception as exc:
            self.metrics.record(
                name, mode="sync", duration_ms=(time.time() - started) * 1000, ok=False
            )
            # Never turn an execution defect into an opaque MCP INTERNAL
            # error. Preserve a typed failure at the protocol boundary.
            result = self._fail(
                name,
                session,
                RemoteErrorCode.EXECUTION_FAILED,
                f"execution failure ({type(exc).__name__})",
            )
        mode = "sync"
        if isinstance(result.result, dict) and result.result.get("accepted"):
            mode = "async"
        self.metrics.record(
            name, mode=mode, duration_ms=(time.time() - started) * 1000, ok=result.ok
        )
        return result

    def metrics_summary(self) -> dict[str, Any]:
        return self.metrics.summary()

    async def _call_impl(
        self, session: RemoteSession, name: str, arguments: dict[str, Any] | None
    ) -> RemoteCallResult:
        started = time.time()
        args = dict(arguments or {})
        binding = BINDING_INDEX.get(name)
        if binding is None:
            return self._fail(
                name, session, RemoteErrorCode.TOOL_DENIED, "unknown or unavailable tool"
            )

        try:
            args = self._normalize_command_selector(name, args)
        except RemoteToolAdapterError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)

        requested_workspace = args.get("workspace")
        workspace = (
            canonical(str(requested_workspace)) if requested_workspace else session.active_workspace
        )
        try:
            policy = WorkspacePolicy(
                # The remote binding is the per-call boundary.  Global runtime
                # extra roots may help canonical host tools resolve absolute
                # paths, but must never widen a client's explicit workspace.
                Path(workspace),
                session.permissions,
            )
        except WorkspacePolicyError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)

        # P0-A/B: the explicitly bound workspace is the only execution target.
        # Resolve its canonical identity up front and fail closed on any mismatch
        # (never fall back to cwd / previous session / default / another repo).
        try:
            ws_binding = resolve_requested_workspace(policy.root, allowed_roots=session.workspaces)
        except WorkspaceBindingError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        workspace = ws_binding.requested_realpath

        if name.startswith("approval."):
            return self._approval_call(session, name, args, workspace, started)

        if name == "process.status":
            return self._process_status(session, name, args, started)
        if name == "process.cancel":
            return await self._process_cancel(session, name, args, started)
        if name.startswith("autonomous.") or name in ("interrupt.reply", "mission.revise"):
            return await self._autonomous_call(session, name, args, started)

        # Check action classification & approval gate (spec §1, §4, §37, §38)
        ok, err_code, err_msg, classification = self.action_gateway.check_action(
            name, args, session, cwd=workspace
        )
        if not ok:
            return self._fail(
                name,
                session,
                err_code or RemoteErrorCode.POLICY_BLOCKED,
                err_msg or "action rejected by gateway",
            )

        try:
            policy.require(
                binding.effect,
                needs_shell=binding.needs_shell,
                needs_git=binding.needs_git,
            )
            command = args.get("command")
            if binding.needs_shell and isinstance(command, str):
                if classification.capability_id == "service_control.user":
                    if not session.permissions.service_control:
                        raise WorkspacePolicyError(
                            "POLICY_BLOCKED",
                            "service control capability is required for systemctl operations",
                        )
                elif classification.category == ActionCategory.HUMAN_GATED:
                    # Verified via approval_id by ActionGateway
                    pass
                else:
                    # AUTO_OPEN operations do not require destructive capability
                    pass
        except WorkspacePolicyError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)

        if (
            name == "shell.exec"
            and isinstance(command, str)
            and classification.capability_id == "service_control.user"
        ):
            if not session.permissions.service_control:
                return self._fail(
                    name,
                    session,
                    RemoteErrorCode.POLICY_BLOCKED,
                    "service control capability is required for systemctl operations",
                )
            return await self._call_service_control(session, command, started)

        # Permission and destructive-command decisions are independent of Git
        # discovery.  Keep them first so an unauthorized request cannot leak
        # repository state (and preserves the stable TOOL_DENIED/
        # POLICY_BLOCKED contract), while authorized worktree-backed tools then
        # resolve their canonical nested repository below.
        try:
            resolution = self._resolve_operation(session, policy, ws_binding, name, args)
            operation_binding = (
                ws_binding.with_resolution(resolution)
                if resolution.repo_root is not None
                else ws_binding
            )
        except (WorkspaceBindingError, RemoteToolAdapterError, WorkspacePolicyError) as exc:
            code = RemoteErrorCode(getattr(exc, "code", RemoteErrorCode.WORKSPACE_DENIED))
            return self._fail(name, session, code, str(getattr(exc, "message", exc)))

        # P0-A: fast metadata primitives are synchronous; never a job, never an LLM.
        if name in _FAST_READ_TOOLS:
            return await self._call_fast_read(
                session, policy, name, args, ws_binding, started, resolution=resolution
            )
        if name in _FAST_GIT_TOOLS:
            return await self._call_fast_git(
                session, name, args, operation_binding, started, resolution=resolution
            )

        # L1 parallel dispatch (mechanical; no planning/routing/JEV).
        if name == "worker.dispatch":
            return await self._call_worker_dispatch(
                session, policy, binding, args, operation_binding, started
            )

        # P0-B/K: command tools -> direct streaming job with a fast sync window.
        if name in _COMMAND_TOOLS:
            return await self._call_command_tool(
                session, policy, binding, args, operation_binding, started, resolution=resolution
            )

        # P1-B: explicit direct_hicode worker (real Hicode + LLM, no orchestrator).
        # Injected executors (tests / qualification harness) keep the canonical
        # durability path below; the production default executor is Hicode itself.
        if name == "hicode.execute" and self._executor is None:
            return await self._call_direct_hicode(
                session, policy, binding, args, operation_binding, started
            )

        # LLM-backed coding runs as a durable background job; the submit RPC
        # returns an execution_id immediately by default. Workspace binding
        # validation + worktree creation happen inside the worker so the durable
        # lifecycle exposes WORKSPACE_VALIDATION -> PLANNING/... -> COMPLETED.
        if binding.long_running:
            wait_s = self._wait_seconds(args)
            record = self.jobs.submit(
                session=session,
                tool=name,
                veya_tool=str(binding.veya_tool or ""),
                binding=operation_binding,
                runner=self._make_long_runner(
                    session, policy, binding, args, operation_binding, tool=name
                ),
                limits=_execution_limits(name, args),
            )
            if wait_s is None or wait_s > 0:
                await self.jobs.wait(record.execution_id, timeout_s=wait_s)
            snapshot = self._redact(
                record.to_public(heartbeat_timeout_s=self.jobs.heartbeat_timeout_s)
            )
            if record.is_terminal and record.status != str(ExecutionStatus.COMPLETED):
                failure = self._fail(
                    name,
                    session,
                    _terminal_error_code(record),
                    record.message or "execution did not succeed",
                )
                failure.result = snapshot
                failure.execution_id = record.execution_id
                failure.duration_ms = (time.time() - started) * 1000
                return failure
            result = dict(snapshot)
            result["accepted"] = True
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=workspace,
                result=self._redact(result),
                execution_id=record.execution_id,
                duration_ms=(time.time() - started) * 1000,
            )

        try:
            veya_tool, kwargs, write_root = await self._prepare(
                session, policy, binding, args, operation_binding
            )
        except RemoteToolAdapterError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        except WorkspacePolicyError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        except WorkspaceBindingError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)

        try:
            output = await self._invoke(veya_tool, kwargs, write_root)
        except TimeoutError:
            return self._fail(name, session, RemoteErrorCode.TIMEOUT, "tool timed out")
        except asyncio.CancelledError:
            return self._fail(name, session, RemoteErrorCode.CANCELLED, "tool cancelled")
        except WorkspacePolicyError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        except Exception as exc:
            code = (
                RemoteErrorCode.TIMEOUT
                if "timed out" in str(exc)
                else RemoteErrorCode.EXECUTION_FAILED
            )
            return self._fail(name, session, code, f"{type(exc).__name__}: {exc}")

        text, truncated = self._limit(output)
        return RemoteCallResult(
            ok=True,
            tool=name,
            session_id=session.session_id,
            workspace=workspace,
            result=self._redact({"text": text, "truncated": truncated}),
            duration_ms=(time.time() - started) * 1000,
        )

    def _approval_call(
        self,
        session: RemoteSession,
        name: str,
        args: dict[str, Any],
        workspace: str,
        started: float,
    ) -> RemoteCallResult:
        store = self.action_gateway.approval_store
        try:
            if name == "approval.request":
                from .models import RiskClass

                risk = RiskClass(str(args.get("risk_class") or "P2_ROOT_MUTATION"))
                record = store.create_approval(
                    principal=session.principal,
                    capability_id=str(args["capability_id"]),
                    normalized_operation=str(args["operation"]),
                    cwd=workspace,
                    workspace=workspace,
                    risk_class=risk,
                    ttl_s=float(args.get("ttl_s") or 300),
                    decision="pending",
                )
                payload = record.to_dict()
                payload["status"] = "PENDING"
            else:
                approval_id = str(args.get("approval_id") or "")
                record = store.lookup(approval_id)
                if record is None or record.principal != session.principal:
                    return self._fail(
                        name, session, RemoteErrorCode.NOT_FOUND, "approval request not found"
                    )
                if name == "approval.status":
                    payload = record.to_dict()
                    payload["status"] = record.decision.upper()
                else:
                    decision = str(args.get("decision") or "").lower()
                    record = store.decide(
                        approval_id, principal=session.principal, decision=decision
                    )
                    payload = record.to_dict()
                    payload["status"] = record.decision.upper()
        except (KeyError, ValueError, PermissionError) as exc:
            return self._fail(name, session, RemoteErrorCode.INVALID_ARGUMENT, str(exc))
        return RemoteCallResult(
            ok=True,
            tool=name,
            session_id=session.session_id,
            workspace=workspace,
            result=payload,
            duration_ms=(time.time() - started) * 1000,
        )

    def _normalize_command_selector(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Normalize the narrow legacy ``cd <path> && <command>`` form.

        The structured ``path`` field is canonical.  This compatibility parser
        accepts only a leading, unquoted, single path component expression and
        removes it before the command runner's no-shell-operator policy sees
        the command.  It is intentionally not a general shell parser.
        """

        normalized = dict(args)
        workspace_path = normalized.pop("workspace_path", None)
        if workspace_path is not None:
            existing_path = normalized.get("path")
            if existing_path is not None and str(existing_path) != str(workspace_path):
                raise RemoteToolAdapterError(
                    RemoteErrorCode.INVALID_ARGUMENT,
                    "path and workspace_path select different targets",
                )
            normalized["path"] = workspace_path

        if name not in _COMMAND_TOOLS or normalized.get("path") or not normalized.get("command"):
            return normalized
        match = re.match(
            r"^\s*cd\s+([^\s;&|<>]+)\s*&&\s*(.+?)\s*$",
            str(normalized["command"]),
            flags=re.DOTALL,
        )
        if not match:
            return normalized
        normalized["path"] = match.group(1)
        normalized["command"] = match.group(2)
        normalized["_repo_selector_evidence"] = {
            "kind": "restricted_cd_compatibility",
            "selector": match.group(1),
        }
        return normalized

    def _resolve_operation(
        self,
        session: RemoteSession,
        policy: WorkspacePolicy,
        ws_binding: WorkspaceBinding,
        name: str,
        args: dict[str, Any],
    ) -> RepoResolution:
        """Resolve the operation target through the one canonical resolver."""

        path = args.get("path")
        if name in {"workspace.list", "workspace.info", "file.read", "file.search"}:
            requested_path = path or "."
            require_repo = False
        elif name in {"file.write", "file.patch"}:
            requested_path = path
            require_repo = True
            if requested_path is None or not str(requested_path).strip():
                raise RemoteToolAdapterError(
                    RemoteErrorCode.INVALID_ARGUMENT, f"{name} requires a path"
                )
            policy.resolve(
                str(requested_path),
                # A patch target may have been created in the isolated
                # worktree by an earlier file.write; existence is checked
                # after owner->worktree mapping.
                must_exist=None,
                for_write=True,
            )
        elif (
            name in _FAST_GIT_TOOLS
            or name in _COMMAND_TOOLS
            or name
            in {
                "worker.dispatch",
                "hicode.execute",
            }
        ):
            requested_path = path or "."
            require_repo = True
        else:
            # Supervision and artifact metadata stay scoped to the bound root;
            # they do not select a Git worktree.
            requested_path = "."
            require_repo = False
        if requested_path is None:
            raise RemoteToolAdapterError(
                RemoteErrorCode.INVALID_ARGUMENT, f"{name} requires a path"
            )
        resolution = resolve_repo_target(
            ws_binding.requested_realpath,
            requested_path,
            operation=name,
            allowed_roots=session.workspaces,
            require_repo=require_repo,
        )
        compat = args.get("_repo_selector_evidence")
        if compat:
            resolution.evidence["compatibility_selector"] = compat
        return resolution

    # ── argument preparation ────────────────────────────────────────
    async def _prepare(
        self,
        session: RemoteSession,
        policy: WorkspacePolicy,
        binding: ToolBinding,
        args: dict[str, Any],
        ws_binding: WorkspaceBinding,
    ) -> tuple[str, dict[str, Any], Path | None]:
        workspace = policy.root
        ensure_readable(str(workspace))
        name = binding.name
        if binding.veya_tool is None:
            raise RemoteToolAdapterError(RemoteErrorCode.TOOL_DENIED, f"{name} is adapter-owned")

        if name.startswith("veya."):
            # Supervision surface: orchestration metadata, bound to the session
            # workspace; execution still goes through the canonical runtime.
            payload = {
                key: value
                for key, value in args.items()
                if key not in {"workspace", "path", "_repo_selector_evidence"}
            }
            return binding.veya_tool, {**payload, "project_root": str(workspace)}, None

        if name in {
            "workspace.list",
            "workspace.info",
            "file.read",
            "file.search",
            "artifact.list",
            "artifact.read",
        }:
            base = self._base_dir(
                session, str(workspace), execution_id=str(args.get("execution_id") or "")
            )
            return binding.veya_tool, self._read_args(name, session, policy, base, args), None

        if name == "hicode.execute":
            # P0-A: pass the requested repo explicitly. Without this the canonical
            # Hicode worker falls back to its process default workspace, which is
            # exactly the wrong-repo regression this task closes. The worker later
            # binds its isolated worktree through ``bound_hicode_workspace``;
            # Remote containment, not HICODE_WORKSPACE, is the authority here.
            hicode_workspace = (
                ws_binding.repo_root if ws_binding.is_git_repo else ws_binding.requested_realpath
            )
            return binding.veya_tool, self._hicode_args(args, hicode_workspace), None

        # Mutations use the operation resolution computed before dispatch.  This
        # keeps file.write/file.patch on the same nested-repo worktree as every
        # other worktree-backed tool.
        if name in {"file.write", "file.patch"}:
            target = Path(ws_binding.canonical_target_path or args["path"]).resolve(strict=False)
            repo_root = Path(ws_binding.repo_root).resolve()
            try:
                relative = target.relative_to(repo_root)
            except ValueError as exc:
                raise WorkspaceBindingError(
                    "WORKSPACE_DENIED",
                    "TARGET_REPO_MISMATCH",
                    f"target is outside the resolved repository: {target}",
                ) from exc

            is_canonical = (
                str(args.get("execution_target") or "").upper() in ("CANONICAL_WORKTREE", "HOST")
                or args.get("target_scope") == "canonical"
                or bool(args.get("allow_canonical"))
            )

            # P0-B/C: every execution-target-aware entry reuses the one
            # canonical resolver — never guesses NEW vs EXISTING on its own.
            execution_target = resolve_execution_target(
                str(repo_root),
                workspace_path=str(target),
                requested_execution_target=str(args.get("execution_target") or ""),
            )
            if is_canonical or execution_target in ("CANONICAL_WORKTREE", "HOST"):
                mapped = target
                write_root = repo_root
            else:
                existing = find_existing_worktree_root(target)
                if existing is not None:
                    mapped = target
                    write_root = existing
                else:
                    execution_id = str(args.get("execution_id") or "")
                    if execution_id:
                        execution_binding = await run_sync_in_daemon_thread(
                            self.execution_worktrees.get_or_create,
                            execution_id,
                            str(repo_root),
                            goal_run_id=str(args.get("goal_run_id") or "") or None,
                            objective=name,
                        )
                        worktree, verified_repo = (
                            execution_binding.worktree_path,
                            execution_binding.canonical_repo_root,
                        )
                    else:
                        worktree, verified_repo = await self._ensure_worktree(
                            session, str(repo_root)
                        )
                    if canonical(verified_repo) != canonical(repo_root):
                        raise WorkspaceBindingError(
                            "WORKSPACE_DENIED",
                            "WORKTREE_REPO_IDENTITY_MISMATCH",
                            f"worktree belongs to {verified_repo}, expected {repo_root}",
                        )
                    mapped = (Path(worktree) / relative).resolve(strict=False)
                    worktree_root = Path(worktree).resolve()
                    if mapped != worktree_root and worktree_root not in mapped.parents:
                        raise WorkspaceBindingError(
                            "WORKSPACE_DENIED",
                            "TARGET_ESCAPES_WORKTREE",
                            f"target escapes isolated worktree: {mapped}",
                        )
                    write_root = Path(worktree)

            if name == "file.patch" and not mapped.exists():
                raise RemoteToolAdapterError(
                    RemoteErrorCode.NOT_FOUND,
                    f"path not found for patch: {args['path']}",
                )
            if name == "file.write":
                kwargs = {
                    "filepath": str(mapped),
                    "content": str(args["content"]),
                    "overwrite": bool(args.get("overwrite", True)),
                }
                return binding.veya_tool, kwargs, write_root
            kwargs = {
                "filepath": str(mapped),
                "start_tag": str(args["start_tag"]),
                "new_text": str(args["new_text"]),
            }
            if args.get("end_tag"):
                kwargs["end_tag"] = str(args["end_tag"])
            return binding.veya_tool, kwargs, write_root

        # Worktree-backed tools derive their repo strictly from the explicitly
        # bound workspace: never from the session's other worktrees (P0-A/M).
        # P0-B: reuse the canonical resolver here as well.
        fallback_target = resolve_execution_target(
            ws_binding.requested_realpath,
            workspace_path=ws_binding.canonical_target_path or ws_binding.repo_root,
            requested_execution_target=str(args.get("execution_target") or ""),
        )
        fallback_existing = (
            find_existing_worktree_root(ws_binding.canonical_target_path or ws_binding.repo_root)
            if fallback_target == "EXISTING_WORKTREE"
            else None
        )
        if fallback_existing is not None:
            worktree = str(fallback_existing)
        else:
            execution_id = str(args.get("execution_id") or "")
            if execution_id:
                execution_binding = await run_sync_in_daemon_thread(
                    self.execution_worktrees.get_or_create,
                    execution_id,
                    ws_binding.repo_root,
                    goal_run_id=str(args.get("goal_run_id") or "") or None,
                    objective=name,
                )
                worktree = execution_binding.worktree_path
            else:
                worktree, _ = await self._ensure_worktree(session, ws_binding.repo_root)
        return (
            binding.veya_tool,
            self._worktree_args(name, session, policy, worktree, args),
            None,
        )

    def _read_args(
        self,
        name: str,
        session: RemoteSession,
        policy: WorkspacePolicy,
        base: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name in {"workspace.list", "workspace.info"}:
            target = self._resolve_in(policy, base, args.get("path") or ".", must_exist=True)
            return {"path": str(target)}
        if name == "file.read":
            target = self._resolve_target(session, policy, base, args["path"], must_exist=True)
            return {"filepath": str(target), "max_lines": int(args.get("max_lines", 2000))}
        if name == "file.search":
            root = self._resolve_target(
                session, policy, base, args.get("path") or ".", must_exist=True
            )
            kwargs: dict[str, Any] = {"pattern": str(args["pattern"]), "root": str(root)}
            if args.get("glob"):
                kwargs["glob"] = str(args["glob"])
            return kwargs
        if name == "artifact.list":
            out = self._task_outputs(session, policy)
            if not out.exists():
                raise RemoteToolAdapterError(
                    RemoteErrorCode.NOT_FOUND, "no finalized artifacts for this session"
                )
            return {"path": str(out)}
        if name == "artifact.read":
            out = self._task_outputs(session, policy)
            target = self._resolve_in(policy, str(out), args["path"], must_exist=True)
            return {"filepath": str(target), "max_lines": int(args.get("max_lines", 4000))}
        raise RemoteToolAdapterError(RemoteErrorCode.INVALID_ARGUMENT, f"bad read tool {name}")

    def _worktree_args(
        self,
        name: str,
        session: RemoteSession,
        policy: WorkspacePolicy,
        worktree: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "git.status":
            return {"worktree_path": worktree}
        if name == "git.diff":
            return {"worktree_path": worktree}
        if name == "git.log":
            limit = max(1, min(int(args.get("limit", 20)), 200))
            return {
                "worktree_path": worktree,
                "command": f"git log -n {limit} --oneline",
                "timeout_s": 60,
            }
        if name == "shell.exec":
            command = str(args["command"])
            kwargs: dict[str, Any] = {
                "worktree_path": worktree,
                "command": command,
                "timeout_s": float(args.get("timeout_s", self._default_timeout_s)),
                "approved": True,
            }
            if args.get("profile"):
                kwargs["profile"] = str(args["profile"])
            if args.get("network"):
                kwargs["network"] = str(args["network"])
            return kwargs
        if name in {"test.run", "build.run"}:
            timeout = float(args.get("timeout_s", self._default_timeout_s))
            if name == "test.run":
                kwargs = {"worktree_path": worktree, "timeout_s": timeout}
            else:
                kwargs = {"worktree_path": worktree, "timeout_s": timeout}
            if args.get("command"):
                kwargs["command"] = str(args["command"])
            kwargs["approved"] = True
            return kwargs
        raise RemoteToolAdapterError(RemoteErrorCode.INVALID_ARGUMENT, f"bad worktree tool {name}")

    def _hicode_args(self, args: dict[str, Any], workspace: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"task": str(args["task"]), "workspace": str(workspace)}
        if args.get("max_steps"):
            kwargs["max_steps"] = int(args["max_steps"])
        if args.get("timeout_sec"):
            # P0-L: ``timeout_sec`` is the execution budget, not a submit-RPC wait.
            kwargs["timeout_sec"] = int(args["timeout_sec"])
        return kwargs

    # ── helpers ─────────────────────────────────────────────────────
    def _resolve_target(
        self,
        session: RemoteSession,
        policy: WorkspacePolicy,
        base: str,
        path: str,
        *,
        must_exist: bool | None = True,
        for_write: bool = False,
    ) -> Path:
        """Resolve a path and map it into this session's isolated worktree.

        ChatGPT sends absolute host paths; when the owning git repo already has
        a session worktree, reads/writes are transparently redirected there so a
        client never has to know about the isolation layer.
        """

        # ``base`` is retained for the artifact/legacy call contract; repo and
        # target selection itself always starts at the canonical bound root so
        # a mapped worktree can never become the next resolver boundary.
        policy.resolve(
            path,
            # A file may have been created only in the session worktree. Check
            # existence again after owner->worktree mapping below.
            must_exist=None if must_exist is True else must_exist,
            for_write=for_write,
        )
        resolution = resolve_repo_target(
            policy.root,
            path,
            operation="file.target",
            require_repo=False,
        )
        target = Path(resolution.target_path)
        if resolution.repo_root is not None:
            worktree = session.worktrees.get(resolution.repo_root)
            if worktree:
                target = (Path(worktree) / target.relative_to(resolution.repo_root)).resolve(
                    strict=False
                )
                worktree_root = Path(worktree).resolve()
                if target != worktree_root and worktree_root not in target.parents:
                    raise RemoteToolAdapterError(
                        RemoteErrorCode.WORKSPACE_DENIED,
                        f"resolved target escapes isolated worktree: {target}",
                    )
        if must_exist is True and not target.exists():
            raise RemoteToolAdapterError(RemoteErrorCode.NOT_FOUND, f"path not found: {path}")
        if must_exist is False and target.exists():
            raise RemoteToolAdapterError(
                RemoteErrorCode.POLICY_BLOCKED, f"path already exists: {path}"
            )
        return target

    def _base_dir(self, session: RemoteSession, workspace: str, *, execution_id: str = "") -> str:
        if execution_id:
            repo = git_repo_root(workspace)
            if repo is not None:
                binding = self.execution_worktrees.resolve(execution_id, repo)
                return binding.worktree_path
        # Worktrees are keyed by repository root; fall back to the raw workspace
        # key only for the legacy non-git case.
        repo = git_repo_root(workspace)
        if repo is not None:
            mapped = session.worktrees.get(str(repo))
            if mapped:
                if Path(mapped).exists():
                    return mapped
                session.worktrees.pop(str(repo), None)
        raw = session.worktrees.get(workspace)
        if raw:
            if Path(raw).exists():
                return raw
            session.worktrees.pop(workspace, None)
        return workspace

    def _task_id(self, session: RemoteSession, workspace: str, lane: str = "") -> str:
        # Deterministic per session + repository identity.  A parent workspace
        # can contain multiple repos, so the repo identity is part of the
        # isolation key and prevents stratum/hevi worktrees from colliding.
        repo_identity = git_repo_identity(workspace)
        seed = (
            f"{session.session_id}:{repo_identity}"
            if not lane
            else f"{session.session_id}:{repo_identity}:{lane}"
        ).encode()
        return "remote-" + hashlib.sha1(seed).hexdigest()[:20]

    def _task_outputs(self, session: RemoteSession, policy: WorkspacePolicy) -> Path:
        return (
            policy.root / _TASK_OUTPUTS / self._task_id(session, str(policy.root)) / "outputs"
        ).resolve()

    async def _ensure_worktree(self, session: RemoteSession, workspace: str) -> tuple[str, str]:
        """Create/reuse the isolated worktree for ``workspace`` (P0-B/M).

        Returns ``(worktree_path, repo_root)``. The repo root is the one the
        canonical ``coding_worktree_create`` recorded for this worktree and it
        must match the requested workspace's repo; otherwise no execution starts.
        """

        try:
            requested = resolve_requested_workspace(workspace, allowed_roots=session.workspaces)
        except WorkspaceBindingError as exc:
            raise RemoteToolAdapterError(RemoteErrorCode(exc.code), exc.message) from exc

        # Key by repository identity, not by whatever workspace string came in.
        key = requested.repo_root
        existing = session.worktrees.get(key)
        if existing and Path(existing).exists():
            verified = verify_worktree_repo_identity(
                requested, worktree_path=existing, worktree_repo_root=key
            )
            return str(verified.worktree_path), str(verified.repo_root)
        task_id = self._task_id(session, key)
        expected = Path(requested.repo_root) / ".veya" / "worktrees" / f"task-{task_id}"
        if expected.is_dir():
            verify_worktree_repo_identity(
                requested, worktree_path=str(expected), worktree_repo_root=requested.repo_root
            )
            session.worktrees[key] = str(expected)
            return str(expected), key
        output = await self._invoke(
            "coding_worktree_create",
            {
                "workspace_path": requested.repo_root,
                "task_id": task_id,
                "objective": "remote mcp session",
            },
            None,
        )
        data = _load_json(output)
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise RemoteToolAdapterError(
                RemoteErrorCode.EXECUTION_FAILED,
                f"could not create isolated worktree: {_short(output)}",
            )
        record = (data.get("data") or {}).get("worktree") or {}
        path = record.get("path")
        reported_repo = record.get("repo_root")
        if not path or not reported_repo:
            raise RemoteToolAdapterError(
                RemoteErrorCode.EXECUTION_FAILED,
                "worktree create returned no path/repo_root (cannot verify repo identity)",
            )
        verified = verify_worktree_repo_identity(
            requested, worktree_path=str(path), worktree_repo_root=str(reported_repo)
        )
        session.worktrees[key] = str(verified.worktree_path)
        return str(verified.worktree_path), str(verified.repo_root)

    def _resolve_in(
        self,
        policy: WorkspacePolicy,
        base: str,
        path: str,
        *,
        must_exist: bool | None = True,
        for_write: bool = False,
    ) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = Path(base) / candidate
        return policy.resolve(candidate, must_exist=must_exist, for_write=for_write)

    def _wait_seconds(self, args: dict[str, Any]) -> float | None:
        # P0-C: submit returns an execution_id immediately by default; the client
        # polls process.status. ``wait=True`` opts into blocking for the result;
        # an explicit ``wait_timeout_s`` bounds a blocking wait. ``timeout_sec``
        # never controls this RPC — it is the execution budget.
        if args.get("wait") is False:
            return 0.0
        if "wait_timeout_s" in args:
            return max(0.0, float(args["wait_timeout_s"]))
        if args.get("wait") is True:
            return None
        return 0.0

    def _limit(self, output: str) -> tuple[str, bool]:
        text = output if isinstance(output, str) else json.dumps(output, default=str)
        if len(text) <= self._output_limit:
            return text, False
        return text[: self._output_limit] + "\n...[truncated remote output]", True

    async def _invoke(self, veya_tool: str, kwargs: dict[str, Any], write_root: Path | None) -> str:
        token = None
        if write_root is not None:
            try:
                from server.tool_registry import bind_write_root

                token = bind_write_root(write_root)
            except Exception:
                token = None
        try:
            return await self._executor_call(veya_tool, kwargs)
        finally:
            if token is not None:
                try:
                    from server.tool_registry import reset_write_root

                    reset_write_root(token)
                except Exception:
                    pass

    async def _executor_call(self, veya_tool: str, kwargs: dict[str, Any]) -> str:
        executor = self._executor or _default_executor
        return await executor(veya_tool, kwargs)

    # ── direct command jobs (P0-B/F/H/K) ───────────────────────────
    def _direct_workdir(
        self, session: RemoteSession, ws_binding: WorkspaceBinding, *, execution_id: str = ""
    ) -> str:
        if execution_id:
            return self.execution_worktrees.resolve(
                execution_id, ws_binding.repo_root
            ).worktree_path
        # Run in the session's isolated worktree when one already exists (edits
        # made via file.write/patch land there); otherwise the bound repo root.
        # The command path never *creates* a worktree, so submit stays fast.
        mapped = session.worktrees.get(ws_binding.repo_root)
        return mapped or ws_binding.repo_root

    def _detect_commands(self, ws_binding: WorkspaceBinding) -> dict[str, list[str]]:
        key = ws_binding.repo_root
        now = time.time()
        cached = self._detect_cache.get(key)
        if cached and now - cached[0] < 60.0:
            return cached[1]
        mapping: dict[str, list[str]] = {"test.run": [], "build.run": []}
        try:
            from runtime.coding.workspace_detect import detect_workspace

            workspace = detect_workspace(ws_binding.repo_root, owner_user_id="remote")
            mapping["test.run"] = [
                str(item) for item in getattr(workspace, "test_commands", []) or []
            ]
            mapping["build.run"] = [
                str(item) for item in getattr(workspace, "build_commands", []) or []
            ]
        except Exception:  # detection is best-effort; explicit command still works
            pass
        self._detect_cache[key] = (now, mapping)
        return mapping

    def _resolve_command(
        self,
        name: str,
        args: dict[str, Any],
        ws_binding: WorkspaceBinding,
        runtime_profile: Any | None = None,
    ) -> str:
        explicit = args.get("command")
        if explicit:
            return str(explicit)
        detected = self._detect_commands(ws_binding).get(name) or []
        if detected:
            return detected[0]
        if name == "test.run":
            if runtime_profile is not None:
                if runtime_profile.pytest:
                    if runtime_profile.python_bin:
                        return f"{runtime_profile.python_bin} -m pytest -q"
                    return "pytest -q"
                if runtime_profile.pnpm:
                    return "pnpm test"
                if runtime_profile.node:
                    return "npm test"
            return "python -m pytest -q"
        if name == "build.run" and runtime_profile is not None:
            if runtime_profile.pnpm:
                return "pnpm build"
            if runtime_profile.node:
                return "npm run build"
        raise RemoteToolAdapterError(
            RemoteErrorCode.INVALID_ARGUMENT,
            "no build command detected; pass command explicitly",
        )

    async def _call_service_control(
        self, session: RemoteSession, command: str, started: float
    ) -> RemoteCallResult:
        parsed = parse_systemctl_command(command)
        if parsed is None or parsed.scope != "user":
            return self._fail(
                "shell.exec",
                session,
                RemoteErrorCode.POLICY_BLOCKED,
                "service control command is outside the allowlist",
            )
        action, unit = parsed.action, parsed.unit or ""
        argv: tuple[str, ...]
        if action == "daemon-reload":
            argv = ("/usr/bin/systemctl", "--user", "daemon-reload")
        elif (
            action in ("restart", "stop")
            and unit in _SERVICE_CONTROL_SELF_UNITS
            and not parsed.options
        ):
            transient = f"veya-service-{action}-{time.time_ns()}"
            argv = (
                "/usr/bin/systemd-run",
                "--user",
                "--quiet",
                "--collect",
                f"--unit={transient}",
                "--on-active=500ms",
                "/usr/bin/systemctl",
                "--user",
                action,
                unit,
            )
        else:
            argv = tuple(
                "/usr/bin/systemctl" if index == 0 else value
                for index, value in enumerate(parsed.argv)
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except (OSError, TimeoutError) as exc:
            return self._fail(
                "shell.exec",
                session,
                RemoteErrorCode.EXECUTION_FAILED,
                f"service control failed: {type(exc).__name__}: {exc}",
            )
        out = stdout.decode("utf-8", "replace").strip()
        err = stderr.decode("utf-8", "replace").strip()
        allowed_rcs = (
            (0, 1, 2, 3) if action == "status" else ((0, 3) if action == "is-active" else (0,))
        )
        if proc.returncode not in allowed_rcs:
            return self._fail(
                "shell.exec",
                session,
                RemoteErrorCode.EXECUTION_FAILED,
                (err or out or f"service control exit {proc.returncode}")[:800],
            )
        payload: dict[str, Any]
        if action == "daemon-reload":
            payload = {
                "service_control": True,
                "action": "daemon-reload",
                "destructive_capability_used": False,
            }
        elif action == "is-active":
            payload = {
                "service_control": True,
                "action": "is-active",
                "unit": unit,
                "active": out == "active",
                "state": out,
                "destructive_capability_used": False,
            }
        elif action == "status":
            state = "unknown"
            for line in out.splitlines():
                line_s = line.strip()
                if line_s.startswith("Active:"):
                    parts = line_s.split("Active:", 1)[1].strip().split()
                    if parts:
                        state = parts[0]
                    break
            payload = {
                "service_control": True,
                "action": "status",
                "unit": unit,
                "state": state,
                "exit_code": proc.returncode,
                "text": (out or err)[:4000],
                "destructive_capability_used": False,
            }
        elif action in ("restart", "stop") and unit in _SERVICE_CONTROL_SELF_UNITS:
            payload = {
                "service_control": True,
                "action": action,
                "unit": unit,
                "accepted": True,
                "scheduled": True,
                "delay_ms": 500,
                "destructive_capability_used": False,
            }
        else:
            payload = {
                "service_control": True,
                "action": action,
                "unit": unit,
                "accepted": True,
                "destructive_capability_used": False,
            }
        return RemoteCallResult(
            ok=True,
            tool="shell.exec",
            session_id=session.session_id,
            workspace=session.active_workspace,
            result=payload,
            duration_ms=(time.time() - started) * 1000,
        )

    async def _call_command_tool(
        self,
        session: RemoteSession,
        policy: WorkspacePolicy,
        binding: ToolBinding,
        args: dict[str, Any],
        ws_binding: WorkspaceBinding,
        started: float,
        *,
        resolution: RepoResolution,
    ) -> RemoteCallResult:
        name = binding.name

        # P0-D: runtime profile discovery must use the canonical *main* project root
        # (not the worktree itself) so that the project venv is always found.
        canonical_proj_root = _canonical_project_root(
            resolution.repo_root or resolution.target_path
        )

        runtime_profile = None
        # ``shell.exec`` carries its already-resolved argv/string command and
        # does not need the heavyweight toolchain inventory before durable
        # admission.  Keeping discovery for test/build preserves their command
        # selection while the shell submit path stays non-blocking.
        if name in ("test.run", "build.run"):
            from veya.remote.runtime_profile import discover_runtime_profile

            try:
                runtime_profile = await run_sync_in_daemon_thread(
                    discover_runtime_profile,
                    resolution.target_path,
                    repo_root=canonical_proj_root,
                )
            except Exception:
                runtime_profile = None

        try:
            command = self._resolve_command(name, args, ws_binding, runtime_profile=runtime_profile)
        except RemoteToolAdapterError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)

        # P0-B: use the canonical resolver so workspace=TARGET_WT is auto-detected
        # as EXISTING_WORKTREE without requiring callers to pass the flag explicitly.
        execution_target = resolve_execution_target(
            ws_binding.requested_realpath,
            workspace_path=resolution.target_path,
            requested_execution_target=str(args.get("execution_target") or ""),
        )
        # P0-E/F: ExecutionDomain (policy) -> SandboxProfile (mechanism) via
        # the one canonical mapping. An explicit domain always wins; the
        # legacy ``profile=`` ids (local_trusted/local_restricted/
        # docker_python/docker_node/l0_*) remain accepted for compatibility.
        from veya.remote.runtime_profile import execution_domain_to_profile

        execution_domain = str(args.get("execution_domain") or "").upper()
        mapped_profile = execution_domain_to_profile(execution_domain or None)
        if mapped_profile is not None:
            profile = mapped_profile
        elif "profile" in args:
            profile = str(args["profile"])
        else:
            profile = "l0_workspace_full"

        approved = bool(args.get("approved")) and session.permissions.destructive
        network = args.get("network")
        timeout_s = float(args.get("timeout_s") or DEFAULT_DIRECT_TIMEOUT_S)
        record = self.jobs.submit(
            session=session,
            tool=name,
            veya_tool=str(binding.veya_tool or "direct_command"),
            binding=ws_binding,
            runner=self._make_direct_runner(
                command=command,
                session=session,
                ws_binding=ws_binding,
                profile=profile,
                approved=approved,
                network=str(network) if network else None,
                timeout_s=timeout_s,
                target_path=resolution.target_path,
                execution_target=execution_target,
                execution_domain=execution_domain or "L0_WORKSPACE_FULL",
                runtime_profile=runtime_profile,
            ),
            limits=_execution_limits(name, args),
            execution_type=str(ExecutionType.DIRECT),
            command=command,
            cwd=None,
            profile=profile,
        )
        # P0-B/J: block only within the direct sync window by default; an explicit
        # wait=True caller waits for the full command or wait_timeout_s.
        sync_wait_s = (
            float(args.get("wait_timeout_s") or timeout_s)
            if args.get("wait") is True
            else direct_sync_window_s()
        )
        await self.jobs.wait(record.execution_id, timeout_s=sync_wait_s)
        snapshot = self._redact(record.to_public(heartbeat_timeout_s=self.jobs.heartbeat_timeout_s))
        if record.is_terminal:
            if record.status in {
                str(ExecutionStatus.FAILED),
                str(ExecutionStatus.BLOCKED),
                str(ExecutionStatus.CANCELLED),
            }:
                failure = self._fail(
                    name,
                    session,
                    _terminal_error_code(record),
                    record.message or "command did not start",
                )
                failure.result = snapshot
                failure.execution_id = record.execution_id
                failure.duration_ms = (time.time() - started) * 1000
                return failure
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=ws_binding.requested_realpath,
                result=self._redact(self._direct_inline(record)),
                execution_id=record.execution_id,
                duration_ms=(time.time() - started) * 1000,
            )
        return RemoteCallResult(
            ok=True,
            tool=name,
            session_id=session.session_id,
            workspace=ws_binding.requested_realpath,
            result=self._redact(
                {
                    "accepted": True,
                    "execution_id": record.execution_id,
                    "execution_type": str(ExecutionType.DIRECT),
                    "status": record.status,
                    "phase": record.phase,
                    "command": record.command,
                    "workspace": ws_binding.requested_realpath,
                    "started_at": record.started_at,
                }
            ),
            execution_id=record.execution_id,
            duration_ms=(time.time() - started) * 1000,
        )

    def _direct_inline(self, record: Any) -> dict[str, Any]:
        return {
            "accepted": True,
            "execution_id": record.execution_id,
            "execution_type": str(ExecutionType.DIRECT),
            "status": record.status,
            "phase": record.phase,
            "command": record.command,
            "cwd": record.cwd,
            "requested_workspace": record.requested_realpath,
            "resolved_repo_root": record.resolved_repo_root,
            "exit_code": record.exit_code,
            "stdout_tail": record.stdout_tail,
            "stderr_tail": record.stderr_tail,
            "bytes_stdout": record.bytes_stdout,
            "bytes_stderr": record.bytes_stderr,
            "text": record.stdout_tail or record.stderr_tail,
        }

    def _make_direct_runner(
        self,
        *,
        command: str,
        session: RemoteSession,
        ws_binding: WorkspaceBinding,
        profile: str,
        approved: bool,
        network: str | None,
        timeout_s: float,
        target_path: str,
        lane: str = "",
        execution_target: str = "NEW_ISOLATED_WORKTREE",
        execution_domain: str = "L0_WORKSPACE_FULL",
        runtime_profile: Any | None = None,
    ) -> Any:
        async def runner(reporter: ProgressReporter) -> str:
            # Phase 1.1: shell/test/build are not guaranteed read-only, so the
            # durable worker prepares + verifies an isolated task worktree for
            # the bound repo before the command runs. There is NO fallback to
            # the owner repo root -> a failure is BLOCKED (fail-closed).
            reporter.phase(
                "STARTING",
                message="preparing isolated worktree",
                event="WORKTREE_PREPARE",
            )
            if execution_target in ("CANONICAL_WORKTREE", "HOST"):
                worktree = ws_binding.repo_root
                repo_root = ws_binding.repo_root
            else:
                worktree, repo_root = await self._ensure_isolated_worktree(
                    session,
                    ws_binding.repo_root,
                    lane,
                    execution_target=execution_target,
                    target_path=target_path,
                )
            cwd = self._map_target_to_worktree(target_path, repo_root, worktree)
            reporter.set_worktree(worktree, repo_root)
            reporter.worker(
                execution_mode="direct_command",
                orchestrator="none",
                worker_type="HOST",
                activity=f"isolated worktree ready: {worktree}",
                worker_workspace=worktree,
            )
            reporter.phase(
                "RUNNING",
                message=f"running {command[:120]}",
                event="COMMAND_STARTED",
            )
            # ``profile`` is already resolved before the durable command is
            # admitted.  Do not rediscover it here: that filesystem probe used
            # to run through ``run_sync_in_daemon_thread`` and left the default
            # executor alive during direct-job teardown.
            active_profile = runtime_profile

            try:
                result = await run_direct_command(
                    worktree,
                    command,
                    cwd=cwd,
                    profile=profile,
                    timeout_s=timeout_s,
                    approved=approved,
                    network=network,
                    runtime_profile=active_profile,
                    on_stdout=lambda line: reporter.output("stdout", str(self._redact(line))),
                    on_stderr=lambda line: reporter.output("stderr", str(self._redact(line))),
                    on_process=lambda pid, pgid: reporter.process(
                        worker_pid=pid, process_group_id=pgid
                    ),
                )
            except (DirectDenied, DirectApprovalRequired) as exc:
                raise ExecutionBlocked("POLICY_BLOCKED", str(exc)) from exc
            except CommandPolicyError as exc:
                raise ExecutionBlocked("INVALID_ARGUMENT", str(exc)) from exc
            except Exception as exc:
                # P0-G: an unknown sandbox profile (or any other spawn-time
                # failure) is a fail-closed block with taxonomy — never a
                # silent terminal success downstream.
                from runtime.coding.sandbox_profiles import SandboxProfileError

                if isinstance(exc, SandboxProfileError):
                    raise ExecutionBlocked("INVALID_ARGUMENT", str(exc)) from exc
                raise
            reporter.phase("FINALIZING", message="command exited", event="COMMAND_EXITED")
            reporter.finish_command(
                exit_code=result.exit_code,
                status=result.status,
                command=result.command,
                cwd=result.cwd,
                profile=result.profile,
                stdout_tail=result.stdout_tail,
                stderr_tail=result.stderr_tail,
                bytes_stdout=result.bytes_stdout,
                bytes_stderr=result.bytes_stderr,
            )
            if result.status != "passed":
                # P0-G: a spawn failure (exit_code None), timeout, or
                # non-zero exit must carry failure_class/source/detail so the
                # terminal projection can never classify it as COMPLETED.
                reporter.failure(
                    failure_class=_direct_failure_class(result),
                    source="direct_command",
                    detail=(
                        result.stderr_tail[-4000:]
                        or result.stdout_tail[-4000:]
                        or f"direct command {result.status} (exit_code={result.exit_code})"
                    ),
                )
            return f"direct command {result.status} (exit_code={result.exit_code})"

        return runner

    @staticmethod
    def _path_within(candidate: Path, root: Path) -> bool:
        candidate = candidate.expanduser().resolve(strict=False)
        root = root.expanduser().resolve(strict=False)
        return candidate == root or root in candidate.parents

    @staticmethod
    def _map_target_to_worktree(target_path: str, repo_root: str, worktree: str) -> str:
        """Map a canonical owner-repo target into its verified worktree."""

        repo = Path(repo_root).resolve()
        target = Path(target_path).resolve(strict=False)
        isolated = Path(worktree).resolve()
        if target == isolated or isolated in target.parents:
            mapped = target
        else:
            try:
                relative = target.relative_to(repo)
            except ValueError as exc:
                raise ExecutionBlocked(
                    "WORKSPACE_DENIED",
                    f"resolved target is outside resolved repository: {target}",
                ) from exc
            mapped = (isolated / relative).resolve(strict=False)
            if mapped != isolated and isolated not in mapped.parents:
                raise ExecutionBlocked(
                    "WORKSPACE_DENIED", f"resolved target escapes isolated worktree: {mapped}"
                )
        if not mapped.is_dir():
            # Commands use a directory cwd. A file selector is a valid repo
            # selector for all other operations, but shell/test/build must run
            # from its containing directory.
            mapped = mapped.parent
        if not mapped.is_dir() or (mapped != isolated and isolated not in mapped.parents):
            raise ExecutionBlocked(
                "WORKSPACE_DENIED", f"resolved command cwd is outside worktree: {mapped}"
            )
        return str(mapped)

    async def _ensure_isolated_worktree(
        self,
        session: RemoteSession,
        repo_root: str,
        lane: str = "",
        *,
        execution_target: str = "NEW_ISOLATED_WORKTREE",
        target_path: str | None = None,
        execution_id: str | None = None,
    ) -> tuple[str, str]:
        """Create/verify the task worktree for ``repo_root`` (Phase 1.1).

        Uses the canonical :class:`WorktreeManager` (one worktree authority).
        ``lane`` isolates parallel L1 children (each gets its own worktree).
        Raises :class:`ExecutionBlocked` on any failure; never returns the owner
        repo root.
        """
        if execution_target in ("CANONICAL_WORKTREE", "HOST"):
            return repo_root, repo_root

        if execution_id:
            # Execution initialization owns the one persistent worktree
            # creation.  Keep this short registry/git operation on the
            # execution loop; a default-executor thread here survives the
            # worker and makes pytest-asyncio loop teardown hang on Python
            # 3.14.
            binding = self.execution_worktrees.get_or_create(
                execution_id, repo_root, objective="L1 execution"
            )
            return binding.worktree_path, binding.canonical_repo_root

        key = canonical(repo_root)
        cache_key = key if not lane else f"{key}::{lane}"

        # P0-C/D: if target_path is already inside an existing linked worktree,
        # reuse it directly — no nested worktrees.  Return the *canonical main*
        # project root as repo_root so that _map_target_to_worktree, runtime
        # profile discovery, and WorktreeManager all operate on the right root.
        if execution_target in ("EXECUTION_WORKTREE", "EXISTING_WORKTREE") or target_path:
            existing = find_existing_worktree_root(target_path or key)
            if existing is not None:
                main_root = _canonical_project_root(existing)
                session.worktrees[cache_key] = str(existing)
                return str(existing), main_root

        if execution_target in ("EXECUTION_WORKTREE", "EXISTING_WORKTREE"):
            candidate = session.worktrees.get(cache_key)
            if candidate and Path(candidate).is_dir():
                return candidate, repo_root
            veya_wts = Path(key) / ".veya" / "worktrees"
            if veya_wts.is_dir():
                dirs = sorted(
                    [d for d in veya_wts.iterdir() if d.is_dir()],
                    key=lambda d: d.stat().st_mtime,
                    reverse=True,
                )
                if dirs:
                    session.worktrees[cache_key] = str(dirs[0])
                    return str(dirs[0]), repo_root

        lock = self._worktree_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            try:
                # Direct command initialization is itself the execution
                # boundary.  Keep these short repo/worktree operations on the
                # worker loop so the fast/direct path does not create a
                # default-executor thread whose shutdown can outlive the job.
                manager = WorktreeManager(key)
            except WorktreeError as exc:
                raise ExecutionBlocked(
                    "WORKSPACE_DENIED", f"cannot isolate worktree: {exc}"
                ) from exc
            task_id = self._task_id(session, key, lane)
            candidate = session.worktrees.get(cache_key)
            if not candidate:
                expected = Path(key) / ".veya" / "worktrees" / f"task-{task_id}"
                candidate = str(expected) if expected.is_dir() else None
            if candidate:
                try:
                    record = manager.status(path=candidate)
                except WorktreeError:
                    record = None
                if record is not None and canonical(record.repo_root) == key:
                    if not self._path_within(Path(record.path), Path(key)):
                        raise ExecutionBlocked("WORKSPACE_DENIED", "WORKTREE_ESCAPES_WORKSPACE")
                    session.worktrees[cache_key] = record.path
                    return record.path, record.repo_root
            try:
                record = manager.create(task_id, "remote mcp direct command")
            except WorktreeError as exc:
                raise ExecutionBlocked(
                    "WORKSPACE_DENIED", f"worktree creation blocked: {exc}"
                ) from exc
            if canonical(record.repo_root) != key:
                raise ExecutionBlocked("WORKSPACE_DENIED", "WORKTREE_REPO_IDENTITY_MISMATCH")
            if not self._path_within(Path(record.path), Path(key)):
                raise ExecutionBlocked("WORKSPACE_DENIED", "WORKTREE_ESCAPES_WORKSPACE")
            session.worktrees[key] = record.path
            return record.path, record.repo_root

    # ── L1 parallel dispatch (mechanical aggregation only) ─────────────
    async def _call_worker_dispatch(
        self,
        session: RemoteSession,
        policy: WorkspacePolicy,
        binding: ToolBinding,
        args: dict[str, Any],
        ws_binding: WorkspaceBinding,
        started: float,
    ) -> RemoteCallResult:
        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return self._fail(
                binding.name,
                session,
                RemoteErrorCode.INVALID_ARGUMENT,
                "tasks must be a non-empty list",
            )
        failure_mode = "fail_fast" if args.get("fail_fast") else "collect_all"
        parent = self.jobs.create_parent(
            session=session,
            tool=binding.name,
            binding=ws_binding,
            failure_mode=failure_mode,
        )
        children: list[Any] = []
        for index, item in enumerate(tasks):
            if not isinstance(item, dict):
                return self._fail(
                    binding.name,
                    session,
                    RemoteErrorCode.INVALID_ARGUMENT,
                    "each task must be an object {worker, task}",
                )
            raw_worker = str(item.get("worker") or "").strip()
            worker = normalize_executor_name(raw_worker)
            task_text = str(item.get("task") or "")
            if not raw_worker or worker not in _WORKER_TYPES or not task_text:
                return self._fail(
                    binding.name,
                    session,
                    RemoteErrorCode.INVALID_ARGUMENT,
                    f"task[{index}] needs a known worker ({sorted(_WORKER_TYPES)}) and a task",
                )
            children.append(
                await self._submit_worker_child(
                    session, ws_binding, parent, worker, task_text, item, index
                )
            )
        payload = {
            "accepted": True,
            "parent_execution_id": parent.execution_id,
            "child_execution_ids": [child.execution_id for child in children],
            "failure_mode": failure_mode,
            "worker_availability": worker_availability(),
            "tasks": [
                {
                    "worker_type": child.worker_type,
                    "execution_id": child.execution_id,
                    "status": child.status,
                }
                for child in children
            ],
        }
        return RemoteCallResult(
            ok=True,
            tool=binding.name,
            session_id=session.session_id,
            workspace=ws_binding.requested_realpath,
            result=self._redact(payload),
            execution_id=parent.execution_id,
            duration_ms=(time.time() - started) * 1000,
        )

    async def _submit_worker_child(
        self,
        session: RemoteSession,
        ws_binding: WorkspaceBinding,
        parent: Any,
        worker: str,
        task_text: str,
        item: dict[str, Any],
        index: int,
    ) -> Any:

        from veya.remote.execution_contract import (
            ExecutionCapabilityEnvelope,
            ExecutionSpec,
            probe_runtime_capability_manifest,
        )

        # A named worker is an exact selector.  Health/capability fallback is
        # for an orchestrator preference list, never for worker.dispatch.
        explicit_pin = True
        required_caps = item.get("required_capabilities") or item.get("capabilities") or []
        if isinstance(required_caps, (list, tuple, set)):
            required_caps = {
                ("supports_shell_effect" if str(cap) == "supports_shell" else str(cap)): True
                for cap in required_caps
            }
        task_contract = L1TaskContract.from_dict(item.get("task_contract"))
        # The contract is explicit data.  Legacy dispatch callers are retained
        # as READ tasks; WRITE/TEST/BUILD callers must opt into their stronger
        # effect and finalization requirements.
        if not item.get("task_contract"):
            requested_kind = str(item.get("task_kind") or "").upper()
            if not requested_kind:
                has_write_effect = any(
                    item.get(key) not in (None, "", "NONE")
                    for key in (
                        "effect_requirement",
                        "verification_requirement",
                        "commit_requirement",
                        "promotion_policy",
                        "verification_command",
                    )
                )
                requested_kind = str(TaskKind.WRITE if has_write_effect else TaskKind.READ)
            task_contract = L1TaskContract(
                task_kind=requested_kind,
                effect_requirement=str(item.get("effect_requirement") or "NONE"),
                verification_requirement=str(item.get("verification_requirement") or "NONE"),
                commit_requirement=str(item.get("commit_requirement") or "NONE"),
                promotion_policy=str(item.get("promotion_policy") or "NONE"),
                allowed_files=[str(v) for v in item.get("allowed_files") or []],
                allowed_roots=[str(v) for v in item.get("allowed_roots") or []],
                allow_noop=bool(item.get("allow_noop", False)),
                verification_command=(
                    str(item["verification_command"])
                    if item.get("verification_command") is not None
                    else None
                ),
            )

        # 1. load ExecutionSpec
        spec = ExecutionSpec(
            objective=task_text,
            executor_requirements={
                "preferred_executor": worker,
                "required_capabilities": required_caps,
                "explicit_pin": explicit_pin,
            },
            workspace=ws_binding.requested_realpath,
            capabilities=ExecutionCapabilityEnvelope(
                filesystem={"allowed_roots": [ws_binding.requested_realpath]},
                network={"mode": "ALLOWLIST"},
                tools={"allowed_tools": []},
                mcp_servers={},
                skills={},
                credentials={},
                compute={},
                workspace=ws_binding.requested_realpath,
                runtime={},
            ),
            resources={},
            supervision={},
            continuation=None,
            task_contract=task_contract.to_dict(),
        )

        # 2. load RuntimeCapabilityManifest & 3-7 choose executor
        selected_worker, sub_evidence = resolve_executor(
            requested=worker,
            explicit_pin=explicit_pin,
            required_capabilities=required_caps,
            health_registry=self.health_registry,
        )

        manifest_snapshot = probe_runtime_capability_manifest(
            selected_worker,
            workspace_path=ws_binding.requested_realpath,
            health_registry=self.health_registry,
            active_executions=len([t for t in self.jobs._tasks.values() if not t.done()]),
        )
        if (
            isinstance(required_caps, dict)
            and required_caps.get("supports_shell_effect")
            and not manifest_snapshot.supports_shell
        ):
            return self._submit_blocked_child(
                session,
                ws_binding,
                parent,
                _WORKER_TYPES[selected_worker],
                selected_worker,
                "REQUIRED_CAPABILITY_UNAVAILABLE",
            )

        # 8. persist decision
        if sub_evidence is not None:
            self.jobs.record_event(
                parent.execution_id,
                kind="EXECUTOR_SUBSTITUTION",
                message=json.dumps(
                    {
                        "requested_executor": worker,
                        "selected_executor": selected_worker,
                        "selection_reason": sub_evidence.to_dict(),
                        "manifest_snapshot": manifest_snapshot.__dict__,
                    }
                ),
            )
        else:
            self.jobs.record_event(
                parent.execution_id,
                kind="ROUTING_DECISION",
                message=json.dumps(
                    {
                        "requested_executor": worker,
                        "selected_executor": selected_worker,
                        "selection_reason": "direct match or pin",
                        "manifest_snapshot": manifest_snapshot.__dict__,
                    }
                ),
            )

        worker = selected_worker
        worker_type = _WORKER_TYPES[worker]
        if (
            task_contract.task_kind == str(TaskKind.WRITE)
            and not capabilities_for(worker).supports_write_task
        ):
            reason = (
                "PINNED_EXECUTOR_NOT_WRITE_QUALIFIED"
                if explicit_pin
                else "WORKER_NOT_WRITE_QUALIFIED"
            )
            return self._submit_blocked_child(
                session, ws_binding, parent, worker_type, worker, reason
            )
        blocker = _WORKER_BLOCKERS.get(worker)
        lane = f"child-{parent.execution_id[-8:]}-{index}"
        if blocker:
            return self._submit_blocked_child(
                session, ws_binding, parent, worker_type, worker, blocker
            )
        # Only real, qualified workers reach a live child; each child gets its
        # own isolated worktree lane.
        repo_root = (
            ws_binding.repo_root if ws_binding.is_git_repo else ws_binding.requested_realpath
        )
        retry_action = str(item.get("retry_action") or "")
        retry_error = str(item.get("retry_error_class") or "")
        dependency_artifacts = list((item.get("inputs") or {}).get("dependency_artifacts") or [])
        if item.get("retry") and retry_action and retry_error:
            memory = TaskMemory(repo_root, str(item.get("mission_id") or parent.execution_id))
            if memory.should_skip_repeat(action=retry_action, error_class=retry_error):
                return self._submit_blocked_child(
                    session, ws_binding, parent, worker_type, worker, "ERROR_REPETITION_GUARD"
                )
        provider, model = _worker_model_identity(worker)
        if worker == "hicode":
            child = self.jobs.submit(
                session=session,
                tool="hicode.execute",
                veya_tool="hicode_run",
                binding=ws_binding,
                spec=spec,
                runner=self._make_hicode_runner(
                    task=task_text,
                    session=session,
                    ws_binding=ws_binding,
                    repo_root=repo_root,
                    args={"timeout_sec": int(item.get("timeout_sec") or 0)},
                    capability_ids=[str(v) for v in item.get("capability_ids", [])],
                    mission_id=str(item.get("mission_id") or parent.execution_id),
                    permission_policy=dict(item.get("mission_policy") or {}),
                    retry_action=retry_action,
                    retry_error_class=retry_error,
                    task_contract=task_contract,
                    lane=lane,
                ),
                execution_type=str(ExecutionType.HICODE),
                execution_mode="direct_hicode",
                orchestrator="none",
                worker_type=worker_type,
                model_provider=provider,
                model=model,
                parent_execution_id=parent.execution_id,
                task_contract=task_contract.to_dict(),
            )
        else:
            requested_timeout_s = float(item.get("timeout_sec") or _DEFAULT_CLI_TIMEOUT_S)
            _, effective_timeout_s = _cli_worker_timeout_budgets(worker, requested_timeout_s)
            child = self.jobs.submit(
                session=session,
                tool="worker.dispatch",
                veya_tool=f"direct_{worker}",
                binding=ws_binding,
                spec=spec,
                runner=self._make_cli_worker_runner(
                    worker=worker,
                    task=task_text,
                    session=session,
                    ws_binding=ws_binding,
                    repo_root=repo_root,
                    lane=lane,
                    timeout_s=effective_timeout_s,
                    capability_ids=[str(v) for v in item.get("capability_ids", [])],
                    mission_id=str(item.get("mission_id") or parent.execution_id),
                    permission_policy=dict(item.get("mission_policy") or {}),
                    retry_action=retry_action,
                    retry_error_class=retry_error,
                    task_contract=task_contract,
                    dependency_artifacts=dependency_artifacts,
                ),
                execution_type=str(ExecutionType.DIRECT),
                execution_mode=f"direct_{worker}",
                orchestrator="none",
                worker_type=worker_type,
                model_provider=provider,
                model=model,
                parent_execution_id=parent.execution_id,
                task_contract=task_contract.to_dict(),
                limits={
                    "execution_timeout_sec": effective_timeout_s,
                    "effective_timeout_ms": int(effective_timeout_s * 1000),
                },
            )
        self.jobs.attach_child(parent.execution_id, child.execution_id)
        return child

    def _submit_blocked_child(
        self,
        session: RemoteSession,
        ws_binding: WorkspaceBinding,
        parent: Any,
        worker_type: str,
        worker: str,
        blocker: str,
    ) -> Any:
        async def blocked_runner(reporter: ProgressReporter) -> str:
            if blocker == "ERROR_REPETITION_GUARD":
                reporter.event(
                    json.dumps(
                        {
                            "identical_retry_attempted": True,
                            "identical_retry_dispatched": False,
                            "guard": "LIVE_ERROR_REPETITION_GUARD",
                        },
                        sort_keys=True,
                    ),
                    kind="RETRY_BLOCKED",
                )
                reporter.failure(
                    failure_class="ERROR_REPETITION_GUARD",
                    source="task_memory",
                    detail="identical unresolved action/error signature",
                )
                raise ExecutionBlocked("ERROR_REPETITION_GUARD", blocker)
            raise ExecutionBlocked("WORKER_UNAVAILABLE", blocker)

        child = self.jobs.submit(
            session=session,
            tool="worker.dispatch",
            veya_tool=f"direct_{worker}",
            binding=ws_binding,
            runner=blocked_runner,
            execution_type=str(ExecutionType.DIRECT),
            execution_mode=f"direct_{worker}",
            orchestrator="none",
            worker_type=worker_type,
            parent_execution_id=parent.execution_id,
        )
        self.jobs.attach_child(parent.execution_id, child.execution_id)
        return child

    # ── direct_hicode (P1-B/H/I) ──────────────────────────────
    async def _call_direct_hicode(
        self,
        session: RemoteSession,
        policy: WorkspacePolicy,
        binding: ToolBinding,
        args: dict[str, Any],
        ws_binding: WorkspaceBinding,
        started: float,
    ) -> RemoteCallResult:
        name = binding.name
        requested_mode = str(args.get("execution_mode") or "direct_hicode")
        if requested_mode != "direct_hicode":
            return self._fail(
                name,
                session,
                RemoteErrorCode.INVALID_ARGUMENT,
                f"unsupported execution_mode {requested_mode!r}; this gateway implements "
                "'direct_hicode' (no auto routing)",
            )
        hicode_workspace = (
            ws_binding.repo_root if ws_binding.is_git_repo else ws_binding.requested_realpath
        )
        provider, model = _hicode_model_identity()
        record = self.jobs.submit(
            session=session,
            tool=name,
            veya_tool="hicode_run",
            binding=ws_binding,
            runner=self._make_hicode_runner(
                task=str(args["task"]),
                session=session,
                ws_binding=ws_binding,
                repo_root=hicode_workspace,
                args=args,
            ),
            execution_type=str(ExecutionType.HICODE),
            execution_mode="direct_hicode",
            orchestrator="none",
            worker_type="HICODE",
            model_provider=provider,
            model=model,
            limits=_execution_limits(name, args),
        )
        # P1-B: durable enqueue -> immediate execution_id; never a long RPC.
        snapshot = dict(
            self._redact(record.to_public(heartbeat_timeout_s=self.jobs.heartbeat_timeout_s))
        )
        snapshot["accepted"] = True
        return RemoteCallResult(
            ok=True,
            tool=name,
            session_id=session.session_id,
            workspace=ws_binding.requested_realpath,
            result=self._redact(snapshot),
            execution_id=record.execution_id,
            duration_ms=(time.time() - started) * 1000,
        )

    def _make_hicode_runner(
        self,
        *,
        task: str,
        session: RemoteSession,
        ws_binding: WorkspaceBinding,
        repo_root: str,
        args: dict[str, Any],
        capability_ids: list[str] | None = None,
        mission_id: str = "",
        permission_policy: dict[str, Any] | None = None,
        retry_action: str = "",
        retry_error_class: str = "",
        lane: str = "",
        task_contract: L1TaskContract | None = None,
    ) -> Any:
        async def _run(reporter: ProgressReporter) -> str:
            reporter.phase("STARTING", message="starting Hicode worker", event="STARTING")
            if task_contract and task_contract.promotion_policy == "AUTO_AFTER_VERIFY":
                await self._ensure_promotion_ledger()
            reporter.worker(
                execution_mode="direct_hicode",
                orchestrator="none",
                worker_type="HICODE",
                activity="Hicode worker started",
            )
            # Section 7: Hicode runs in the verified isolated task worktree, never
            # the owner repo. The worker receives exactly the validated workspace.
            worktree, verified_repo = await self._ensure_isolated_worktree(
                session, repo_root, lane, execution_id=reporter._execution_id
            )
            reporter.set_worktree(worktree, verified_repo)
            reporter.worker(
                execution_mode="direct_hicode",
                orchestrator="none",
                worker_type="HICODE",
                activity=f"isolated worktree ready: {worktree}",
                worker_workspace=worktree,
            )
            from server.hicode_agent import bound_hicode_execution_id, bound_hicode_workspace

            with (
                bound_hicode_workspace(worktree),
                bound_hicode_execution_id(reporter._execution_id),
            ):
                try:
                    _ensure_hicode_workspace(worktree)
                except WorkspaceBindingError as exc:
                    raise ExecutionBlocked(exc.code, exc.message) from exc
            reporter.event(f"checkpoint snapshot {worktree}", kind="CHECKPOINT")
            _context, rendered_context, memory = await self._prepare_execution_context(
                reporter,
                task=task,
                worker_type="hicode",
                worktree=worktree,
                memory_root=repo_root,
                mission_id=mission_id or reporter._execution_id,
                capability_ids=capability_ids or [str(v) for v in args.get("capability_ids", [])],
                permission_policy=permission_policy or dict(args.get("mission_policy") or {}),
            )
            worker_input = f"{task}\n\n{rendered_context}"
            in_flight = {"value": False}
            owned: dict[str, int | None] = {"pid": None, "pgid": None}

            def on_process(pid: int, pgid: int) -> None:
                owned["pid"] = pid
                owned["pgid"] = pgid
                reporter.process(worker_pid=pid, process_group_id=pgid)

            def on_event(event: dict[str, Any]) -> None:
                stage = str(event.get("stage") or "")
                detail = str(event.get("detail") or "")
                tool = event.get("tool")
                if stage == "provider_failure":
                    if in_flight["value"]:
                        in_flight["value"] = False
                        reporter.model_completed(activity="model round failed")
                    round_index = event.get("round_index")
                    reporter.failure(
                        failure_class=str(event.get("code") or "HICODE_PROVIDER_ROUND_FAILURE"),
                        source="hicode_provider",
                        detail=detail or "structured provider failure",
                        code=str(event.get("code") or "HICODE_PROVIDER_ROUND_FAILURE"),
                        raw_evidence=event.get("raw_evidence")
                        if isinstance(event.get("raw_evidence"), dict)
                        else None,
                        round_index=round_index
                        if isinstance(round_index, int) and not isinstance(round_index, bool)
                        else None,
                    )
                elif stage == "planning":
                    if not in_flight["value"]:
                        in_flight["value"] = True
                        reporter.model_started(activity=detail or "Hicode planning")
                    reporter.phase(
                        "THINKING",
                        message=detail or "Hicode thinking",
                        event="MODEL_REQUEST_STARTED",
                    )
                elif stage == "executing":
                    completed = "完成" in detail
                    if not completed and in_flight["value"]:
                        in_flight["value"] = False
                        reporter.model_completed(activity="model returned a tool call")
                    reporter.tool_activity(
                        activity=detail or f"tool {tool}",
                        kind="TOOL_COMPLETED" if completed else "TOOL_STARTED",
                        count=not completed,
                    )
                    reporter.phase(
                        "EXECUTING",
                        message=detail or "Hicode executing",
                        event="TOOL_COMPLETED" if completed else "TOOL_STARTED",
                    )
                elif stage == "stats":
                    if in_flight["value"]:
                        in_flight["value"] = False
                        reporter.model_completed(activity=detail or "model responded")
                elif detail:
                    reporter.event(detail, kind=stage or "activity")

            try:
                # Direct worker invocation (not the mainline queue) so cancel
                # propagates into the Hicode loop / child process. force_cli is
                # required so the workspace actually constrains execution.
                # The L1 worktree is already session-authorized and isolated;
                # bind Hicode's resolver to this child without changing the
                # process-global HICODE_WORKSPACE policy.
                with (
                    bound_hicode_workspace(worktree),
                    bound_hicode_execution_id(reporter._execution_id),
                ):
                    from server.hicode_agent import (
                        HicodeExecutionError,
                        HicodeUnavailable,
                        _execute_hicode_core,
                    )

                    output = await _execute_hicode_core(
                        worker_input,
                        workspace=worktree,
                        max_steps=int(args.get("max_steps") or 0),
                        timeout_sec=int(args.get("timeout_sec") or 0),
                        on_event=on_event,
                        force_cli=True,
                        on_process=on_process,
                    )
            except HicodeExecutionError as exc:
                fc = classify_executor_failure(error=exc, detail=f"{exc.code}: {exc.detail}")
                self.health_registry.record_failure("hicode", fc, detail=exc.detail)
                failure_cls = str(exc.code) if exc.code else str(fc)
                reporter.failure(
                    failure_class=failure_cls,
                    source="hicode_provider",
                    detail=exc.detail,
                    code=exc.code,
                    raw_evidence=exc.raw_evidence,
                )
                self._write_task_memory_failure(
                    memory, reporter._execution_id, exc.detail, error_class=exc.code
                )
                raise ExecutionError(exc.code, exc.detail) from exc
            except HicodeUnavailable as exc:
                detail = str(exc)[:4000]
                fc = ExecutorFailureClass.PROVIDER_UNAVAILABLE
                self.health_registry.record_failure("hicode", fc, detail=detail)
                reporter.failure(
                    failure_class=str(fc),
                    source="hicode_runtime",
                    detail=detail,
                    code="HICODE_RUNTIME_UNAVAILABLE",
                    raw_evidence={"exception_type": type(exc).__name__, "detail": detail},
                )
                self._write_task_memory_failure(
                    memory,
                    reporter._execution_id,
                    detail,
                    error_class="HICODE_RUNTIME_UNAVAILABLE",
                )
                raise ExecutionError("HICODE_RUNTIME_UNAVAILABLE", detail) from exc
            except asyncio.CancelledError:
                # Kill exactly this execution's process group (reasonix + its
                # tool grandchildren); never the shared runtime/gateway.
                self.health_registry.record_failure(
                    "hicode", ExecutorFailureClass.WORKER_CANCELLED, detail="cancelled"
                )
                await terminate_process_group_id(owned["pgid"] or 0)
                raise
            except ExecutionError:
                # Preserve the canonical Hicode/provider error. Do not re-wrap it.
                raise
            except Exception as exc:
                fc = classify_executor_failure(error=exc, detail=f"{type(exc).__name__}: {exc}")
                self.health_registry.record_failure("hicode", fc, detail=str(exc))
                reporter.failure(
                    failure_class=str(fc),
                    source="hicode",
                    detail=f"{type(exc).__name__}: {exc}",
                )
                self._write_task_memory_failure(
                    memory,
                    reporter._execution_id,
                    exc,
                    action=retry_action,
                    error_class=retry_error_class,
                )
                raise
            finally:
                if in_flight["value"]:
                    reporter.model_completed(activity="model request finished")
            self.health_registry.record_success("hicode")
            reporter.phase("FINALIZING", message="Hicode finalizing", event="FINALIZING")
            contract = task_contract or L1TaskContract()
            if contract.task_kind in {
                str(TaskKind.WRITE),
                str(TaskKind.TEST),
                str(TaskKind.BUILD),
            }:
                receipt = EffectReceipt(
                    execution_id=reporter._execution_id,
                    worker_runtime_id=None,
                    worker_type="HICODE",
                    repo_identity=git_repo_identity(verified_repo),
                    worktree_path=worktree,
                    task_kind=contract.task_kind,
                    tool_calls=[],
                )
                finalization = await run_sync_in_daemon_thread(
                    self.l1_finalizer.finalize,
                    execution_id=reporter._execution_id,
                    repo=verified_repo,
                    task_contract=contract,
                    worker_result=WorkerResult(
                        textual_summary=output,
                        process_exit_code=0,
                        effect_receipt=receipt,
                    ),
                )
                reporter.finalization(finalization.to_dict())
                reporter.event(
                    json.dumps(finalization.to_dict(), default=str), kind="L1_FINALIZATION"
                )
                if not finalization.ok:
                    reporter.failure(
                        failure_class=finalization.failure_class or "FINALIZATION_FAILED",
                        source="hicode",
                        detail=finalization.message,
                    )
                    raise ExecutionBlocked(
                        finalization.failure_class or "FINALIZATION_FAILED",
                        finalization.message,
                    )
                output = (
                    f"{output}\nchanged_files={','.join(finalization.receipt.changed_files)}"
                    f"\ncommit_sha={finalization.commit_sha or ''}"
                    f"\npromotion_status={finalization.promotion_status}"
                )[-4000:]
            self._write_task_memory_success(memory, reporter._execution_id, output)
            return output

        return self._lease_wrapped_runner(
            _run,
            ws_binding=ws_binding,
            task_contract=task_contract,
            worker_type="hicode",
        )

    def _lease_wrapped_runner(
        self,
        runner: Any,
        *,
        ws_binding: WorkspaceBinding,
        task_contract: L1TaskContract | None,
        worker_type: str,
    ) -> Any:
        """Serialize WRITE/TEST/BUILD workers for one execution/repository."""

        async def wrapped(reporter: ProgressReporter) -> str:
            contract = task_contract or L1TaskContract()
            needs_writer = contract.task_kind in {
                str(TaskKind.WRITE),
                str(TaskKind.TEST),
                str(TaskKind.BUILD),
            }
            lease = None
            if needs_writer:
                lease = await run_sync_in_daemon_thread(
                    self.execution_worktrees.acquire_lease,
                    reporter._execution_id,
                    ws_binding.repo_identity,
                    f"{worker_type}:{reporter._execution_id}",
                )
            try:
                return await runner(reporter)
            finally:
                if lease is not None:
                    await run_sync_in_daemon_thread(self.execution_worktrees.release_lease, lease)

        return wrapped

    # ── generic CLI worker (DSH / Pi / Grok) ───────────────────────
    def _make_cli_worker_runner(
        self,
        *,
        worker: str,
        task: str,
        session: RemoteSession,
        ws_binding: WorkspaceBinding,
        repo_root: str,
        lane: str,
        timeout_s: float,
        capability_ids: list[str] | None = None,
        mission_id: str = "",
        permission_policy: dict[str, Any] | None = None,
        retry_action: str = "",
        retry_error_class: str = "",
        dependency_artifacts: list[dict[str, Any]] | None = None,
        task_contract: L1TaskContract | None = None,
    ) -> Any:
        async def _run(reporter: ProgressReporter) -> str:
            reporter.phase("STARTING", message=f"starting {worker} worker", event="STARTING")
            if task_contract and task_contract.promotion_policy == "AUTO_AFTER_VERIFY":
                await self._ensure_promotion_ledger()
            reporter.worker(
                execution_mode=f"direct_{worker}",
                orchestrator="none",
                worker_type=worker.upper(),
                activity=f"{worker} worker started",
            )
            worktree, verified_repo = await self._ensure_isolated_worktree(
                session, repo_root, lane, execution_id=reporter._execution_id
            )
            staged_dependency_artifacts = await run_sync_in_daemon_thread(
                _stage_dependency_artifacts,
                worktree,
                repo_root,
                dependency_artifacts or [],
            )
            reporter.set_worktree(worktree, verified_repo)
            reporter.worker(
                execution_mode=f"direct_{worker}",
                orchestrator="none",
                worker_type=worker.upper(),
                activity=f"isolated worktree ready: {worktree}",
                worker_workspace=worktree,
            )
            context, rendered_context, memory = await self._prepare_execution_context(
                reporter,
                task=task,
                worker_type=worker,
                worktree=worktree,
                memory_root=repo_root,
                mission_id=mission_id or reporter._execution_id,
                capability_ids=capability_ids or [],
                permission_policy=permission_policy or {},
            )
            dependency_context = ""
            if staged_dependency_artifacts:
                dependency_context = (
                    "\n\nDependency artifacts are available only at these staged paths; "
                    "do not read any other worktree, owner-repo, or absolute source path:\n"
                    + json.dumps(staged_dependency_artifacts, ensure_ascii=False, sort_keys=True)
                )
            worker_input = f"{task}{dependency_context}\n\n{rendered_context}"
            coding_mode = (task_contract or L1TaskContract()).task_kind in {
                str(TaskKind.WRITE),
                str(TaskKind.TEST),
                str(TaskKind.BUILD),
            }
            opencode_agent = _resolve_opencode_agent() if worker == "opencode" else None
            if coding_mode and worker == "opencode" and not opencode_agent:
                raise ExecutionBlocked(
                    "OPENCODE_WRITE_CAPABILITY_NOT_QUALIFIED",
                    "VEYA_OPENCODE_AGENT is not configured; coding agent qualification is required",
                )
            argv, env = _worker_command(
                worker,
                worker_input,
                worktree_path=worktree,
                coding_mode=coding_mode,
                agent=opencode_agent,
            )
            env["VEYA_EXECUTION_CONTEXT_HASH"] = context_hash(context)
            env["VEYA_EXECUTION_ID"] = reporter._execution_id
            reporter.model_started(activity=f"{worker} model request")
            reporter.phase("THINKING", message=f"{worker} thinking", event="MODEL_REQUEST_STARTED")
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=worktree,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                pgid = os.getpgid(proc.pid)
            except OSError:
                pgid = proc.pid
            reporter.process(worker_pid=proc.pid, process_group_id=pgid)
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            activity_event = asyncio.Event()
            last_activity = [time.monotonic()]

            async def pump(stream: Any, name: str, sink: list[str] | None) -> None:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", "replace")
                    last_activity[0] = time.monotonic()
                    activity_event.set()
                    reporter.output(name, text)
                    if sink is not None:
                        sink.append(text)

            async def heartbeat() -> None:
                while proc.returncode is None:
                    await asyncio.sleep(2.0)
                    if proc.returncode is None:
                        self.health_registry.set_heartbeat(worker, alive=True)
                        reporter.phase(
                            "THINKING",
                            message=f"{worker} process alive",
                            event="WORKER_HEARTBEAT",
                        )

            try:
                pump_tasks = {
                    asyncio.create_task(pump(proc.stdout, "stdout", stdout_lines)),
                    asyncio.create_task(pump(proc.stderr, "stderr", stderr_lines)),
                }
                process_task = asyncio.create_task(proc.wait())
                heartbeat_task = asyncio.create_task(heartbeat())
                all_tasks = pump_tasks | {process_task}
                inactivity_timeout_s, hard_max_runtime_s = _cli_worker_timeout_budgets(
                    worker, timeout_s
                )
                await _wait_for_cli_process(
                    proc,
                    all_tasks,
                    activity_event=activity_event,
                    last_activity=last_activity,
                    inactivity_timeout_s=inactivity_timeout_s,
                    hard_max_runtime_s=hard_max_runtime_s,
                )
            except _CLIWorkerTimeout as exc:
                await terminate_process_group_id(pgid)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                reporter.model_completed(activity=f"{worker} model request timed out")
                timeout_detail = (
                    f"timeout_layer=Veya runner; timeout_kind={exc.kind}; "
                    f"inactivity_timeout_ms={exc.timeout_s * 1000:.0f}; "
                    f"hard_max_runtime_ms={exc.hard_max_s * 1000:.0f}"
                )
                reporter.event(timeout_detail, kind="WORKER_TIMEOUT_EVIDENCE")
                fc = ExecutorFailureClass.WORKER_TIMEOUT
                self.health_registry.record_failure(worker, fc, detail=timeout_detail)
                reporter.failure(failure_class=str(fc), source=worker, detail=timeout_detail)
                self._write_task_memory_failure(
                    memory,
                    reporter._execution_id,
                    timeout_detail,
                    action=retry_action,
                    error_class=retry_error_class,
                )
                raise ExecutionBlocked("TIMEOUT", timeout_detail) from None
            except TimeoutError:
                await terminate_process_group_id(pgid)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                reporter.model_completed(activity=f"{worker} model request timed out")
                timeout_detail = (
                    f"timeout_layer=Veya runner; hard_max_runtime_ms={timeout_s * 1000:.0f}"
                )
                reporter.event(timeout_detail, kind="WORKER_TIMEOUT_EVIDENCE")
                fc = ExecutorFailureClass.WORKER_TIMEOUT
                self.health_registry.record_failure(worker, fc, detail=timeout_detail)
                reporter.failure(failure_class=str(fc), source=worker, detail=timeout_detail)
                self._write_task_memory_failure(
                    memory,
                    reporter._execution_id,
                    timeout_detail,
                    action=retry_action,
                    error_class=retry_error_class,
                )
                raise ExecutionBlocked("TIMEOUT", timeout_detail) from None
            except asyncio.CancelledError:
                await terminate_process_group_id(pgid)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                fc = ExecutorFailureClass.WORKER_CANCELLED
                self.health_registry.record_failure(worker, fc, detail="cancelled")
                reporter.failure(failure_class=str(fc), source=worker, detail="cancelled")
                raise
            finally:
                if "heartbeat_task" in locals():
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
            reporter.model_completed(activity=f"{worker} model finished")
            if proc.returncode not in (0, None):
                stderr_tail = "".join(stderr_lines).strip()[-1200:]
                stdout_tail = "".join(stdout_lines).strip()[-1200:]
                detail = f"exit_code={proc.returncode}; stderr={stderr_tail}; stdout={stdout_tail}".strip()
                fc = classify_executor_failure(exit_code=proc.returncode, detail=detail)
                self.health_registry.record_failure(worker, fc, detail=detail)
                reporter.failure(failure_class=str(fc), source=worker, detail=detail)
                self._write_task_memory_failure(
                    memory,
                    reporter._execution_id,
                    detail,
                    action=retry_action,
                    error_class=retry_error_class,
                )
                raise ExecutionError("WORKER_FAILED", f"{worker} exited with {proc.returncode}")
            self.health_registry.record_success(worker)
            changed = await self._worktree_changed(worktree)
            if changed:
                reporter.tool_activity(
                    activity=f"{worker} changed {len(changed)} file(s)",
                    kind="TOOL_COMPLETED",
                    count=True,
                )
                for path in changed:
                    reporter.event(path, kind="ARTIFACT_CREATED")
            reporter.phase("FINALIZING", message=f"{worker} finalizing", event="FINALIZING")
            summary = "".join(stdout_lines).strip()
            if not summary:
                summary = f"{worker} exit={proc.returncode}"
            summary = summary[-4000:]
            receipt = EffectReceipt(
                execution_id=reporter._execution_id,
                worker_runtime_id=None,
                worker_type=worker.upper(),
                repo_identity=git_repo_identity(verified_repo),
                worktree_path=worktree,
                task_kind=(task_contract.task_kind if task_contract else str(TaskKind.READ)),
                tool_calls=_worker_tool_events(worker, stdout_lines),
                shell_calls=_worker_shell_events(worker, stdout_lines),
                file_writes=changed if worker == "opencode" else [],
            )
            contract = task_contract or L1TaskContract()
            if contract.task_kind in {
                str(TaskKind.WRITE),
                str(TaskKind.TEST),
                str(TaskKind.BUILD),
            }:
                finalization = await run_sync_in_daemon_thread(
                    self.l1_finalizer.finalize,
                    execution_id=reporter._execution_id,
                    repo=verified_repo,
                    task_contract=contract,
                    worker_result=WorkerResult(
                        textual_summary=summary,
                        process_exit_code=proc.returncode,
                        effect_receipt=receipt,
                    ),
                )
                reporter.finalization(finalization.to_dict())
                reporter.event(
                    json.dumps(finalization.to_dict(), default=str), kind="L1_FINALIZATION"
                )
                if not finalization.ok:
                    reporter.failure(
                        failure_class=finalization.failure_class or "FINALIZATION_FAILED",
                        source=worker,
                        detail=finalization.message,
                    )
                    raise ExecutionBlocked(
                        finalization.failure_class or "FINALIZATION_FAILED",
                        finalization.message,
                    )
                summary = (
                    f"{summary}\nchanged_files={','.join(finalization.receipt.changed_files)}"
                    f"\ncommit_sha={finalization.commit_sha or ''}"
                    f"\npromotion_status={finalization.promotion_status}"
                )[-4000:]
            self._write_task_memory_success(memory, reporter._execution_id, summary)
            return summary

        return self._lease_wrapped_runner(
            _run,
            ws_binding=ws_binding,
            task_contract=task_contract,
            worker_type=worker,
        )

    async def _prepare_execution_context(
        self,
        reporter: ProgressReporter,
        *,
        task: str,
        worker_type: str,
        worktree: str,
        memory_root: str,
        mission_id: str,
        capability_ids: list[str],
        permission_policy: dict[str, Any],
    ) -> tuple[ExecutionCapabilityContext, str, TaskMemory]:
        """Build and inject one live context for one worker execution."""

        policy = _normalize_mission_policy(permission_policy)
        memory = TaskMemory(memory_root, mission_id)
        builder = ExecutionContextBuilder(
            capability_registry=shared_capability_registry(),
            skill_registry=shared_skill_registry(),
            task_memory=memory,
            permission_policy=policy,
        )
        context, evidence = await builder.build(
            mission_id=mission_id,
            execution_id=reporter._execution_id,
            worker_type=worker_type,
            workspace=worktree,
            capability_ids=capability_ids,
            task_text=task,
        )
        capability_results: list[dict[str, Any]] = []
        for capability_id in capability_ids:
            try:
                result = await shared_capability_registry().invoke(
                    capability_id,
                    {"objective": task, "worker_type": worker_type, "workspace": worktree},
                )
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                reporter.failure(
                    failure_class="CAPABILITY_INVOCATION_FAILED",
                    source="capability",
                    detail=detail,
                )
                memory.record_error(
                    action=f"invoke {capability_id}",
                    error_class="CAPABILITY_INVOCATION_FAILED",
                    attempt=1,
                    evidence=detail,
                )
                raise ExecutionBlocked("CAPABILITY_UNAVAILABLE", detail) from exc
            capability_results.append(result)
            reporter.event(
                json.dumps(
                    {
                        "capability_id": capability_id,
                        "selected_provider": result.get("selected_provider"),
                        "health": result.get("health"),
                        "probe_evidence": result.get("probe_evidence"),
                        "rejected_providers": result.get("routing", {}).get("rejected", []),
                    },
                    sort_keys=True,
                    default=str,
                ),
                kind="CAPABILITY_INVOKED",
            )
        if capability_results:
            context.project_context["capability_results"] = capability_results
        rendered = render_execution_context(context)
        rendered_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        input_hash = hashlib.sha256(f"{task}\n\n{rendered}".encode()).hexdigest()
        budget: dict[str, Any] = dict(context_budget(context))
        budget["worker_input_context_hash"] = input_hash
        budget["rendered_context_hash"] = rendered_hash
        reporter.execution_context(
            context_id=f"ctx_{reporter._execution_id}",
            context_hash=context_hash(context),
            budget=budget,
            capability_ids=[c["capability_id"] for c in context.selected_capabilities],
            skill_ids=[s["skill_id"] for s in context.selected_skills],
        )
        reporter.event(
            json.dumps(
                {
                    "execution_context_id": f"ctx_{reporter._execution_id}",
                    "execution_context_hash": context_hash(context),
                    "rendered_context_hash": rendered_hash,
                    "worker_input_context_hash": input_hash,
                    "selected_capabilities": context.selected_capabilities,
                    "selected_skills": evidence["selected_skills"],
                    "selected_skill_provenance": context.selected_skills,
                    "declined_skills": evidence["declined_skills"],
                    "skill_level2_loaded": bool(context.selected_skills),
                },
                sort_keys=True,
                default=str,
            ),
            kind="EXECUTION_CONTEXT_CONSUMED",
        )
        if evidence["declined_skills"]:
            reporter.failure(
                failure_class="SKILL_PERMISSION_DENIED",
                source="skill",
                detail=json.dumps(evidence["declined_skills"], default=str),
            )
            memory.record_error(
                action=f"load skill for {worker_type}",
                error_class="SKILL_PERMISSION_DENIED",
                attempt=1,
                evidence=json.dumps(evidence["declined_skills"], default=str),
            )
            raise ExecutionBlocked(
                "SKILL_PERMISSION_DENIED", "selected skill denied by mission policy"
            )
        return context, rendered, memory

    @staticmethod
    def _write_task_memory_success(memory: TaskMemory, execution_id: str, output: str) -> None:
        memory.add_progress(f"worker {execution_id} completed")
        memory.add_finding("worker completed", evidence=f"execution:{execution_id}")
        memory.record_worker_event(
            execution_id,
            {"kind": "completed", "evidence": _short(output, 800)},
        )

    @staticmethod
    def _write_task_memory_failure(
        memory: TaskMemory,
        execution_id: str,
        error: Any,
        *,
        action: str = "",
        error_class: str = "",
    ) -> None:
        detail = _short(str(error), 800)
        memory.record_error(
            action=action or f"worker:{execution_id}",
            error_class=error_class
            or (type(error).__name__ if isinstance(error, Exception) else "WORKER_FAILED"),
            attempt=1,
            evidence=detail,
        )
        memory.record_worker_event(
            execution_id,
            {"kind": "failed", "evidence": detail},
        )

    async def _worktree_changed(self, worktree: str) -> list[str]:
        code, out, _err = await self._run_capture(
            ["git", "-C", worktree, "status", "--porcelain"], timeout=20
        )
        if code not in (0, None):
            return []
        changed: list[str] = []
        for line in out.splitlines():
            if line.strip():
                changed.append(line[3:].strip() if len(line) > 3 else line.strip())
        return changed

    # ── fast read-only git (P0-A) ──────────────────────────────────
    async def _call_fast_git(
        self,
        session: RemoteSession,
        name: str,
        args: dict[str, Any],
        ws_binding: WorkspaceBinding,
        started: float,
        *,
        resolution: RepoResolution,
    ) -> RemoteCallResult:
        target = self._direct_workdir(
            session, ws_binding, execution_id=str(args.get("execution_id") or "")
        )
        try:
            if name == "git.status":
                code, out, err = await self._run_capture(
                    ["git", "-C", target, "status", "--porcelain=v1", "--branch"], timeout=20
                )
                payload = self._parse_git_status(out)
                payload["exit_code"] = code
            elif name == "git.diff":
                code, out, err = await self._run_capture(
                    ["git", "-C", target, "diff", "HEAD"], timeout=30
                )
                text, truncated = self._limit(out)
                payload = {"diff": text, "truncated": truncated, "exit_code": code}
            elif name == "git.promote":
                execution_id = str(args.get("execution_id") or "")
                if execution_id:
                    from veya.remote.execution_worktree import (
                        CanonicalPromotionService,
                        ExecutionWorktreeError,
                    )

                    try:
                        ledger = await self._ensure_promotion_ledger()
                        execution_record = self.jobs.lookup(execution_id)
                        verification_evidence = {}
                        if execution_record is not None and isinstance(
                            execution_record.effect_receipt, dict
                        ):
                            raw_verification = execution_record.effect_receipt.get("verification")
                            if isinstance(raw_verification, dict):
                                verification_evidence = dict(raw_verification)
                        # Promotion is a bounded CAS/ref update plus the
                        # short SQLite ledger transaction.  Keep it on the
                        # fast tool path instead of creating the Python
                        # default executor just to invoke a synchronous
                        # authority.
                        evidence = CanonicalPromotionService(
                            self.execution_worktrees, side_effect_ledger=ledger
                        ).promote(
                            execution_id,
                            ws_binding.repo_root,
                            expected_base_sha=str(args.get("expected_base_sha") or ""),
                            verification_evidence=verification_evidence,
                        )
                    except ExecutionWorktreeError as exc:
                        return self._fail(
                            name,
                            session,
                            RemoteErrorCode.POLICY_BLOCKED,
                            f"promotion blocked: {exc.message}",
                        )
                    payload = evidence.to_dict()
                    payload["workspace"] = ws_binding.requested_realpath
                    payload["repo_root"] = ws_binding.repo_root
                    payload["resolution"] = resolution.to_public()
                    return RemoteCallResult(
                        ok=True,
                        tool=name,
                        session_id=session.session_id,
                        workspace=ws_binding.requested_realpath,
                        result=self._redact(payload),
                        duration_ms=(time.time() - started) * 1000,
                    )

                source_worktree = str(args.get("source_worktree") or "")
                if source_worktree:
                    from veya.remote.git_promotion import (
                        PromotionError,
                        apply_promotion,
                        preflight_promotion,
                    )

                    target_canonical = str(args.get("target_canonical") or ws_binding.repo_root)
                    files = args.get("files")
                    expected_base_sha = args.get("expected_base_sha")
                    verify_after = bool(args.get("verify_after", True))
                    verify_command = args.get("verify_command")
                    rollback_on_failure = bool(args.get("rollback_on_failure", True))

                    try:
                        preflight = await asyncio.to_thread(
                            preflight_promotion,
                            source_worktree,
                            target_canonical,
                            files=files,
                            expected_base_sha=expected_base_sha,
                        )
                        if not preflight.safe_to_promote:
                            from veya.remote.git_promotion import PromotionResult

                            blocked_result = PromotionResult(
                                status="BLOCKED",
                                preflight=preflight,
                                promoted_files=[],
                                unrelated_dirty_preserved=True,
                                verified=False,
                                message=preflight.rejection_reason or "promotion preflight blocked",
                            )
                            payload = blocked_result.to_dict()
                            payload["workspace"] = ws_binding.requested_realpath
                            payload["repo_root"] = ws_binding.repo_root
                            payload["resolution"] = resolution.to_public()
                            return self._fail(
                                name,
                                session,
                                RemoteErrorCode.POLICY_BLOCKED,
                                blocked_result.message,
                                result=self._redact(payload),
                            )
                        result = await asyncio.to_thread(
                            apply_promotion,
                            preflight,
                            verify_after=verify_after,
                            verify_command=verify_command,
                            rollback_on_failure=rollback_on_failure,
                        )
                        payload = result.to_dict()
                    except PromotionError as exc:
                        return self._fail(name, session, RemoteErrorCode.POLICY_BLOCKED, str(exc))
                    payload["workspace"] = ws_binding.requested_realpath
                    payload["repo_root"] = ws_binding.repo_root
                    payload["resolution"] = resolution.to_public()
                    if result.status != "PROMOTED":
                        return self._fail(
                            name,
                            session,
                            RemoteErrorCode.POLICY_BLOCKED,
                            result.message or f"promotion status: {result.status}",
                            result=self._redact(payload),
                        )
                    return RemoteCallResult(
                        ok=True,
                        tool=name,
                        session_id=session.session_id,
                        workspace=ws_binding.requested_realpath,
                        result=self._redact(payload),
                        duration_ms=(time.time() - started) * 1000,
                    )

                return self._fail(
                    name,
                    session,
                    RemoteErrorCode.POLICY_BLOCKED,
                    "git.promote requires execution_id or source_worktree",
                )
            else:
                limit = max(1, min(int(args.get("limit", 20)), 200))
                code, out, err = await self._run_capture(
                    ["git", "-C", target, "log", f"-n{limit}", "--oneline"], timeout=20
                )
                payload = {"log": out, "exit_code": code}
            # Keep the execution identity explicit in every fast Git response.
            # This makes metadata, repo selection and command cwd auditable as
            # one tuple and prevents a correct-looking result from hiding a
            # wrong-repository execution.
            payload["workspace"] = ws_binding.requested_realpath
            payload["repo_root"] = ws_binding.repo_root
            payload["cwd"] = target
            payload["resolution"] = resolution.to_public()
            if code not in (0, None) and err:
                payload["stderr"] = err[-2000:]
        except Exception as exc:
            return self._fail(
                name, session, RemoteErrorCode.EXECUTION_FAILED, f"{type(exc).__name__}: {exc}"
            )
        return RemoteCallResult(
            ok=True,
            tool=name,
            session_id=session.session_id,
            workspace=ws_binding.requested_realpath,
            result=self._redact(payload),
            duration_ms=(time.time() - started) * 1000,
        )

    async def _run_capture(self, argv: list[str], *, timeout: float) -> tuple[int | None, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            await terminate_process_group(proc)
            return proc.returncode, "", "command timed out"
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    @staticmethod
    def _parse_git_status(text: str) -> dict[str, Any]:
        branch: str | None = None
        changed: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                branch = line[3:].strip()
            elif line.strip():
                changed.append(line[3:].strip() if len(line) > 3 else line.strip())
        return {
            "branch": branch,
            "clean": not changed,
            "changed_files": changed,
            "text": text,
        }

    # ── fast read primitives (P0-A/N) ────────────────────────
    async def _call_fast_read(
        self,
        session: RemoteSession,
        policy: WorkspacePolicy,
        name: str,
        args: dict[str, Any],
        ws_binding: WorkspaceBinding,
        started: float,
        *,
        resolution: RepoResolution,
    ) -> RemoteCallResult:
        base = self._base_dir(session, ws_binding.requested_realpath)
        try:
            payload = await self._fast_read_payload(session, policy, name, args, base)
        except RemoteToolAdapterError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        except WorkspacePolicyError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        except FileNotFoundError as exc:
            return self._fail(name, session, RemoteErrorCode.NOT_FOUND, str(exc))
        except Exception as exc:
            return self._fail(
                name, session, RemoteErrorCode.EXECUTION_FAILED, f"{type(exc).__name__}: {exc}"
            )
        payload.setdefault("resolution", resolution.to_public())
        return RemoteCallResult(
            ok=True,
            tool=name,
            session_id=session.session_id,
            workspace=ws_binding.requested_realpath,
            result=self._redact(payload),
            duration_ms=(time.time() - started) * 1000,
        )

    async def _fast_read_payload(
        self,
        session: RemoteSession,
        policy: WorkspacePolicy,
        name: str,
        args: dict[str, Any],
        base: str,
    ) -> dict[str, Any]:
        if name == "workspace.info":
            target = self._resolve_in(policy, base, args.get("path") or ".", must_exist=True)
            from runtime.coding.workspace_detect import detect_workspace

            workspace = detect_workspace(str(target), owner_user_id="remote")
            return {"path": str(target), "workspace": workspace.to_dict()}
        if name == "workspace.list":
            target = self._resolve_in(policy, base, args.get("path") or ".", must_exist=True)
            lines = _list_workspace(target)
            text = "\n".join(lines) or "(empty)"
            output, truncated = self._limit(text)
            return {"path": str(target), "entries": lines, "text": output, "truncated": truncated}
        if name == "runtime.profile":
            target = self._resolve_in(policy, base, args.get("path") or ".", must_exist=True)
            from veya.remote.runtime_profile import discover_runtime_profile

            force_refresh = bool(args.get("force_refresh", False))
            profile = discover_runtime_profile(str(target), force_refresh=force_refresh)
            return {"path": str(target), "profile": profile.to_dict()}
        if name == "runtime.capabilities":
            target = self._resolve_in(policy, base, args.get("path") or ".", must_exist=True)
            from veya.remote.runtime_profile import discover_runtime_profile

            profile = discover_runtime_profile(str(target))
            caps = {
                "workspace": str(target),
                "python": profile.python,
                "pytest": profile.pytest,
                "ruff": profile.ruff,
                "mypy": profile.mypy,
                "node": profile.node,
                "pnpm": profile.pnpm,
                "uv": profile.uv,
                "docker": profile.docker,
                "service_capabilities": list(profile.service_capabilities),
                "available_domains": ["L0_WORKSPACE_FULL", "L0_ISOLATED", "L0_HOST"],
                "available_targets": [
                    "NEW_ISOLATED_WORKTREE",
                    "EXISTING_WORKTREE",
                    "CANONICAL_WORKTREE",
                    "HOST",
                ],
                "profile_hash": profile.profile_hash,
            }
            return {"path": str(target), "capabilities": caps}
        if name == "runtime.probe":
            target = self._resolve_in(policy, base, args.get("path") or ".", must_exist=True)
            from veya.remote.direct_exec import run_direct_command
            from veya.remote.runtime_profile import discover_runtime_profile

            profile = discover_runtime_profile(str(target))
            tool = str(args.get("tool") or "").strip()
            command = str(args.get("command") or "").strip()
            if not command and tool:
                if tool == "python":
                    command = (
                        f"{profile.python_bin} --version"
                        if profile.python_bin
                        else "python --version"
                    )
                elif tool == "pytest":
                    command = (
                        f"{profile.pytest_bin} --version"
                        if profile.pytest_bin
                        else f"{profile.python_bin} -m pytest --version"
                    )
                elif tool == "ruff":
                    command = (
                        f"{profile.ruff_bin} --version" if profile.ruff_bin else "ruff --version"
                    )
                elif tool == "mypy":
                    command = (
                        f"{profile.mypy_bin} --version" if profile.mypy_bin else "mypy --version"
                    )
                elif tool == "node":
                    command = (
                        f"{profile.node_bin} --version" if profile.node_bin else "node --version"
                    )
                elif tool == "pnpm":
                    command = (
                        f"{profile.pnpm_bin} --version" if profile.pnpm_bin else "pnpm --version"
                    )
                elif tool == "uv":
                    command = f"{profile.uv_bin} --version" if profile.uv_bin else "uv --version"
                elif tool == "docker":
                    command = (
                        f"{profile.docker_bin} --version"
                        if profile.docker_bin
                        else "docker --version"
                    )
                else:
                    command = f"{tool} --version"
            if not command:
                raise RemoteToolAdapterError(
                    RemoteErrorCode.INVALID_ARGUMENT, "tool or command required for probe"
                )
            result = await run_direct_command(
                str(target),
                command,
                cwd=str(target),
                profile="l0_workspace_full",
                runtime_profile=profile,
                timeout_s=30.0,
            )
            return {
                "path": str(target),
                "tool": tool,
                "command": command,
                "status": result.status,
                "exit_code": result.exit_code,
                "stdout": result.stdout_tail.strip(),
                "stderr": result.stderr_tail.strip(),
                "duration_ms": result.duration_ms,
            }
        if name == "file.read":
            target = self._resolve_target(session, policy, base, args["path"], must_exist=True)
            # Fast reads are deliberately synchronous.  They only resolve an
            # existing target and must not create an execution worktree or
            # initialize the event loop's default executor.
            content = Path(target).read_text(encoding="utf-8", errors="replace")
            cap = max(1, int(args.get("max_lines", 2000)))
            from server.hashline import render

            text, truncated = self._limit(f"[hashline {target}]\n{render(content, max_lines=cap)}")
            return {"path": str(target), "text": text, "truncated": truncated}
        if name == "file.search":
            root = self._resolve_target(
                session, policy, base, args.get("path") or ".", must_exist=True
            )
            pattern = str(args["pattern"])
            argv = ["rg", "--no-heading", "-n", pattern, str(root)]
            if args.get("glob"):
                argv[3:3] = ["-g", str(args["glob"])]
            code, out, err = await self._run_capture(argv, timeout=30)
            if code == 127 or "not found" in err:
                raise RemoteToolAdapterError(
                    RemoteErrorCode.EXECUTION_FAILED, "ripgrep (rg) is not installed"
                )
            text, truncated = self._limit(out)
            return {"root": str(root), "text": text, "truncated": truncated, "exit_code": code}
        if name in {"artifact.list", "artifact.read"}:
            outputs_dir = self._task_outputs(session, policy)
            if not outputs_dir.exists():
                raise RemoteToolAdapterError(
                    RemoteErrorCode.NOT_FOUND, "no finalized artifacts for this session"
                )
            if name == "artifact.list":
                lines = _list_workspace(outputs_dir)
                text = "\n".join(lines) or "(empty)"
                output, truncated = self._limit(text)
                return {
                    "path": str(outputs_dir),
                    "entries": lines,
                    "text": output,
                    "truncated": truncated,
                }
            target = self._resolve_in(policy, str(outputs_dir), args["path"], must_exist=True)
            content = Path(target).read_text(encoding="utf-8", errors="replace")
            cap = max(1, int(args.get("max_lines", 4000)))
            from server.hashline import render

            text, truncated = self._limit(f"[hashline {target}]\n{render(content, max_lines=cap)}")
            return {"path": str(target), "text": text, "truncated": truncated}
        raise RemoteToolAdapterError(RemoteErrorCode.INVALID_ARGUMENT, f"bad read tool {name}")

    # ── progress-aware runner (P0-E/P0-Q) ───────────────────────────
    def _make_long_runner(
        self,
        session: RemoteSession,
        policy: WorkspacePolicy,
        binding: ToolBinding,
        args: dict[str, Any],
        ws_binding: WorkspaceBinding,
        *,
        tool: str,
    ) -> Any:
        async def runner(reporter: ProgressReporter) -> str:
            reporter.phase(
                ExecutionPhase.WORKSPACE_VALIDATION,
                message="workspace validated",
                event="workspace validated",
            )
            try:
                veya_tool, kwargs, write_root = await self._prepare(
                    session, policy, binding, args, ws_binding
                )
            except (RemoteToolAdapterError, WorkspacePolicyError, WorkspaceBindingError) as exc:
                raise ExecutionBlocked(
                    str(getattr(exc, "code", "POLICY_BLOCKED")), exc.message
                ) from exc
            worktree_path = write_root or kwargs.get("worktree_path")
            if worktree_path:
                # Persist the verified worktree identity for process.status (P0-B).
                reporter.set_worktree(str(worktree_path), ws_binding.repo_root)
            return await self._invoke_with_progress(
                reporter, veya_tool, kwargs, write_root, tool=tool
            )

        return runner

    async def _invoke_with_progress(
        self,
        reporter: ProgressReporter,
        veya_tool: str,
        kwargs: dict[str, Any],
        write_root: Path | None,
        *,
        tool: str,
    ) -> str:
        # Report only phases we can observe truthfully. Executors may publish
        # finer real progress through ``veya.remote.execution.report_progress``.
        reporter.phase(
            _initial_phase(tool),
            message=f"dispatching {veya_tool}",
            event=f"dispatching {veya_tool}",
        )
        with use_reporter(reporter):
            output = await self._invoke(veya_tool, kwargs, write_root)
        reporter.phase(ExecutionPhase.FINALIZING, message="finalizing result", event="finalizing")
        return output

    # ── process.* ───────────────────────────────────────────────────
    def _process_status(
        self, session: RemoteSession, name: str, args: dict[str, Any], started: float
    ) -> RemoteCallResult:
        execution_id = str(args.get("execution_id", ""))
        try:
            record = self.jobs.status(
                execution_id,
                token_id=session.token_id,
                workspace_realpath=session.explicit_workspace,
            )
        except ExecutionError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        payload = record.to_public(heartbeat_timeout_s=self.jobs.heartbeat_timeout_s)
        if record.child_execution_ids:
            # L1 parent: mechanical aggregation only (no ranking/winner/JEV).
            aggregate = self.jobs.aggregate(record)
            payload["status"] = aggregate["status"]
            payload["phase"] = aggregate["phase"]
            payload["children"] = aggregate["children"]
            payload["aggregation"] = aggregate["counts"]
        return RemoteCallResult(
            ok=True,
            tool=name,
            session_id=session.session_id,
            workspace=session.active_workspace,
            result=self._redact(payload),
            execution_id=record.execution_id,
            duration_ms=(time.time() - started) * 1000,
        )

    async def _process_cancel(
        self, session: RemoteSession, name: str, args: dict[str, Any], started: float
    ) -> RemoteCallResult:
        execution_id = str(args.get("execution_id", ""))
        try:
            record = await self.jobs.cancel(
                execution_id,
                token_id=session.token_id,
                session_id=session.session_id,
                workspace_realpath=session.explicit_workspace,
                principal=session.principal,
            )
        except ExecutionError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        return RemoteCallResult(
            ok=True,
            tool=name,
            session_id=session.session_id,
            workspace=session.active_workspace,
            result=self._redact(
                record.to_public(heartbeat_timeout_s=self.jobs.heartbeat_timeout_s)
            ),
            execution_id=record.execution_id,
            duration_ms=(time.time() - started) * 1000,
        )

    # ── autonomous.* (spec §35) ─────────────────────────────────────
    async def _autonomous_call(
        self, session: RemoteSession, name: str, args: dict[str, Any], started: float
    ) -> RemoteCallResult:
        mission_id = str(args.get("mission") or "")
        project_root = Path(session.active_workspace or os.getcwd())
        auto_dir = project_root / ".veya" / "autonomous" / mission_id

        if name == "autonomous.status":
            sfile = auto_dir / "state.json"
            if sfile.is_file():
                with open(sfile, encoding="utf-8") as f:
                    res = json.load(f)
            else:
                res = {"mission_id": mission_id, "status": "NOT_FOUND"}
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result=res,
                duration_ms=(time.time() - started) * 1000,
            )

        elif name == "autonomous.observations":
            limit = int(args.get("limit") or 50)
            from veya.autonomous.journal import ObservationJournal

            journal = ObservationJournal(auto_dir / "journal.jsonl")
            obs = [o.to_dict() for o in journal.query(mission_id, limit=limit)]
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result={"observations": obs},
                duration_ms=(time.time() - started) * 1000,
            )

        elif name == "autonomous.decisions":
            limit = int(args.get("limit") or 50)
            from veya.autonomous.decision import DecisionStore

            store = DecisionStore(auto_dir / "decisions.jsonl")
            decs = [d.to_dict() for d in store.query(mission_id, limit=limit)]
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result={"decisions": decs},
                duration_ms=(time.time() - started) * 1000,
            )

        elif name == "autonomous.progress":
            sfile = auto_dir / "state.json"
            st = {}
            if sfile.is_file():
                with open(sfile, encoding="utf-8") as f:
                    st = json.load(f)
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result={
                    "mission_id": mission_id,
                    "objective": st.get("objective", ""),
                    "accepted_progress": st.get("accepted_progress", []),
                    "state": st.get("state", "UNKNOWN"),
                },
                duration_ms=(time.time() - started) * 1000,
            )

        elif name == "autonomous.waits":
            from veya.autonomous.wait import WaitConditionManager

            wm = WaitConditionManager(auto_dir / "waits.jsonl")
            waits = [w.to_dict() for w in wm.query(mission_id)]
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result={"waits": waits},
                duration_ms=(time.time() - started) * 1000,
            )

        elif name == "autonomous.escalations":
            from veya.autonomous.escalation import EscalationManager

            em = EscalationManager(auto_dir / "escalations.jsonl")
            escs = [e.to_dict() for e in em.query(mission_id)]
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result={"escalations": escs},
                duration_ms=(time.time() - started) * 1000,
            )

        elif name == "autonomous.explain":
            dec_id = str(args.get("decision_id") or "")
            from veya.autonomous.decision import DecisionStore

            found = None
            all_auto = project_root / ".veya" / "autonomous"
            if all_auto.is_dir():
                for m_path in all_auto.iterdir():
                    d_file = m_path / "decisions.jsonl"
                    if d_file.is_file():
                        st_dec = DecisionStore(d_file)
                        found = st_dec.get_decision(dec_id)
                        if found:
                            break
            res = found.to_dict() if found else {"decision_id": dec_id, "found": False}
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result=res,
                duration_ms=(time.time() - started) * 1000,
            )

        elif name == "interrupt.reply":
            esc_id = str(args.get("escalation_id") or "")
            reply = str(args.get("reply") or "")
            from veya.autonomous.cycle import AutonomousCycle

            cycle = AutonomousCycle(mission_id=mission_id, objective="", base_dir=project_root)
            st_reply = cycle.handle_owner_reply(esc_id, reply)
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result=st_reply.to_dict(),
                duration_ms=(time.time() - started) * 1000,
            )

        elif name == "mission.revise":
            new_obj = str(args.get("new_objective") or "")
            reason = str(args.get("reason") or "Mission revision requested via MCP")
            from veya.autonomous.cycle import AutonomousCycle
            from veya.autonomous.models import InterruptCategory

            cycle = AutonomousCycle(mission_id=mission_id, objective="", base_dir=project_root)
            st_rev = cycle.handle_interrupt(
                sender="mcp_client",
                content=f"REVISE: {new_obj} (reason: {reason})",
                category=InterruptCategory.OBJECTIVE_CHANGE,
            )
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result=st_rev.to_dict(),
                duration_ms=(time.time() - started) * 1000,
            )

        return self._fail(name, session, RemoteErrorCode.TOOL_DENIED, "unknown autonomous tool")

    # ── failures ────────────────────────────────────────────────────
    def _fail(
        self,
        tool: str,
        session: RemoteSession | None,
        code: RemoteErrorCode,
        message: str,
        result: Any = None,
    ) -> RemoteCallResult:
        return RemoteCallResult(
            ok=False,
            tool=tool,
            session_id=session.session_id if session else None,
            workspace=session.active_workspace if session else None,
            error_code=code,
            message=message,
            result=result,
        )


def _direct_failure_class(result: Any) -> str:
    """Failure taxonomy for a non-passed direct command (P0-G)."""

    status = str(getattr(result, "status", "") or "")
    exit_code = getattr(result, "exit_code", None)
    stderr = str(getattr(result, "stderr_tail", "") or "")
    if status == "timeout" or getattr(result, "timed_out", False):
        return "COMMAND_TIMEOUT"
    if exit_code is None or "unable to execute command" in stderr:
        return "COMMAND_SPAWN_FAILED"
    return "COMMAND_FAILED"


def _terminal_error_code(record: Any) -> RemoteErrorCode:
    status = str(getattr(record, "status", ""))
    error = str(getattr(record, "error", "") or "")
    if status == str(ExecutionStatus.CANCELLED):
        return RemoteErrorCode.CANCELLED
    if error:
        try:
            return RemoteErrorCode(error)
        except ValueError:
            pass
    if status == str(ExecutionStatus.BLOCKED):
        return RemoteErrorCode.POLICY_BLOCKED
    return RemoteErrorCode.EXECUTION_FAILED


def _normalize_mission_policy(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate mission policy into explicit skill permissions.

    Network/GitHub are denied unless the mission explicitly permits them;
    ordinary local read/write execution remains usable by default.
    """

    permissions = {str(permission) for permission in SkillPermission}
    policy = dict(raw or {})
    if str(policy.get("network", "ALLOW")).upper() == "DENY":
        permissions.discard(str(SkillPermission.NETWORK))
        permissions.discard(str(SkillPermission.GITHUB))
    if str(policy.get("github", "ALLOW")).upper() == "DENY":
        permissions.discard(str(SkillPermission.GITHUB))
    explicit = policy.get("allowed")
    if isinstance(explicit, list):
        permissions = {str(value) for value in explicit}
    policy["allowed"] = sorted(permissions)
    return policy


def _initial_phase(tool: str) -> ExecutionPhase:
    if tool in {"test.run", "build.run"}:
        return ExecutionPhase.RUNNING
    if tool in {"shell.exec"}:
        return ExecutionPhase.RUNNING
    return ExecutionPhase.STARTING


def _execution_limits(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Separate the distinct budgets (P0-L); never fold them into one timeout."""

    limits: dict[str, Any] = {}
    if tool == "hicode.execute":
        if args.get("max_steps"):
            limits["max_steps"] = int(args["max_steps"])
        if args.get("timeout_sec"):
            limits["execution_timeout_sec"] = float(args["timeout_sec"])
    elif args.get("timeout_s"):
        limits["command_timeout_sec"] = float(args["timeout_s"])
    return limits


def _error_code(value: str | None) -> RemoteErrorCode:
    if value:
        try:
            return RemoteErrorCode(value)
        except ValueError:
            pass
    return RemoteErrorCode.EXECUTION_FAILED


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _short(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


_NOISE_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".coverage",
        "dist",
        "build",
    }
)


def _list_workspace(target: Path, *, limit: int = 200) -> list[str]:
    """Bounded, noise-free directory listing (P0-N: O(request scope))."""

    lines: list[str] = []
    for path in sorted(target.rglob("*")):
        if any(part in _NOISE_DIRS for part in path.parts):
            continue
        try:
            rel = path.relative_to(target)
        except ValueError:
            continue
        lines.append(f"{rel}/" if path.is_dir() else f"{rel} ({path.stat().st_size}b)")
        if len(lines) >= limit:
            lines.append("... (truncated)")
            break
    return lines


def _worker_runtime_identity(worker: str) -> ExecutorRuntimeIdentity:
    """Return the one canonical identity projection used by dispatch."""

    return get_executor_registry().identity(worker)


def _hicode_model_identity() -> tuple[str, str]:
    identity = _worker_runtime_identity("hicode")
    return identity.provider or "unknown", identity.model or "unknown"


def _worker_model_identity(worker: str) -> tuple[str, str]:
    """Provider/model identity for an L1 worker (no secrets)."""

    identity = _worker_runtime_identity(worker)
    return identity.provider or "unknown", identity.model or "unknown"


def worker_availability() -> dict[str, Any]:
    """Canonical L1 availability registry (routing evidence; Codex stays registered).

    ``available`` workers may be selected; ``temporarily_unavailable`` workers keep
    their canonical registration and carry the real runtime blocker. The planner/
    router may use this evidence to avoid selecting an unavailable worker, but a
    caller that explicitly requests one is never substituted.
    """

    return {
        "available_workers": [
            worker_type
            for worker, worker_type in _WORKER_TYPES.items()
            if worker not in _WORKER_BLOCKERS
        ],
        "temporarily_unavailable_workers": {
            _WORKER_TYPES[worker]: reason for worker, reason in _WORKER_BLOCKERS.items()
        },
    }


def _is_pi_coding_agent(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return False
    return os.access(path.expanduser(), os.X_OK) and "pi-coding-agent" in str(resolved)


def _resolve_pi_binary() -> str:
    configured = os.environ.get("VEYA_PI_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if _is_pi_coding_agent(candidate):
            return str(candidate)
        raise RuntimeError(f"VEYA_PI_BIN is not a Pi Coding Agent executable: {candidate}")

    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    candidates = list(nvm_root.glob("*/bin/pi")) if nvm_root.is_dir() else []
    candidates.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0.0, reverse=True)
    for candidate in candidates:
        if _is_pi_coding_agent(candidate):
            return str(candidate)

    discovered = shutil.which("pi")
    if discovered and _is_pi_coding_agent(Path(discovered)):
        return discovered
    raise RuntimeError("Pi Coding Agent executable not found; set VEYA_PI_BIN")


def _resolve_codex_binary() -> str:
    configured = os.environ.get("VEYA_CODEX_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise RuntimeError(f"VEYA_CODEX_BIN is not executable: {candidate}")

    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    candidates = list(nvm_root.glob("*/bin/codex")) if nvm_root.is_dir() else []
    candidates.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0.0, reverse=True)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    discovered = shutil.which("codex")
    if discovered:
        return discovered
    raise RuntimeError("Codex executable not found; set VEYA_CODEX_BIN")


def _resolve_codex_model() -> str:
    configured = str(os.environ.get("VEYA_CODEX_MODEL") or "").strip()
    return configured or (get_executor_registry().identity("codex").model or "")


def _resolve_antigravity_binary() -> str:
    configured = os.environ.get("VEYA_ANTIGRAVITY_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise RuntimeError(f"VEYA_ANTIGRAVITY_BIN is not executable: {candidate}")

    discovered = shutil.which("agy")
    if discovered:
        return discovered
    candidate = Path.home() / ".local" / "bin" / "agy"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    raise RuntimeError("Antigravity CLI executable not found; set VEYA_ANTIGRAVITY_BIN")


def _resolve_antigravity_model() -> str | None:
    configured = str(os.environ.get("VEYA_ANTIGRAVITY_MODEL") or "").strip()
    if configured:
        return configured
    settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    try:
        payload = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    model = str(payload.get("model") or "").strip() if isinstance(payload, dict) else ""
    return model or get_executor_registry().identity("antigravity").model


def _resolve_opencode_binary() -> str:
    configured = os.environ.get("VEYA_OPENCODE_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise RuntimeError(f"VEYA_OPENCODE_BIN is not executable: {candidate}")

    discovered = shutil.which("opencode")
    if discovered:
        return discovered
    candidate = Path.home() / ".opencode" / "bin" / "opencode"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    candidate_local = Path.home() / ".local" / "bin" / "opencode"
    if candidate_local.is_file() and os.access(candidate_local, os.X_OK):
        return str(candidate_local)
    raise RuntimeError("OpenCode CLI executable not found; set VEYA_OPENCODE_BIN")


def _resolve_opencode_model() -> str | None:
    configured = str(os.environ.get("VEYA_OPENCODE_MODEL") or "").strip()
    return configured or get_executor_registry().identity("opencode").model


def _ensure_proxy_env(env: dict[str, str]) -> None:
    """Normalize and propagate proxy configuration to child worker processes.

    The adapter never hardcodes host-specific proxy addresses. The proxy is
    supplied by the environment (systemd unit, shell env, or explicit
    ``VEYA_RUNTIME_PROXY`` / ``VEYA_PROXY``).
    """

    proxy = (
        env.get("https_proxy")
        or env.get("HTTPS_PROXY")
        or env.get("http_proxy")
        or env.get("HTTP_PROXY")
        or env.get("all_proxy")
        or env.get("ALL_PROXY")
        or os.environ.get("VEYA_RUNTIME_PROXY")
        or os.environ.get("VEYA_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("http_proxy")
        or os.getenv("all_proxy")  # noqa: SIM112
    )
    if not proxy:
        return

    proxy_str = str(proxy).strip()
    if not proxy_str:
        return

    env.setdefault("http_proxy", proxy_str)
    env.setdefault("https_proxy", proxy_str)
    env.setdefault("HTTP_PROXY", proxy_str)
    env.setdefault("HTTPS_PROXY", proxy_str)
    env.setdefault("all_proxy", proxy_str)
    env.setdefault("ALL_PROXY", proxy_str)

    no_proxy_val = (
        env.get("no_proxy")
        or env.get("NO_PROXY")
        or os.environ.get("no_proxy")
        or os.environ.get("NO_PROXY")
    )
    if no_proxy_val:
        env.setdefault("no_proxy", str(no_proxy_val).strip())
        env.setdefault("NO_PROXY", str(no_proxy_val).strip())


def _external_worker_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("OPENCODE_DANGEROUSLY_" + "SKIP_PERMISSIONS", None)
    env.setdefault("HOME", str(Path.home()))
    _ensure_proxy_env(env)
    return env


def _opencode_runtime_env() -> dict[str, str]:
    """Keep OpenCode state writable and outside the execution worktree.

    ``--dir`` selects the code surface; XDG paths select provider-owned
    logs/session/cache state.  We intentionally keep HOME pointed at the
    operator home so the existing OpenCode config/credentials remain visible,
    while relocating mutable runtime state to a managed Veya directory.
    """

    env = dict(os.environ)
    env.pop("OPENCODE_DANGEROUSLY_" + "SKIP_PERMISSIONS", None)
    home = Path(env.get("HOME") or Path.home()).expanduser().resolve()
    source_data_home = (
        Path(env.get("XDG_DATA_HOME") or home / ".local" / "share").expanduser().resolve()
    )
    configured_root = env.get("VEYA_OPENCODE_RUNTIME_HOME")
    runtime_root = (
        Path(configured_root).expanduser().resolve()
        if configured_root
        else (home / ".veya" / "opencode-runtime")
    )
    try:
        for name in ("data", "state", "cache"):
            (runtime_root / name).mkdir(parents=True, exist_ok=True)
    except OSError:
        if configured_root:
            raise
        # The operator home can be read-only in a service/sandbox.  Keep the
        # provider state outside the code worktree and use the managed temp
        # root rather than silently falling back to the execution directory.
        runtime_root = Path(tempfile.gettempdir()) / "veya-opencode-runtime"
        for name in ("data", "state", "cache"):
            (runtime_root / name).mkdir(parents=True, exist_ok=True)
    # OpenCode stores provider credentials below XDG_DATA_HOME/opencode.  The
    # runtime directories are deliberately separate from the code worktree,
    # but moving XDG_DATA_HOME must not silently make an already-authenticated
    # provider look unauthenticated.  Mirror only the credential file into the
    # managed data root with restrictive permissions; config remains sourced
    # from HOME and no credential value is placed in the worker environment.
    source_auth = source_data_home / "opencode" / "auth.json"
    target_auth = runtime_root / "data" / "opencode" / "auth.json"
    try:
        if source_auth.is_file() and source_auth.resolve() != target_auth.resolve():
            source_mtime = source_auth.stat().st_mtime_ns
            target_mtime = target_auth.stat().st_mtime_ns if target_auth.exists() else -1
            if source_mtime > target_mtime:
                target_auth.parent.mkdir(parents=True, exist_ok=True)
                temporary_auth = target_auth.with_name(f".auth-{os.getpid()}.tmp")
                shutil.copyfile(source_auth, temporary_auth)
                os.chmod(temporary_auth, 0o600)
                os.replace(temporary_auth, target_auth)
    except OSError:
        # Text/read qualification can still run without credentials; the
        # provider will report its own unavailable/auth failure instead of
        # making environment construction itself opaque.
        pass
    env["HOME"] = str(home)
    env["XDG_DATA_HOME"] = str(runtime_root / "data")
    env["XDG_STATE_HOME"] = str(runtime_root / "state")
    env["XDG_CACHE_HOME"] = str(runtime_root / "cache")
    _ensure_proxy_env(env)
    return env


def _codex_worker_env() -> dict[str, str]:
    """Use the native Codex/OpenAI config without Veya/local endpoint leakage."""

    env = dict(os.environ)
    for key in (
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_API_HOST",
        "OPENAI_ENDPOINT",
        "OPENAI_PROXY",
        "VEYA_LLM_ENDPOINT",
        "VEYA_OPENAI_BASE_URL",
        "VEYA_OPENAI_ENDPOINT",
    ):
        env.pop(key, None)
    env.setdefault("HOME", str(Path.home()))
    _ensure_proxy_env(env)
    return env


def _worker_command(
    worker: str,
    task: str,
    *,
    worktree_path: str | None = None,
    coding_mode: bool = False,
    agent: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Real, worker-specific argv + env. Never a Hicode wrapper."""

    if worker == "dsh":
        from server import dsh_plane

        bin_path = shutil.which("dsh") or str(Path.home() / ".local/bin/dsh")
        # DSH is an executor behind the Veya gateway.  Pin the child wiring to
        # the canonical local route even when a stale DEEPSEEK_* process env is
        # present; the DSH key remains a non-secret placeholder.
        dsh_cfg = dsh_plane.load_config()
        dsh_cfg.update(
            {
                "DSH_PROVIDER": get_executor_registry().identity("dsh").provider or "",
                "DSH_BASE_URL": dsh_plane.DEFAULT_BASE_URL,
                "DSH_MODEL": get_executor_registry().identity("dsh").model or "",
                "DSH_API_KEY": dsh_plane.api_key(dsh_cfg),
            }
        )
        dsh_env = dsh_plane.subprocess_env(dsh_cfg)
        return dsh_plane.dsh_argv(bin_path, task, dsh_cfg), dsh_env
    if worker == "pi":
        bin_path = _resolve_pi_binary()
        return [
            bin_path,
            "-p",
            task,
            "--provider",
            get_executor_registry().identity("pi").provider or "",
            "--model",
            get_executor_registry().identity("pi").model or "",
            "--tools",
            "read,bash,edit,write",
            "--approve",
        ], dict(os.environ)
    if worker == "opencode":
        if coding_mode and not worktree_path:
            raise RemoteToolAdapterError(
                RemoteErrorCode.WORKSPACE_DENIED,
                "OpenCode coding mode requires an execution worktree",
            )
        bin_path = _resolve_opencode_binary()
        model = _resolve_opencode_model()
        from server.engine_runner import build_argv

        argv = build_argv(
            "opencode",
            task,
            model=model,
            workspace=worktree_path,
            agent=agent if coding_mode else None,
            coding_mode=coding_mode,
            execution_worktree_verified=bool(worktree_path),
        )
        if argv and bin_path:
            argv[0] = bin_path
        return argv, _opencode_runtime_env()
    if worker == "grok":
        bin_path = shutil.which("grok") or str(Path.home() / ".grok/bin/grok")
        grok_env = dict(os.environ)
        grok_env["GROK_SESSION_DIR"] = str(Path.home() / ".grok/sessions")
        return [
            bin_path,
            "-p",
            task,
            "--model",
            get_executor_registry().identity("grok").model or "",
            "--tools",
            "read_file,search_replace,grep,list_dir,run_terminal_command",
            "--sandbox",
            "workspace",
            "--permission-mode",
            "bypassPermissions",
            "--always-approve",
        ], grok_env
    if worker == "codex":
        argv = [
            _resolve_codex_binary(),
            "exec",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
        ]
        codex_model = _resolve_codex_model()
        if codex_model:
            argv.extend(["--model", codex_model])
        argv.append(task)
        return argv, _codex_worker_env()
    if worker == "antigravity":
        argv = [
            _resolve_antigravity_binary(),
            "--print",
            task,
            "--mode",
            "accept-edits",
            "--sandbox",
            "--dangerously-skip-permissions",
            "--print-timeout",
            "10m",
        ]
        antigravity_model = _resolve_antigravity_model()
        if antigravity_model:
            argv.extend(["--model", antigravity_model])
        return argv, _external_worker_env()
    raise RemoteToolAdapterError(
        RemoteErrorCode.INVALID_ARGUMENT, f"no CLI command for worker {worker!r}"
    )


def _resolve_opencode_agent() -> str | None:
    value = os.environ.get("VEYA_OPENCODE_AGENT", "").strip()
    return value or None


def _json_worker_events(worker: str, lines: list[str]) -> list[dict[str, Any]]:
    """Parse bounded structured worker events without trusting them as effect truth."""

    if worker != "opencode":
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events[-1000:]


def _event_tool_name(event: dict[str, Any]) -> str:
    for key in ("tool", "tool_name", "name", "part"):
        value = event.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = value.get("name") or value.get("tool")
            if isinstance(nested, str):
                return nested
    return ""


def _worker_tool_events(worker: str, lines: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in _json_worker_events(worker, lines):
        event_type = str(event.get("type") or event.get("event") or "").lower()
        name = _event_tool_name(event)
        if "tool" in event_type or name:
            result.append({"type": event_type, "name": name})
    return result


def _worker_shell_events(worker: str, lines: list[str]) -> list[dict[str, Any]]:
    return [
        event
        for event in _worker_tool_events(worker, lines)
        if any(
            token in str(event.get("name", "")).lower()
            for token in ("shell", "bash", "terminal", "exec")
        )
    ]


def _ensure_hicode_workspace(workspace: str) -> None:
    """Fail closed when the canonical Hicode resolver would not honour ``workspace``.

    The remote adapter must never let Hicode silently fall back to its process
    default workspace. Hicode's own sandbox resolver stays authoritative; this
    only asserts that the explicit workspace it will receive is the one we bound.
    """

    try:
        from server.hicode_agent import _resolve_workspace
    except Exception:  # pragma: no cover - hicode unavailable; canonical tool reports it
        return
    try:
        resolved = _resolve_workspace(workspace)
    except ValueError as exc:
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED", "HICODE_WORKSPACE_DENIED", str(exc)
        ) from exc
    if canonical(resolved) != canonical(workspace):
        raise WorkspaceBindingError(
            "WORKSPACE_DENIED",
            "HICODE_WORKSPACE_MISMATCH",
            f"hicode resolved {resolved} instead of the bound workspace {workspace}",
        )


def _canonical_extra_roots() -> tuple[Path, ...]:
    """Extra roots the canonical file tools accept (VEYA_WORKSPACE_EXTRA_DIRS)."""

    raw = os.environ.get("VEYA_WORKSPACE_EXTRA_DIRS", "")
    roots: list[Path] = []
    for part in raw.split(":"):
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(part).expanduser().resolve())
        except OSError:  # pragma: no cover - defensive
            continue
    return tuple(roots)


def ensure_readable(workspace: str) -> None:
    """Make a bound workspace readable by the canonical file tools.

    ``tool_registry._resolve_path`` allows ``VEYA_WORKSPACE`` plus
    ``VEYA_WORKSPACE_EXTRA_DIRS``. The extra-dir list is a union that only ever
    grows, so adding a workspace is additive and cannot widen another session's
    view (each session still passes its own :class:`WorkspacePolicy` check).
    """

    if not workspace:
        return
    current = os.environ.get("VEYA_WORKSPACE_EXTRA_DIRS", "")
    parts = [p for p in current.split(":") if p]
    if workspace not in parts:
        parts.append(workspace)
        os.environ["VEYA_WORKSPACE_EXTRA_DIRS"] = ":".join(parts)


async def _default_executor(name: str, kwargs: dict[str, Any]) -> str:
    from server import tool_registry as registry_module

    registry = registry_module.master_tools
    if name.startswith("hicode_") and not registry.has(name):
        from server.hicode_agent import wire_master_tools

        await wire_master_tools()
    return await registry.execute(name, kwargs)


def default_tool_adapter(**kwargs: Any) -> RemoteToolAdapter:
    return RemoteToolAdapter(**kwargs)
