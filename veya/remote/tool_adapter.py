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
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from runtime.coding.command_runner import CommandPolicyError
from runtime.coding.worktree import WorktreeError, WorktreeManager
from veya.remote.skills import SkillPermission
from veya.supervision.task_memory import TaskMemory

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
from .metrics import LatencyMetrics
from .models import (
    EffectClass,
    RemoteCallResult,
    RemoteErrorCode,
    RemoteSession,
    ToolBinding,
)
from .workspace_binding import (
    RepoResolution,
    WorkspaceBinding,
    WorkspaceBindingError,
    canonical,
    git_repo_identity,
    git_repo_root,
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
    }
)
_FAST_GIT_TOOLS = frozenset({"git.status", "git.diff", "git.log"})
# P0-B: long-command tools -> DirectJobManager with a fast sync window.
_COMMAND_TOOLS = frozenset({"shell.exec", "test.run", "build.run"})

# L1 direct worker registry. Each worker keeps its own real runtime/model
# semantics; none of these is a wrapper around Hicode.
_WORKER_TYPES = {
    "hicode": "HICODE",
    "dsh": "DSH",
    "pi": "PI",
    "grok": "GROK",
    "codex": "CODEX",
}
# Real blockers observed on this host (2026-09). A worker absent from this map is
# dispatched to a live child; otherwise the child is created BLOCKED with the
# exact reason (never substituted by another worker).
_WORKER_BLOCKERS = {
    "codex": (
        "UPSTREAM_QUOTA: codex-cli 0.154.0 ChatGPT plan usage limit (opencodex 10100 "
        "Responses API reachable; quota resets ~3.6h)"
    ),
}
# CLI worker runtime config (real, non-Hicode). DSH/Pi/Grok are provider-closed
# against the local Veya gateway (127.0.0.1:8791); Codex uses its own runtime.
_CLI_WORKERS = {
    "dsh": {"provider": "VEYA_LOCAL_GATEWAY", "model": "veya1.2"},
    # The 128K/free pool is text-only in the installed provider definitions.  A
    # successful process exit from that pool is not enough for a worker whose
    # contract requires filesystem artifacts.  Keep both workers on the
    # existing tool-capable Veya model; this changes no worker identity or
    # cross-worker routing.
    "pi": {"provider": "VEYA_LOCAL", "model": "veya1.2"},
    "grok": {"provider": "VEYA_LOCAL_GATEWAY", "model": "veya1.2"},
    "codex": {"provider": "openai", "model": "gpt-5.6-luna"},
}

_TIMEOUT_SEPARATED_CLI_WORKERS = frozenset({"pi", "grok"})
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
        "Write text content to a file inside the isolated session worktree.",
        _obj(
            {"path": _STR, "content": _STR, "overwrite": {"type": "boolean"}},
            ["path", "content"],
        ),
    ),
    ToolBinding(
        "file.patch",
        "edit_hashline",
        EffectClass.WRITE,
        "Replace a LINE#hash span in a file. Read the file first to obtain the tags.",
        _obj(
            {"path": _STR, "start_tag": _STR, "new_text": _STR, "end_tag": _STR},
            ["path", "start_tag", "new_text"],
        ),
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
            }
        ),
        long_running=True,
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


BINDINGS = BINDINGS + _supervision_bindings()

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
            "failures": [],
        }

    async def initialize(self) -> dict[str, Any]:
        """Initialize the projection and trigger canonical GoalRun recovery.

        This is lifecycle wiring only.  Durable recovery remains implemented
        by ``DurableJobManager.recover_unfinished`` and GoalRun; the adapter
        never resumes a provider or changes a business state itself.
        """
        async with self._startup_lock:
            if self._startup_complete:
                if self._startup_error is not None:
                    raise RuntimeError("remote startup recovery failed") from self._startup_error
                return dict(self.startup_recovery_report)
            if self._startup_error is not None:
                raise RuntimeError("remote startup recovery failed") from self._startup_error
            try:
                unfinished = self.jobs.unfinished_count()
                if unfinished and self.jobs.recovery_runner_factory is None:
                    now = time.time()
                    # A just-admitted GoalRun can be persisted between its
                    # projection write and the worker's first heartbeat
                    # update.  Treat a fresh non-terminal heartbeat as live;
                    # an actual crashed process becomes recoverable only after
                    # that lease window expires.
                    live = all(
                        (record.status == "QUEUED" and record.heartbeat_at is None)
                        or (
                            record.heartbeat_at is not None
                            and now - record.heartbeat_at <= self.jobs.heartbeat_timeout_s
                            and not record.is_terminal
                        )
                        for record in self.jobs.unfinished_records()
                    )
                    if not live:
                        raise RuntimeError(
                            f"{unfinished} stale remote projection(s) require a recovery runner"
                        )
                    # A second adapter in the same process is a reconnecting
                    # projection, not a new owner.  Leave the live GoalRun
                    # untouched and make the deferred state explicit.
                    self.startup_recovery_report = {
                        "started": True,
                        "recovered": 0,
                        "deferred_live": unfinished,
                        "failures": [],
                    }
                    self._startup_complete = True
                    return dict(self.startup_recovery_report)
                recovered = await self.jobs.recover_unfinished()
                self.startup_recovery_report = {
                    "started": True,
                    "recovered": recovered,
                    "failures": list(self.jobs.recovery_failures),
                }
                if self.jobs.recovery_failures:
                    raise RuntimeError(
                        f"{len(self.jobs.recovery_failures)} remote recovery item(s) failed"
                    )
                self._startup_complete = True
                return dict(self.startup_recovery_report)
            except BaseException as exc:
                self._startup_error = exc
                self.startup_recovery_report = {
                    "started": True,
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
        except Exception:
            self.metrics.record(
                name, mode="sync", duration_ms=(time.time() - started) * 1000, ok=False
            )
            raise
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

        if name == "process.status":
            return self._process_status(session, name, args, started)
        if name == "process.cancel":
            return await self._process_cancel(session, name, args, started)

        try:
            policy.require(
                binding.effect,
                needs_shell=binding.needs_shell,
                needs_git=binding.needs_git,
            )
            command = args.get("command")
            if binding.needs_shell and isinstance(command, str):
                policy.require_not_destructive(command)
        except WorkspacePolicyError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)

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
            payload = {key: value for key, value in args.items() if key != "workspace"}
            return binding.veya_tool, {**payload, "project_root": str(workspace)}, None

        if name in {
            "workspace.list",
            "workspace.info",
            "file.read",
            "file.search",
            "artifact.list",
            "artifact.read",
        }:
            base = self._base_dir(session, str(workspace))
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
            worktree, verified_repo = await self._ensure_worktree(session, str(repo_root))
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
            if name == "file.patch" and not mapped.exists():
                raise RemoteToolAdapterError(
                    RemoteErrorCode.NOT_FOUND,
                    f"path not found in isolated worktree: {args['path']}",
                )
            if name == "file.write":
                kwargs = {
                    "filepath": str(mapped),
                    "content": str(args["content"]),
                    "overwrite": bool(args.get("overwrite", True)),
                }
                return binding.veya_tool, kwargs, Path(worktree)
            kwargs = {
                "filepath": str(mapped),
                "start_tag": str(args["start_tag"]),
                "new_text": str(args["new_text"]),
            }
            if args.get("end_tag"):
                kwargs["end_tag"] = str(args["end_tag"])
            return binding.veya_tool, kwargs, Path(worktree)

        # Worktree-backed tools derive their repo strictly from the explicitly
        # bound workspace: never from the session's other worktrees (P0-A/M).
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
            kwargs: dict[str, Any] = {
                "worktree_path": worktree,
                "command": str(args["command"]),
                "timeout_s": float(args.get("timeout_s", self._default_timeout_s)),
                "approved": bool(args.get("approved")) and session.permissions.destructive,
            }
            if args.get("profile"):
                kwargs["profile"] = str(args["profile"])
            if args.get("network"):
                kwargs["network"] = str(args["network"])
            return kwargs
        if name in {"test.run", "build.run"}:
            timeout = float(args.get("timeout_s", self._default_timeout_s))
            approved = session.permissions.destructive
            if name == "test.run":
                kwargs = {"worktree_path": worktree, "timeout_s": timeout}
            else:
                kwargs = {"worktree_path": worktree, "timeout_s": timeout}
            if args.get("command"):
                kwargs["command"] = str(args["command"])
            kwargs["approved"] = bool(args.get("approved")) and approved
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

    def _base_dir(self, session: RemoteSession, workspace: str) -> str:
        # Worktrees are keyed by repository root; fall back to the raw workspace
        # key only for the legacy non-git case.
        repo = git_repo_root(workspace)
        if repo is not None:
            mapped = session.worktrees.get(str(repo))
            if mapped:
                return mapped
        return session.worktrees.get(workspace, workspace)

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
    def _direct_workdir(self, session: RemoteSession, ws_binding: WorkspaceBinding) -> str:
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
        self, name: str, args: dict[str, Any], ws_binding: WorkspaceBinding
    ) -> str:
        explicit = args.get("command")
        if explicit:
            return str(explicit)
        detected = self._detect_commands(ws_binding).get(name) or []
        if detected:
            return detected[0]
        if name == "test.run":
            return "python -m pytest -q"
        raise RemoteToolAdapterError(
            RemoteErrorCode.INVALID_ARGUMENT,
            "no build command detected; pass command explicitly",
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
        try:
            command = self._resolve_command(name, args, ws_binding)
        except RemoteToolAdapterError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        profile = str(args.get("profile") or "local_restricted")
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
            ),
            limits=_execution_limits(name, args),
            execution_type=str(ExecutionType.DIRECT),
            command=command,
            cwd=None,
            profile=profile,
        )
        # P0-B/J: block only within the direct sync window; a longer command
        # returns its execution_id immediately and survives client disconnect.
        await self.jobs.wait(record.execution_id, timeout_s=direct_sync_window_s())
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
            worktree, repo_root = await self._ensure_isolated_worktree(
                session, ws_binding.repo_root, lane
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
            try:
                result = await run_direct_command(
                    worktree,
                    command,
                    cwd=cwd,
                    profile=profile,
                    timeout_s=timeout_s,
                    approved=approved,
                    network=network,
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
        self, session: RemoteSession, repo_root: str, lane: str = ""
    ) -> tuple[str, str]:
        """Create/verify the task worktree for ``repo_root`` (Phase 1.1).

        Uses the canonical :class:`WorktreeManager` (one worktree authority).
        ``lane`` isolates parallel L1 children (each gets its own worktree).
        Raises :class:`ExecutionBlocked` on any failure; never returns the owner
        repo root.
        """

        key = canonical(repo_root)
        cache_key = key if not lane else f"{key}::{lane}"
        lock = self._worktree_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            try:
                manager = await asyncio.to_thread(WorktreeManager, key)
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
                    record = await asyncio.to_thread(manager.status, path=candidate)
                except WorktreeError:
                    record = None
                if record is not None and canonical(record.repo_root) == key:
                    if not self._path_within(Path(record.path), Path(key)):
                        raise ExecutionBlocked("WORKSPACE_DENIED", "WORKTREE_ESCAPES_WORKSPACE")
                    session.worktrees[cache_key] = record.path
                    return record.path, record.repo_root
            try:
                record = await asyncio.to_thread(
                    manager.create, task_id, "remote mcp direct command"
                )
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
            worker = str(item.get("worker") or "").strip().lower()
            task_text = str(item.get("task") or "")
            if worker not in _WORKER_TYPES or not task_text:
                return self._fail(
                    binding.name,
                    session,
                    RemoteErrorCode.INVALID_ARGUMENT,
                    f"task[{index}] needs a known worker ({sorted(_WORKER_TYPES)}) and a task",
                )
            children.append(
                self._submit_worker_child(
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

    def _submit_worker_child(
        self,
        session: RemoteSession,
        ws_binding: WorkspaceBinding,
        parent: Any,
        worker: str,
        task_text: str,
        item: dict[str, Any],
        index: int,
    ) -> Any:
        worker_type = _WORKER_TYPES[worker]
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
                    lane=lane,
                ),
                execution_type=str(ExecutionType.HICODE),
                execution_mode="direct_hicode",
                orchestrator="none",
                worker_type=worker_type,
                model_provider=provider,
                model=model,
                parent_execution_id=parent.execution_id,
            )
        else:
            requested_timeout_s = float(item.get("timeout_sec") or _DEFAULT_CLI_TIMEOUT_S)
            _, effective_timeout_s = _cli_worker_timeout_budgets(worker, requested_timeout_s)
            child = self.jobs.submit(
                session=session,
                tool="worker.dispatch",
                veya_tool=f"direct_{worker}",
                binding=ws_binding,
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
                    dependency_artifacts=dependency_artifacts,
                ),
                execution_type=str(ExecutionType.DIRECT),
                execution_mode=f"direct_{worker}",
                orchestrator="none",
                worker_type=worker_type,
                model_provider=provider,
                model=model,
                parent_execution_id=parent.execution_id,
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
    ) -> Any:
        async def runner(reporter: ProgressReporter) -> str:
            reporter.phase("STARTING", message="starting Hicode worker", event="STARTING")
            reporter.worker(
                execution_mode="direct_hicode",
                orchestrator="none",
                worker_type="HICODE",
                activity="Hicode worker started",
            )
            # Section 7: Hicode runs in the verified isolated task worktree, never
            # the owner repo. The worker receives exactly the validated workspace.
            worktree, verified_repo = await self._ensure_isolated_worktree(session, repo_root, lane)
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
                if stage == "planning":
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
                    from server.hicode_agent import _execute_hicode_core

                    output = await _execute_hicode_core(
                        worker_input,
                        workspace=worktree,
                        max_steps=int(args.get("max_steps") or 0),
                        timeout_sec=int(args.get("timeout_sec") or 0),
                        on_event=on_event,
                        force_cli=True,
                        on_process=on_process,
                    )
                hicode_failure = _hicode_failure_message(output)
                if hicode_failure is not None:
                    reporter.failure(
                        failure_class="HICODE_EXECUTION_FAILED",
                        source="hicode",
                        detail=hicode_failure,
                    )
                    self._write_task_memory_failure(memory, reporter._execution_id, hicode_failure)
                    raise ExecutionError("HICODE_FAILED", hicode_failure)
            except asyncio.CancelledError:
                # Kill exactly this execution's process group (reasonix + its
                # tool grandchildren); never the shared runtime/gateway.
                await terminate_process_group_id(owned["pgid"] or 0)
                raise
            except Exception as exc:
                reporter.failure(
                    failure_class="WORKER_EXECUTION_FAILED",
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
            reporter.phase("FINALIZING", message="Hicode finalizing", event="FINALIZING")
            self._write_task_memory_success(memory, reporter._execution_id, output)
            return output

        return runner

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
    ) -> Any:
        async def runner(reporter: ProgressReporter) -> str:
            reporter.phase("STARTING", message=f"starting {worker} worker", event="STARTING")
            reporter.worker(
                execution_mode=f"direct_{worker}",
                orchestrator="none",
                worker_type=worker.upper(),
                activity=f"{worker} worker started",
            )
            worktree, verified_repo = await self._ensure_isolated_worktree(session, repo_root, lane)
            staged_dependency_artifacts = await asyncio.to_thread(
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
            argv, env = _worker_command(worker, worker_input)
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
                reporter.failure(
                    failure_class="WORKER_TIMEOUT", source=worker, detail=timeout_detail
                )
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
                reporter.failure(
                    failure_class="WORKER_TIMEOUT", source=worker, detail=timeout_detail
                )
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
                raise
            finally:
                if "heartbeat_task" in locals():
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
            reporter.model_completed(activity=f"{worker} model finished")
            if proc.returncode not in (0, None):
                stderr_tail = "".join(stderr_lines).strip()[-1200:]
                detail = f"exit_code={proc.returncode}; stderr={stderr_tail}"
                reporter.failure(
                    failure_class="WORKER_EXECUTION_FAILED", source=worker, detail=detail
                )
                self._write_task_memory_failure(
                    memory,
                    reporter._execution_id,
                    detail,
                    action=retry_action,
                    error_class=retry_error_class,
                )
                raise ExecutionError("WORKER_FAILED", f"{worker} exited with {proc.returncode}")
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
            self._write_task_memory_success(memory, reporter._execution_id, summary)
            return summary

        return runner

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
        target = self._direct_workdir(session, ws_binding)
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

            workspace = await asyncio.to_thread(
                detect_workspace, str(target), owner_user_id="remote"
            )
            return {"path": str(target), "workspace": workspace.to_dict()}
        if name == "workspace.list":
            target = self._resolve_in(policy, base, args.get("path") or ".", must_exist=True)
            lines = await asyncio.to_thread(_list_workspace, target)
            text = "\n".join(lines) or "(empty)"
            output, truncated = self._limit(text)
            return {"path": str(target), "entries": lines, "text": output, "truncated": truncated}
        if name == "file.read":
            target = self._resolve_target(session, policy, base, args["path"], must_exist=True)
            content = await asyncio.to_thread(
                Path(target).read_text, encoding="utf-8", errors="replace"
            )
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
                lines = await asyncio.to_thread(_list_workspace, outputs_dir)
                text = "\n".join(lines) or "(empty)"
                output, truncated = self._limit(text)
                return {
                    "path": str(outputs_dir),
                    "entries": lines,
                    "text": output,
                    "truncated": truncated,
                }
            target = self._resolve_in(policy, str(outputs_dir), args["path"], must_exist=True)
            content = await asyncio.to_thread(
                Path(target).read_text, encoding="utf-8", errors="replace"
            )
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
                workspace_realpath=session.explicit_workspace,
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

    # ── failures ────────────────────────────────────────────────────
    def _fail(
        self,
        tool: str,
        session: RemoteSession | None,
        code: RemoteErrorCode,
        message: str,
    ) -> RemoteCallResult:
        return RemoteCallResult(
            ok=False,
            tool=tool,
            session_id=session.session_id if session else None,
            workspace=session.active_workspace if session else None,
            error_code=code,
            message=message,
        )


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


def _hicode_failure_message(output: Any) -> str | None:
    """Convert Hicode's legacy error-string results into failed executions."""

    text = str(output or "").strip()
    prefixes = (
        "hicode 不可用:",
        "hicode 执行失败:",
        "hicode 执行异常:",
        "错误:",
        "⚠ hicode 执行失败",
    )
    if text.startswith(prefixes):
        return text[:2000]
    return None


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
        return ExecutionPhase.TESTING
    if tool in {"shell.exec"}:
        return ExecutionPhase.EDITING
    return ExecutionPhase.PLANNING


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


def _hicode_model_identity() -> tuple[str, str]:
    """Provider/model identity for the direct_hicode worker (P1-I, no secrets)."""

    model = os.environ.get("HICODE_REASONIX_MODEL") or os.environ.get("HICODE_MODEL") or "unknown"
    base = os.environ.get("HICODE_REASONIX_BASE_URL", "")
    if "opencode" in base:
        provider = "opencode-go"
    elif base:
        provider = "local-gateway"
    else:
        provider = "unknown"
    return provider, model


def _worker_model_identity(worker: str) -> tuple[str, str]:
    """Provider/model identity for an L1 worker (no secrets)."""

    if worker == "hicode":
        return _hicode_model_identity()
    cfg = _CLI_WORKERS.get(worker)
    if cfg:
        return str(cfg["provider"]), str(cfg["model"])
    return "unknown", "unknown"


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


def _worker_command(worker: str, task: str) -> tuple[list[str], dict[str, str]]:
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
                "DSH_PROVIDER": "VEYA_LOCAL_GATEWAY",
                "DSH_BASE_URL": dsh_plane.DEFAULT_BASE_URL,
                "DSH_MODEL": "veya1.2",
                "DSH_API_KEY": dsh_plane.api_key(dsh_cfg),
            }
        )
        dsh_env = dsh_plane.subprocess_env(dsh_cfg)
        return dsh_plane.dsh_argv(bin_path, task, dsh_cfg), dsh_env
    if worker == "pi":
        bin_path = shutil.which("pi") or "pi"
        return [
            bin_path,
            "-p",
            task,
            "--provider",
            "veya",
            "--model",
            "veya1.2",
            "--tools",
            "read,bash,edit,write",
            "--approve",
        ], dict(os.environ)
    if worker == "grok":
        bin_path = shutil.which("grok") or str(Path.home() / ".grok/bin/grok")
        grok_env = dict(os.environ)
        grok_env["GROK_SESSION_DIR"] = str(Path.home() / ".grok/sessions")
        return [
            bin_path,
            "-p",
            task,
            "--model",
            "veya1.2",
            "--tools",
            "read_file,search_replace,grep,list_dir,run_terminal_command",
            "--sandbox",
            "workspace",
            "--permission-mode",
            "bypassPermissions",
            "--always-approve",
        ], grok_env
    if worker == "codex":
        bin_path = shutil.which("codex") or "codex"
        return [
            bin_path,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            task,
        ], dict(os.environ)
    raise RemoteToolAdapterError(
        RemoteErrorCode.INVALID_ARGUMENT, f"no CLI command for worker {worker!r}"
    )


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
