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

The adapter never runs a keyword/semantic router.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.coding.command_runner import CommandPolicyError
from runtime.coding.worktree import WorktreeError, WorktreeManager
from runtime.execution.side_effects import SideEffectLedger
from server.hashline import DEFAULT_MAX_LINES, HARD_MAX_LINES
from veya.obase.async_utils import run_sync_in_daemon_thread
from veya.obase.compat import build_ripgrep_args
from veya.remote.qualification_faults import QualificationFault
from veya.remote.qualification_faults import checkpoint as qualification_checkpoint
from veya.remote.skills import SkillPermission
from veya.supervision.task_memory import TaskMemory

from .action_gateway import ActionCategory, ActionGateway, parse_systemctl_command
from .admission import admission_for_blocker
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
    ExecutionFailureClass,
    ExecutionPhase,
    ExecutionStatus,
    ExecutionStore,
    ExecutionType,
    ProgressReporter,
    TimeoutKind,
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
from .execution_timeout import ExecutionPolicy
from .execution_worktree import ExecutionWorktreeRegistry
from .executor_health import (
    ExecutorFailureClass,
    ExecutorHealthRegistry,
    classify_failure,
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

# Search-window bounds.  ``max_results`` defaults to 40 because that is what
# ``_tool_grep`` has always returned; the ceiling is what makes the bound a
# bound rather than a default.
_FILE_SEARCH_DEFAULT_MAX_RESULTS = 40
_FILE_SEARCH_HARD_MAX_RESULTS = 1000
_FILE_SEARCH_HARD_MAX_CONTEXT = 20

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


def _git_pathspec(raw: Any) -> tuple[list[str], str | None]:
    """Validate a caller-supplied path filter into git pathspec arguments.

    ``git.diff`` and ``git.log`` both advertise a ``path`` argument and both used
    to drop it: the caller asked about one file and received the whole-tree
    answer under ``ok=true``, with nothing in the payload recording that the
    filter had been discarded. Measured against a worktree with edits in three
    files, ``path="veya/remote/execution.py"`` returned a diff the same length
    as the unfiltered one and still naming the other two files.

    Returns ``(pathspec, error)``. An absent or blank filter means "no filter"
    and yields an empty pathspec, which callers turn into no ``--`` argument at
    all. A filter that could read as an option, escape the worktree, or is not a
    string is refused rather than passed through.
    """
    if raw is None:
        return [], None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or not all(isinstance(item, str) for item in raw):
        return [], "path must be a string or a list of strings"
    values = [item.strip() for item in raw if item.strip()]
    if not values:
        return [], None
    for value in values:
        pure = PurePosixPath(value)
        if pure.is_absolute() or value.startswith("-") or ".." in pure.parts:
            return [], f"path must stay inside the worktree: {value!r}"
    return values, None


def _read_window(
    raw: Any, *, name: str, default: int, minimum: int = 1, hard_max: int | None = None
) -> tuple[int, str | None]:
    """Normalise one caller-supplied read/search window parameter.

    ``file.read``'s two paths disagreed about ``max_lines``: the ``read_hashline``
    path clamped to 8000 while the fast path passed the caller's number
    straight through, so ``max_lines=10**7`` returned 8000 lines on one path and
    the entire file on the other, both under ``ok=true``. One builder used by
    both is what makes it one bound instead of two.

    A caller asking for more than ``hard_max`` gets the ceiling rather than an
    error: the request is honoured as far as the contract allows, and the
    response says which line or result index to resume from, so the clamp is
    reported rather than silent. A caller sending something that is not a
    positive integer is refused, because silently reading a different range than
    the one asked for is worse than refusing.

    Returns ``(value, error)``.
    """
    if raw is None or raw == "":
        return default, None
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return default, f"{name} must be an integer"
    try:
        value = int(raw)
    except ValueError:
        return default, f"{name} must be an integer"
    if value < minimum:
        return default, f"{name} must be >= {minimum}"
    if hard_max is not None:
        value = min(value, hard_max)
    return value, None


def _rg_hit_records(
    stdout: str,
    *,
    max_results: int,
    context_before: int = 0,
    context_after: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Turn ``rg --json`` output into ``{path, line, match, context}`` records.

    ripgrep emits context lines as their own ``context`` events rather than
    folding them into the match, so a record's context has to be assembled by
    line number. Records are capped here rather than with ``rg --max-count``,
    which limits matches *per file* and would let the total still grow with the
    number of files.

    Returns ``(records, truncated)`` where ``truncated`` means matches existed
    beyond ``max_results`` — not that the output was clipped for space.
    """
    context: dict[tuple[str, int], str] = {}
    matches: list[tuple[str, int, str]] = []
    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind not in ("match", "context"):
            continue
        data = event.get("data") or {}
        path = str((data.get("path") or {}).get("text", ""))
        try:
            line_no = int(data.get("line_number"))
        except (TypeError, ValueError):
            continue
        body = str((data.get("lines") or {}).get("text", "")).rstrip("\n")
        if kind == "context":
            context[(path, line_no)] = body
        else:
            matches.append((path, line_no, body))

    truncated = len(matches) > max_results
    records: list[dict[str, Any]] = []
    for path, line_no, body in matches[:max_results]:
        before = range(max(1, line_no - context_before), line_no)
        after = range(line_no + 1, line_no + 1 + context_after)
        around = [
            f"{n}: {context[(path, n)]}" for n in before if (path, n) in context
        ] + [f"{line_no}: {body}"] + [
            f"{n}: {context[(path, n)]}" for n in after if (path, n) in context
        ]
        records.append(
            {
                "path": path,
                "line": line_no,
                "match": body,
                "context": around,
            }
        )
    return records, truncated


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
    *,
    intent: str = "read",
) -> str:
    """Canonical execution-target resolver (P0-B).

    Rules (in priority order):
    1. A non-empty ``requested_execution_target`` is honoured only if it is one of
       ``EXECUTION_TARGETS``; anything else raises ``INVALID_ARGUMENT`` rather
       than degrading to the auto-detected default.
    2. ``workspace`` itself is a linked worktree (``.git`` is a file pointing into
       ``<main>/.git/worktrees/*``) → ``EXISTING_WORKTREE``.
    3. ``workspace_path`` resolves to a directory inside ``.veya/worktrees/``
       of some parent repo → ``EXISTING_WORKTREE``.
    4. Otherwise → ``CANONICAL_WORKTREE`` for read/execute intent, and
       ``NEW_ISOLATED_WORKTREE`` for ``intent="mutation"``.

    Read and execute default to canonical. A fresh worktree is a clean checkout:
    it carries no untracked source, no local virtualenv, and none of the current
    working state, so defaulting execution to it made the current tree's own
    modules unimportable while qualifying it.

    Mutation keeps the isolated default. Writing into the owner's live tree by
    default would drop the cross-repo write guard and the stale-write isolation
    that ``file.write`` / ``file.patch`` / worker children rely on, which widens
    the write boundary rather than narrowing it. A caller that genuinely means to
    mutate the canonical tree must still pass ``CANONICAL_WORKTREE`` explicitly.

    This is the *only* place that maps a workspace/path to an execution target.
    All callers (shell.exec, test.run, build.run, file.write, file.patch) must
    use this function instead of guessing individually.
    """
    explicit = str(requested_execution_target or "").strip().upper()
    if explicit:
        if explicit not in EXECUTION_TARGETS:
            raise RemoteToolAdapterError(
                RemoteErrorCode.INVALID_ARGUMENT,
                f"unknown execution_target {explicit!r}; expected one of: "
                f"{', '.join(EXECUTION_TARGETS)}",
            )
        return explicit

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

    if str(intent or "read").strip().lower() in {"mutation", "write", "mutate"}:
        return "NEW_ISOLATED_WORKTREE"
    return "CANONICAL_WORKTREE"


_ISOLATED_TARGETS = frozenset({"NEW_ISOLATED_WORKTREE", "EXECUTION_WORKTREE", "EXISTING_WORKTREE"})


def _target_activity(target_type: str) -> str:
    """Human-readable label for the checkout an execution actually ran in."""

    if target_type in _ISOLATED_TARGETS:
        return "isolated worktree ready"
    if target_type in ("CANONICAL_WORKTREE", "HOST"):
        return "canonical worktree ready"
    return f"worktree ready ({target_type})"


def _worktree_dirty_state(worktree_path: str | Path | None) -> bool | None:
    """Whether ``worktree_path`` carried local state at dispatch time.

    ``git status`` is asked directly, so a linked worktree (whose ``.git`` is a
    file) and a main checkout (whose ``.git`` is a directory) are both answered
    by git rather than by a guess. ``None`` means "could not be determined",
    which the receipt keeps distinct from "clean".
    """

    if not worktree_path:
        return None
    root = Path(worktree_path)
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


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
# semantics; none of these is a wrapper around Hicode.  Hicode is retired
# from the active plane (source retained); claude_code is the canonical
# Claude Code executor.
def _worker_types() -> dict[str, str]:
    """Executor id -> the label reported in receipts.

    Derived from ExecutorRegistry rather than hand-listed. A parallel dict is a
    second inventory: it had already drifted (the registry admits ``acp``, the
    literal did not), and because it is also used as a lookup gate, drift
    surfaces as a KeyError mid-dispatch instead of a clean refusal.
    """

    return {eid: eid.upper() for eid in get_executor_registry().snapshot()}


class _WorkerTypes:
    """Mapping view that always reflects the current registry membership."""

    def __getitem__(self, worker: str) -> str:
        types = _worker_types()
        if worker not in types:
            raise KeyError(f"{worker!r} is not an admitted executor; admitted: {sorted(types)}")
        return types[worker]

    def __contains__(self, worker: object) -> bool:
        return worker in _worker_types()

    def get(self, worker: str, default: Any = None) -> Any:
        return _worker_types().get(worker, default)

    def __iter__(self):
        return iter(_worker_types())

    def keys(self):  # pragma: no cover - convenience for diagnostics
        return _worker_types().keys()

    def items(self):  # pragma: no cover - convenience for diagnostics
        return _worker_types().items()

    def __len__(self) -> int:
        return len(_worker_types())


_WORKER_TYPES = _WorkerTypes()
# Runtime blockers are evidence-driven and temporary. Never keep stale provider
# outage/quota snapshots after a worker has been re-qualified.
_WORKER_BLOCKERS: dict[str, str] = {}


def _cli_workers() -> dict[str, dict[str, str]]:
    """Runtime projection of the CLI workers this adapter supports.

    Reads ``ExecutorRegistry.snapshot()`` and nothing else: ``snapshot()`` is a
    read-only view, so projecting from it can never admit an executor.  The
    previous construction called ``identity()`` at import time, which made
    discovery load-bearing -- a worker the registry had not admitted was
    silently discovered *into* the registry just by importing this module.

    ``_WORKER_TYPES`` still states which workers this adapter supports; it is
    deliberately not an authority for which executors exist.
    """
    snapshot = get_executor_registry().snapshot()
    result: dict[str, dict[str, str]] = {}
    for worker in _WORKER_TYPES:
        executor = snapshot.get(worker)
        if executor is None:
            # Not admitted by the registry: report nothing rather than resolving
            # (and thereby admitting) it.
            continue
        result[worker] = {
            "provider": executor.provider or "unknown",
            "model": executor.model or "unknown",
        }
    return result


# Compatibility projection for older callers, computed once at import from the
# registry snapshot. Never consulted as an authority.
_CLI_WORKERS = _cli_workers()
_TIMEOUT_SEPARATED_CLI_WORKERS = frozenset(
    {"pi", "grok", "codex", "antigravity", "opencode", "claude_code"}
)
_DEFAULT_CLI_TIMEOUT_S = 600.0
_DSH_INACTIVITY_TIMEOUT_S = 120.0


def _cli_worker_timeout_budgets(worker: str, requested_timeout_s: float) -> tuple[float, float]:
    """Return ``(inactivity_timeout, hard_max_runtime)`` for a CLI worker.

    Pi and Grok can spend several minutes in provider-side tool activity before
    their headless CLI emits a final response. Their old single wall-clock
    timeout killed that active work. DSH stays bounded at its existing hard
    limit, but a quiet runaway is stopped earlier.
    """

    requested = max(1.0, float(requested_timeout_s))
    policy = ExecutionPolicy.from_legacy(
        requested,
        separated_cli=worker in _TIMEOUT_SEPARATED_CLI_WORKERS,
    )
    return policy.idle_timeout_ms / 1000.0, policy.max_runtime_ms / 1000.0


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
        "Read a bounded window of a file with per-line LINE#hash tags (used by file.patch for stale-safe edits).",
        _obj(
            {
                "path": _STR,
                "start_line": {
                    "type": "integer",
                    "description": "first 1-based line to return (default 1)",
                },
                "max_lines": {
                    "type": "integer",
                    "description": f"cap on returned lines (default 2000, ceiling {HARD_MAX_LINES})",
                },
            },
            ["path"],
        ),
    ),
    ToolBinding(
        "file.search",
        "grep",
        EffectClass.READ,
        "Search the workspace with ripgrep. Returns path:line matches.",
        _obj(
            {
                "pattern": _STR,
                "glob": _STR,
                "path": _STR,
                "max_results": {
                    "type": "integer",
                    "description": (
                        f"cap on returned matches (default 40, ceiling {_FILE_SEARCH_HARD_MAX_RESULTS})"
                    ),
                },
                "context_before": {
                    "type": "integer",
                    "description": f"lines of leading context, 0..{_FILE_SEARCH_HARD_MAX_CONTEXT}",
                },
                "context_after": {
                    "type": "integer",
                    "description": f"lines of trailing context, 0..{_FILE_SEARCH_HARD_MAX_CONTEXT}",
                },
            },
            ["pattern"],
        ),
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
                            "task_contract": {
                                "type": "object",
                                "description": "Caller-declared L1 effect/verification contract; preserved as received.",
                                "additionalProperties": True,
                            },
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
                "dispatch_id": {
                    "type": "string",
                    "description": "Stable idempotency key. Retries return the same durable execution.",
                },
                "fail_fast": {"type": "boolean"},
                "workspace": _STR,
                "path": _STR,
                "execution_target": _EXECUTION_TARGET_SCHEMA,
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
        self._blocked_sweeper: asyncio.Task | None = None
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

    def resource_metrics(self) -> dict[str, Any]:
        """D5 gate input: live resource counters, read from the real objects.

        No inference: every number comes from the live execution manager, the
        live worktree registry, or the live durable repository.  A leak claim is
        "these counters returned to their pre-round baseline", not "the code
        looks like it releases".
        """
        metrics: dict[str, Any] = dict(self.jobs.metrics_snapshot())
        metrics.update(self.execution_worktrees.lease_metrics())
        metrics["active_executions"] = self.jobs.unfinished_count()
        metrics["worker_permits_in_use"] = metrics["active_executions"]
        metrics["leased_worker_permits"] = metrics["active_worktree_leases"]
        repository = getattr(self.side_effect_ledger, "repository", None)
        if repository is not None and hasattr(repository, "sqlite_metrics"):
            metrics.update(repository.sqlite_metrics())
        return metrics

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

    def _start_blocked_sweeper(self) -> None:
        """Converge long-resting BLOCKED projections to a terminal status.

        ``BLOCKED`` is terminal in phase, so ``reconcile_unfinished`` skips it and
        the projection would otherwise stay BLOCKED for the life of the process.

        The sweeper is demand-driven: it runs only while a BLOCKED record exists
        and exits as soon as the last one is converged, so a healthy process
        carries no idle background task.
        """

        if self._blocked_sweeper is not None and not self._blocked_sweeper.done():
            return
        if self.jobs.blocked_record_count() <= 0:
            return
        try:
            interval = float(os.environ.get("VEYA_BLOCKED_SWEEP_INTERVAL_S", "60") or 60)
        except ValueError:
            interval = 60.0

        async def sweep() -> None:
            while self.jobs.blocked_record_count() > 0:
                try:
                    await asyncio.sleep(max(5.0, interval))
                    if self.jobs.blocked_record_count() <= 0:
                        return
                    await run_sync_in_daemon_thread(self.jobs.sweep_blocked_records)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.getLogger("veya.remote.adapter").exception(
                        "blocked-record sweep failed"
                    )

        try:
            self._blocked_sweeper = asyncio.create_task(sweep(), name="veya-blocked-sweeper")
        except RuntimeError:
            self._blocked_sweeper = None

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
                from server.goal_run.pre_admission import reconcile_unbound_runs

                unbound_reconciled = reconcile_unbound_runs(
                    os.environ.get("VEYA_WORKSPACE_ROOT", os.getcwd()),
                    reason="backend restarted before execution persistence",
                )
                reconciliation = self.jobs.reconcile_unfinished()
                # Finish any worktree release interrupted by a crash between
                # "terminal persisted" and "worktree removed".  Only durably
                # terminal records are eligible, and keep_worktree is honoured.
                worktree_reconciled = self.jobs.reconcile_worktrees()
                prelaunch_reconciled = self.jobs.reconcile_prelaunch()
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
                        "prelaunch_reconciled": prelaunch_reconciled,
                        "unbound_pre_admissions_reconciled": unbound_reconciled,
                        "worktrees_released": worktree_reconciled.get("released", 0),
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
                    "prelaunch_reconciled": prelaunch_reconciled,
                    "unbound_pre_admissions_reconciled": unbound_reconciled,
                    "worktrees_released": worktree_reconciled.get("released", 0),
                    "failures": failures,
                }
                self._startup_complete = True
                self._start_blocked_sweeper()
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
            waited = wait_s is None or wait_s > 0
            if waited:
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
            if waited and not record.is_terminal:
                # The caller asked to block for the result and no terminal result
                # arrived. This used to fall through to ok=True/accepted=True,
                # handing back an unfinished job in a success envelope: the caller
                # had asked to wait for a result and received a receipt for the
                # submission instead, with nothing in the payload saying so.
                # Failing explicitly keeps the execution_id — the only way to keep
                # watching it — while refusing to call it a success.
                bounded = wait_s is not None
                pending = self._fail(
                    name,
                    session,
                    RemoteErrorCode.TIMEOUT if bounded else RemoteErrorCode.EXECUTION_FAILED,
                    (
                        f"waited {wait_s:g}s and the execution is still {record.status}"
                        if bounded
                        else f"wait returned with the execution still {record.status}"
                    )
                    + "; poll process.status with this execution_id",
                )
                pending.result = {**snapshot, "accepted": False, "terminal": False}
                pending.execution_id = record.execution_id
                pending.duration_ms = (time.time() - started) * 1000
                return pending
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
        elif name in _FAST_GIT_TOOLS or name in _COMMAND_TOOLS or name == "worker.dispatch":
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
                intent="mutation",
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
            window, error = _read_window(
                args.get("max_lines"),
                name="max_lines",
                default=DEFAULT_MAX_LINES,
                hard_max=HARD_MAX_LINES,
            )
            if error:
                raise RemoteToolAdapterError(RemoteErrorCode.INVALID_ARGUMENT, error)
            begin, error = _read_window(args.get("start_line"), name="start_line", default=1)
            if error:
                raise RemoteToolAdapterError(RemoteErrorCode.INVALID_ARGUMENT, error)
            return {"filepath": str(target), "max_lines": window, "start_line": begin}
        if name == "file.search":
            root = self._resolve_target(
                session, policy, base, args.get("path") or ".", must_exist=True
            )
            kwargs: dict[str, Any] = {"pattern": str(args["pattern"]), "root": str(root)}
            if args.get("glob"):
                kwargs["glob"] = str(args["glob"])
            kwargs.update(self._search_window_args(args))
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

    def _search_window_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """Normalise the three ``file.search`` window arguments, or refuse.

        Shared by the adapter path and the direct fast path so a search cannot
        mean one thing on one path and something else on the other.
        """
        out: dict[str, Any] = {}
        specs = (
            ("max_results", _FILE_SEARCH_DEFAULT_MAX_RESULTS, 1, _FILE_SEARCH_HARD_MAX_RESULTS),
            ("context_before", 0, 0, _FILE_SEARCH_HARD_MAX_CONTEXT),
            ("context_after", 0, 0, _FILE_SEARCH_HARD_MAX_CONTEXT),
        )
        for name, default, minimum, hard_max in specs:
            value, error = _read_window(
                args.get(name), name=name, default=default, minimum=minimum, hard_max=hard_max
            )
            if error:
                raise RemoteToolAdapterError(RemoteErrorCode.INVALID_ARGUMENT, error)
            out[name] = value
        return out

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
            reporter.set_target(execution_target, _worktree_dirty_state(worktree))
            reporter.worker(
                execution_mode="direct_command",
                orchestrator="none",
                worker_type="HOST",
                activity=f"{_target_activity(execution_target)}: {worktree}",
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
                failure_class = _direct_failure_class(result)
                reporter.failure(
                    failure_class=failure_class,
                    source="direct_command",
                    detail=(
                        result.stderr_tail[-4000:]
                        or result.stdout_tail[-4000:]
                        or f"direct command {result.status} (exit_code={result.exit_code})"
                    ),
                )
                if failure_class == str(ExecutionFailureClass.PROCESS_TIMEOUT):
                    # A deadline that expired is a timeout, not a generic
                    # failure, and the receipt has to say which budget ran out.
                    reporter.timeout(kind=str(TimeoutKind.PROCESS), seconds=timeout_s)
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
        execution_target: str = "CANONICAL_WORKTREE",
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
        # The dispatch entry resolves the execution target exactly once and
        # hands the decision to every child. Children must not re-derive it.
        child_execution_target = resolve_execution_target(
            ws_binding.repo_root or ws_binding.requested_realpath,
            workspace_path=getattr(ws_binding, "canonical_target_path", None) or None,
            requested_execution_target=str(args.get("execution_target") or ""),
            intent="mutation",
        )
        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return self._fail(
                binding.name,
                session,
                RemoteErrorCode.INVALID_ARGUMENT,
                "tasks must be a non-empty list",
            )
        dispatch_id = str(args.get("dispatch_id") or f"dispatch_{uuid.uuid4().hex}")
        failure_mode = "fail_fast" if args.get("fail_fast") else "collect_all"
        # Validate the complete admission before creating any durable child.
        # This prevents partial admission and duplicate side effects on a
        # malformed retry.
        validated: list[tuple[int, str, str, dict[str, Any]]] = []
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
            validated.append((index, worker, task_text, item))
        pre_admission = None
        parent = None

        def _fault_failure(exc: QualificationFault) -> RemoteCallResult:
            from server.goal_run.pre_admission import fail_pre_admission

            if pre_admission is not None:
                with contextlib.suppress(Exception):
                    fail_pre_admission(
                        project_root=project_root,
                        goal_run_id=pre_admission.goal_run_id,
                        reason=str(exc),
                    )
            if parent is not None and not parent.is_terminal:
                self.jobs._finish(
                    parent,
                    str(ExecutionStatus.FAILED),
                    message=str(exc),
                    error="QUALIFICATION_FAULT",
                )
            return self._fail(
                binding.name,
                session,
                RemoteErrorCode.EXECUTION_FAILED,
                str(exc),
            )

        try:
            qualification_checkpoint(
                "AFTER_VALIDATE",
                dispatch_id=dispatch_id,
                session_id=session.session_id,
                requested_executor=validated[0][1],
            )
        except QualificationFault as exc:
            return _fault_failure(exc)
        from server.goal_run.pre_admission import (
            bind_execution,
            create_pending_run,
        )

        project_root = ws_binding.repo_root or ws_binding.requested_realpath
        requested_executor = validated[0][1] if len(validated) == 1 else "parallel"
        try:
            qualification_checkpoint(
                "BEFORE_GOALRUN_PRECREATE",
                dispatch_id=dispatch_id,
                session_id=session.session_id,
                requested_executor=requested_executor,
            )
            pre_admission = create_pending_run(
                project_root=project_root,
                dispatch_id=dispatch_id,
                session_id=session.session_id,
                requested_executor=requested_executor,
                tasks=[
                    {"worker": worker, "task": task_text, "task_contract": dict(item.get("task_contract") or {})}
                    for _index, worker, task_text, item in validated
                ],
            )
            qualification_checkpoint(
                "AFTER_GOALRUN_PRECREATE",
                dispatch_id=dispatch_id,
                goal_run_id=pre_admission.goal_run_id,
                goal_task_id=pre_admission.goal_task_id,
            )
        except QualificationFault as exc:
            return _fault_failure(exc)
        existing_parent = self.jobs.lookup_dispatch(dispatch_id)
        try:
            qualification_checkpoint(
                "BEFORE_EXECUTION_PERSIST",
                dispatch_id=dispatch_id,
                goal_run_id=pre_admission.goal_run_id,
                goal_task_id=pre_admission.goal_task_id,
            )
            parent = self.jobs.create_parent(
                session=session,
                tool=binding.name,
                binding=ws_binding,
                failure_mode=failure_mode,
                dispatch_id=dispatch_id,
                executor_id=requested_executor,
                goal_run_id=pre_admission.goal_run_id,
                goal_task_id=pre_admission.goal_task_id,
                goal_project_root=project_root,
                task_contract=(dict(validated[0][3].get("task_contract") or {}) if len(validated) == 1 else {"tasks": [dict(item.get("task_contract") or {}) for _, _, _, item in validated]}),
            )
            qualification_checkpoint(
                "AFTER_EXECUTION_PERSIST",
                dispatch_id=dispatch_id,
                execution_id=parent.execution_id,
                goal_run_id=parent.goal_run_id,
                goal_task_id=parent.goal_task_id,
            )
        except QualificationFault as exc:
            return _fault_failure(exc)
        if existing_parent is not None and parent.child_execution_ids:
            # Durable idempotent replay: never start a second worker batch.
            children = [self.jobs.lookup(child_id) for child_id in parent.child_execution_ids]
            children = [child for child in children if child is not None]
        else:
            if pre_admission.state.status.value == "failed":
                return self._fail(
                    binding.name,
                    session,
                    RemoteErrorCode.EXECUTION_FAILED,
                    pre_admission.state.last_stop_reason
                    or "canonical GoalRun pre-admission failed",
                )
            if existing_parent is None:
                bind_execution(pre_admission, parent.execution_id, project_root)
            self.jobs.transition_lifecycle(parent.execution_id, "VALIDATED")
            self.jobs.transition_lifecycle(parent.execution_id, "ADMITTED")
            try:
                qualification_checkpoint(
                    "AFTER_ADMISSION",
                    dispatch_id=dispatch_id,
                    execution_id=parent.execution_id,
                    goal_run_id=parent.goal_run_id,
                    goal_task_id=parent.goal_task_id,
                )
            except QualificationFault as exc:
                return _fault_failure(exc)
            self.jobs.transition_lifecycle(parent.execution_id, "PERSISTED")
            for index, worker, task_text, item in validated:
                try:
                    qualification_checkpoint(
                        "BEFORE_WORKER_LAUNCH",
                        dispatch_id=dispatch_id,
                        execution_id=parent.execution_id,
                        goal_run_id=parent.goal_run_id,
                        goal_task_id=pre_admission.task_ids[index],
                        executor_id=worker,
                    )
                except QualificationFault as exc:
                    return _fault_failure(exc)
                children.append(
                    await self._submit_worker_child(
                        session,
                        ws_binding,
                        parent,
                        worker,
                        task_text,
                        item,
                        index,
                        dispatch_id=dispatch_id,
                        goal_run_id=pre_admission.goal_run_id,
                        goal_task_id=pre_admission.task_ids[index],
                        goal_project_root=project_root,
                        execution_target=child_execution_target,
                    )
                )
                try:
                    qualification_checkpoint(
                        "AFTER_WORKER_LAUNCH",
                        dispatch_id=dispatch_id,
                        execution_id=children[-1].execution_id,
                        goal_run_id=parent.goal_run_id,
                        goal_task_id=children[-1].goal_task_id,
                        executor_id=worker,
                    )
                except QualificationFault as exc:
                    return _fault_failure(exc)
            self.jobs.transition_lifecycle(parent.execution_id, "DISPATCHED")
        payload = {
            "accepted": True,
            "dispatch_id": dispatch_id,
            "parent_execution_id": parent.execution_id,
            "execution_id": parent.execution_id,
            "goal_run_id": parent.goal_run_id,
            "goal_task_id": parent.goal_task_id,
            "session_id": parent.session_id,
            "executor_id": parent.worker_id,
            "status": parent.lifecycle_state,
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
        dispatch_id: str,
        goal_run_id: str,
        goal_task_id: str,
        goal_project_root: str,
        execution_target: str = "CANONICAL_WORKTREE",
    ) -> Any:

        from veya.remote.execution_contract import (
            ExecutionCapabilityEnvelope,
            ExecutionSpec,
            probe_runtime_capability_manifest_cached,
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

        manifest_snapshot = await run_sync_in_daemon_thread(
            probe_runtime_capability_manifest_cached,
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
                execution_target=execution_target,
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
            idempotency_key=f"{dispatch_id}:child:{index}",
            dispatch_id=f"{dispatch_id}:child:{index}",
            goal_run_id=goal_run_id,
            goal_task_id=goal_task_id,
            goal_project_root=goal_project_root,
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
        # Admission refusals never reached execution, so they must not be
        # reported as a runtime block. Every blocker routed here is a refusal
        # at the admission gate: a capability the executor lacks, a worker the
        # registry will not admit, or a policy that declined the dispatch.
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
            # Label the refusal with *why* admission declined. The lifecycle
            # state is unchanged: splitting the semantics is a separate step,
            # and doing both at once is what broke three suites last time.
            reporter.admission(admission_for_blocker(blocker, blocker))
            raise ExecutionBlocked(blocker, blocker)

        self._start_blocked_sweeper()
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
        execution_target: str = "CANONICAL_WORKTREE",
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
                session,
                repo_root,
                lane,
                execution_target=execution_target,
                target_path=None,
                execution_id=None
                if execution_target in ("CANONICAL_WORKTREE", "HOST")
                else reporter._execution_id,
            )
            staged_dependency_artifacts = await run_sync_in_daemon_thread(
                _stage_dependency_artifacts,
                worktree,
                repo_root,
                dependency_artifacts or [],
            )
            reporter.set_worktree(worktree, verified_repo)
            reporter.set_target(execution_target, _worktree_dirty_state(worktree))
            reporter.worker(
                execution_mode=f"direct_{worker}",
                orchestrator="none",
                worker_type=worker.upper(),
                activity=f"{_target_activity(execution_target)}: {worktree}",
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
                # Attribute to exactly one layer. A provider refusal (429 /
                # 400 / 503 / timeout) is not an executor fault: the executor
                # launched cleanly and was turned away upstream, so the
                # observation belongs to ProviderRegistry and the receipt must
                # carry the provider code, never a generic WORKER_FAILED.
                attribution = classify_failure(exit_code=proc.returncode, detail=detail)
                if attribution.provider_failure_class is not None:
                    provider_code = str(attribution.provider_failure_class)
                    _record_provider_observation(worker, provider_code, detail)
                    reporter.failure(
                        failure_class=provider_code,
                        source=worker,
                        detail=detail,
                        code=provider_code,
                    )
                    self._write_task_memory_failure(
                        memory,
                        reporter._execution_id,
                        detail,
                        action=retry_action,
                        error_class=retry_error_class or provider_code,
                    )
                    raise ExecutionError(provider_code, f"{worker} provider failed: {detail[:500]}")
                executor_code = str(attribution.executor_failure_class)
                self.health_registry.record_failure(worker, executor_code, detail=detail)
                reporter.failure(
                    failure_class=executor_code,
                    source=worker,
                    detail=detail,
                    code=executor_code,
                )
                self._write_task_memory_failure(
                    memory,
                    reporter._execution_id,
                    detail,
                    action=retry_action,
                    error_class=retry_error_class or executor_code,
                )
                raise ExecutionError(executor_code, f"{worker} exited with {proc.returncode}")
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
            summary = _worker_result_summary(worker, stdout_lines)
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
                task_contract=(task_contract.to_dict() if task_contract else {}),
                tool_calls=_worker_tool_events(worker, stdout_lines),
                shell_calls=_worker_shell_events(worker, stdout_lines),
                file_writes=changed if worker in {"opencode", "claude_code"} else [],
            )
            # Persist telemetry for every task kind; READ tasks never reach the
            # finalizer, so without this their receipt would be dropped.
            contract = task_contract or L1TaskContract()
            declared_kind = str(contract.task_kind or TaskKind.READ).upper()
            observed_kind = str(receipt.task_kind or TaskKind.READ).upper()
            if declared_kind in {str(TaskKind.WRITE), str(TaskKind.TEST), str(TaskKind.BUILD)} and observed_kind != declared_kind:
                reporter.failure(failure_class="EFFECT_CONTRACT_MISMATCH", source=worker, detail=f"declared={declared_kind} observed={observed_kind}")
                raise ExecutionBlocked("EFFECT_CONTRACT_MISMATCH", f"task contract downgraded: declared={declared_kind} observed={observed_kind}")
            self.jobs.set_effect_receipt(reporter._execution_id, receipt.to_dict())
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
                pathspec, spec_error = _git_pathspec(args.get("path"))
                if spec_error is not None:
                    return self._fail(name, session, RemoteErrorCode.INVALID_ARGUMENT, spec_error)
                argv = ["git", "-C", target, "diff", "HEAD"]
                if pathspec:
                    # "--" so git reads the filter as a pathspec, never as an option.
                    argv += ["--", *pathspec]
                code, out, err = await self._run_capture(argv, timeout=30)
                text, truncated = self._limit(out)
                payload = {
                    "diff": text,
                    "truncated": truncated,
                    "exit_code": code,
                    # Echo the filter that was applied, so a caller can tell a
                    # scoped answer from a whole-tree one.
                    "pathspec": pathspec,
                }
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
                pathspec, spec_error = _git_pathspec(args.get("path"))
                if spec_error is not None:
                    return self._fail(name, session, RemoteErrorCode.INVALID_ARGUMENT, spec_error)
                argv = ["git", "-C", target, "log", f"-n{limit}", "--oneline"]
                if pathspec:
                    argv += ["--", *pathspec]
                code, out, err = await self._run_capture(argv, timeout=20)
                payload = {"log": out, "exit_code": code, "pathspec": pathspec}
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
            from veya.remote.runtime_profile import ExecutionDomain, discover_runtime_profile

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
                "available_domains": [str(domain.value) for domain in ExecutionDomain],
                # Derived from the one authority that resolves targets, not a
                # hand-maintained list. The literal this replaced named four of
                # the five targets in EXECUTION_TARGETS and omitted
                # EXECUTION_WORKTREE, so a caller reading the advertised surface
                # could not see a target it was allowed to ask for.
                "available_targets": list(EXECUTION_TARGETS),
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
            window, error = _read_window(
                args.get("max_lines"),
                name="max_lines",
                default=DEFAULT_MAX_LINES,
                hard_max=HARD_MAX_LINES,
            )
            if error:
                raise RemoteToolAdapterError(RemoteErrorCode.INVALID_ARGUMENT, error)
            begin, error = _read_window(args.get("start_line"), name="start_line", default=1)
            if error:
                raise RemoteToolAdapterError(RemoteErrorCode.INVALID_ARGUMENT, error)
            content = Path(target).read_text(encoding="utf-8", errors="replace")
            from server.hashline import render

            every = content.splitlines()
            returned = every[begin - 1 : begin - 1 + window]
            line_truncated = len(every) > begin - 1 + window
            rendered = render(content, max_lines=window, start_line=begin)
            text, byte_truncated = self._limit(f"[hashline {target}]\n{rendered}")
            result: dict[str, Any] = {
                "path": str(target),
                "text": text,
                "truncated": line_truncated or byte_truncated,
                "line_truncated": line_truncated,
                "byte_truncated": byte_truncated,
                "start_line": begin,
                "end_line": begin + len(returned) - 1,
                "lines": len(returned),
                "total_lines": len(every),
            }
            if line_truncated:
                result["next_start_line"] = begin + window
            return result
        if name == "file.search":
            root = self._resolve_target(
                session, policy, base, args.get("path") or ".", must_exist=True
            )
            limits = self._search_window_args(args)
            argv = build_ripgrep_args(
                str(args["pattern"]),
                root=str(root),
                glob=str(args["glob"]) if args.get("glob") else None,
                context_before=limits["context_before"],
                context_after=limits["context_after"],
            )
            code, out, err = await self._run_capture(argv, timeout=30)
            if code == 127 or "not found" in err:
                raise RemoteToolAdapterError(
                    RemoteErrorCode.EXECUTION_FAILED, "ripgrep (rg) is not installed"
                )
            results, hit_truncated = _rg_hit_records(
                out,
                max_results=limits["max_results"],
                context_before=limits["context_before"],
                context_after=limits["context_after"],
            )
            rendered = "\n".join(f"{r['path']}:{r['line']}: {r['match']}" for r in results)
            text, byte_truncated = self._limit(rendered)
            return {
                "root": str(root),
                "text": text,
                "results": results,
                "truncated": hit_truncated or byte_truncated,
                "hit_truncated": hit_truncated,
                "byte_truncated": byte_truncated,
                "exit_code": code,
            }
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
        if record.child_execution_ids:
            # L1 parent: mechanical aggregation only (no ranking/winner/JEV).
            aggregate = self.jobs.aggregate(record)
        payload = record.to_public(heartbeat_timeout_s=self.jobs.heartbeat_timeout_s)
        if record.child_execution_ids:
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

    def _read_mission_state(
        self, tool: str, session: RemoteSession, auto_dir: Path
    ) -> tuple[dict[str, Any], RemoteCallResult | None]:
        """Read a mission's state.json, or say precisely why it cannot be read.

        ``autonomous.status`` and ``autonomous.progress`` used to answer ok=true
        for a missing file — as ``status: NOT_FOUND`` and ``state: UNKNOWN`` —
        and let ``json.load`` raise out of the handler for a corrupt one. A
        caller could not tell a mission that had not started yet from a record
        that could not be read, and an unreadable record surfaced as an opaque
        execution failure instead of a state error.
        """
        sfile = auto_dir / "state.json"
        if not sfile.is_file():
            return {}, self._fail(
                tool,
                session,
                RemoteErrorCode.NOT_FOUND,
                f"mission state not written yet: {sfile}",
            )
        try:
            with open(sfile, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            return {}, self._fail(
                tool,
                session,
                RemoteErrorCode.EXECUTION_FAILED,
                f"mission state is unreadable: {sfile}: {exc}",
            )
        if not isinstance(data, dict) or "state" not in data:
            # The writer always stores the full AutonomousState snapshot, so a
            # record without "state" is a partial write, not an early mission.
            return {}, self._fail(
                tool,
                session,
                RemoteErrorCode.EXECUTION_FAILED,
                f"mission state is incomplete (no 'state' field): {sfile}",
            )
        return data, None

    # ── autonomous.* (spec §35) ─────────────────────────────────────
    async def _autonomous_call(
        self, session: RemoteSession, name: str, args: dict[str, Any], started: float
    ) -> RemoteCallResult:
        mission_id = str(args.get("mission") or "")
        project_root = Path(session.active_workspace or os.getcwd())
        if not mission_id:
            # Without a mission the path below collapses to the shared parent
            # directory, so a nameless read answers with another mission's
            # records. Refuse instead of guessing which one was meant.
            return self._fail(
                name,
                session,
                RemoteErrorCode.INVALID_ARGUMENT,
                "mission is required; it selects the .veya/autonomous/<mission> record set",
            )
        auto_dir = project_root / ".veya" / "autonomous" / mission_id
        if not auto_dir.is_dir():
            # "No such mission" and "this mission recorded nothing" were both
            # answered with ok=true and an empty or NOT_FOUND body.
            return self._fail(
                name,
                session,
                RemoteErrorCode.NOT_FOUND,
                f"no autonomous mission {mission_id!r} under {auto_dir}",
            )

        if name == "autonomous.status":
            state, failure = self._read_mission_state(name, session, auto_dir)
            if failure is not None:
                return failure
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result=dict(state),
                duration_ms=(time.time() - started) * 1000,
            )

        elif name == "autonomous.observations":
            limit = int(args.get("limit") or 50)
            from veya.autonomous.journal import ObservationJournal

            journal = ObservationJournal(auto_dir / "journal.jsonl")
            obs = [o.to_dict() for o in journal.query(mission_id, limit=limit)]
            body: dict[str, Any] = {"observations": obs}
            if journal.unreadable_records:
                # A damaged append-only journal must not read as "this mission
                # observed nothing".
                return self._fail(
                    name,
                    session,
                    RemoteErrorCode.EXECUTION_FAILED,
                    f"{journal.unreadable_records} observation record(s) could not be decoded"
                    f" ({journal.last_load_error})",
                    result=body,
                )
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result=body,
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
            state, failure = self._read_mission_state(name, session, auto_dir)
            if failure is not None:
                return failure
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result={
                    "mission_id": mission_id,
                    "objective": str(state.get("objective") or ""),
                    "accepted_progress": list(state.get("accepted_progress") or []),
                    "state": str(state["state"]),
                },
                duration_ms=(time.time() - started) * 1000,
            )

        elif name == "autonomous.waits":
            from veya.autonomous.wait import WaitConditionManager

            wm = WaitConditionManager(auto_dir / "waits.jsonl")
            waits = [w.to_dict() for w in wm.query(mission_id)]
            body: dict[str, Any] = {"mission_id": mission_id, "waits": waits}
            if wm.unreadable_records:
                # A corrupt journal must not read as "this mission never waited".
                return self._fail(
                    name,
                    session,
                    RemoteErrorCode.EXECUTION_FAILED,
                    f"{wm.unreadable_records} wait record(s) could not be decoded",
                    result=body,
                )
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=session.active_workspace,
                result=body,
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
    """Failure taxonomy for a non-passed direct command (P0-G).

    Uses the L0 taxonomy so a direct timeout reads the same as any other
    execution timeout, and a command that never started is distinguishable
    from one that started and then failed.
    """

    status = str(getattr(result, "status", "") or "")
    exit_code = getattr(result, "exit_code", None)
    stderr = str(getattr(result, "stderr_tail", "") or "")
    if status == "timeout" or getattr(result, "timed_out", False):
        return str(ExecutionFailureClass.PROCESS_TIMEOUT)
    if exit_code is None or "unable to execute command" in stderr:
        return str(ExecutionFailureClass.PROCESS_START_FAILURE)
    if exit_code in {-9, -15, 137, 143}:
        # killed by signal rather than exiting: the process stopped, it did not
        # return. That is a runtime failure, not a clean non-zero exit.
        return str(ExecutionFailureClass.PROCESS_RUNTIME_FAILURE)
    return str(ExecutionFailureClass.PROCESS_RUNTIME_FAILURE)


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
    if args.get("timeout_s"):
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

# Directories pruned by path, relative to the listing root.  ``.veya/worktrees``
# holds one git worktree per execution, so walking it made ``workspace.list``
# cost grow with total execution history rather than with the requested scope.
_PRUNED_SUBDIRS = frozenset({".veya/worktrees"})


def _list_workspace(target: Path, *, limit: int = 200, max_depth: int = 3) -> list[str]:
    """Bounded, noise-free directory listing (O(request scope)).

    The previous implementation used ``sorted(target.rglob("*"))`` and filtered
    noise *afterwards*.  ``rglob`` cannot prune, so every noisy subtree was fully
    traversed and materialised before the filter ran: measured p50 40.7s on the
    veya repo, which carries hundreds of execution worktrees.  This walks with
    ``os.scandir`` and refuses to descend into noise, so the cost is bounded by
    the requested scope and the entry budget.
    """
    lines: list[str] = []
    budget = max(1, limit) * 20
    truncated = False

    def walk(root: Path, rel_prefix: str, depth: int) -> None:
        nonlocal budget, truncated
        if depth > max_depth or truncated:
            return
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if budget <= 0:
                truncated = True
                return
            budget -= 1
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if entry.name in _NOISE_DIRS:
                    continue
                rel = f"{rel_prefix}{entry.name}"
                if rel in _PRUNED_SUBDIRS:
                    continue
                lines.append(f"{rel}/")
                walk(Path(entry.path), f"{rel}/", depth + 1)
            else:
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                lines.append(f"{rel_prefix}{entry.name} ({size}b)")
            if len(lines) >= limit:
                truncated = True
                return

    walk(target, "", 0)
    if truncated:
        lines.append("... (truncated)")
    return lines


def _worker_runtime_identity(worker: str) -> ExecutorRuntimeIdentity:
    """Return the one canonical identity projection used by dispatch."""

    return get_executor_registry().identity(worker)


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
    if configured:
        return configured
    identity = get_executor_registry().identity("opencode")
    model = identity.model
    if not model:
        return None
    if "/" in model:
        return model
    return f"{identity.provider}/{model}" if identity.provider else model


def _resolve_claude_binary() -> str:
    configured = os.environ.get("VEYA_CLAUDE_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise RuntimeError(f"VEYA_CLAUDE_BIN is not executable: {candidate}")

    discovered = shutil.which("claude")
    if discovered:
        return discovered
    candidate = Path.home() / ".local" / "bin" / "claude"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    raise RuntimeError("Claude Code CLI executable not found; set VEYA_CLAUDE_BIN")


def _claude_code_worker_env() -> dict[str, str]:
    """Run Claude Code with its native auth, without Veya endpoint leakage.

    Credentials are sourced by the CLI itself (``~/.claude/.credentials.json``
    OAuth or ``ANTHROPIC_*`` env / local bridge); no secret is copied into the
    worker environment.  Veya-internal LLM endpoint overrides are stripped so
    the worker can never be redirected to a second provider authority.
    """

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


def _record_provider_observation(worker: str, provider_code: str, detail: str) -> None:
    """Charge a provider refusal to the provider layer, never the executor.

    Mirrors the supervision runner's responsible-layer rule at the point where
    the evidence is richest (the worker's own stdout/stderr). A lazy import
    keeps provider state behind ProviderRegistry; an unknown provider is not
    this function's job to invent.
    """

    from veya.remote.provider_registry import (
        ProviderAvailability,
        ProviderHealthState,
        get_provider_registry,
        normalize_provider_name,
    )

    try:
        provider_name = get_executor_registry().identity(worker).provider
    except ValueError:
        return
    if not provider_name:
        return
    try:
        get_provider_registry().record_observation(
            normalize_provider_name(provider_name),
            health_state=str(ProviderHealthState.UNHEALTHY),
            availability=str(ProviderAvailability.UNAVAILABLE),
            failure_state=str(provider_code),
        )
    except ValueError:
        return


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
    if worker == "claude_code":
        if coding_mode and not worktree_path:
            raise RemoteToolAdapterError(
                RemoteErrorCode.WORKSPACE_DENIED,
                "Claude Code coding mode requires an execution worktree",
            )
        bin_path = _resolve_claude_binary()
        # No explicit --model: the local bridge maps the CLI default through
        # its own ANTHROPIC_DEFAULT_*_MODEL config, and the declared contract
        # model (claude-sonnet-4-5) is not a runtime identifier the bridge
        # accepts.  Model selection stays with the provider config, never
        # hardcoded in the adapter.
        argv = [
            bin_path,
            "--print",
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            "stream-json",
            "--verbose",
            task,
        ]
        return argv, _claude_code_worker_env()
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

    if worker not in {"opencode", "claude_code"}:
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
    # Claude Code stream-json: assistant message content blocks carry tool_use.
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name")
                    if isinstance(name, str):
                        return name
    return ""


def _worker_result_summary(worker: str, lines: list[str]) -> str:
    """Extract the final result text for workers with structured JSON output.

    Claude Code stream-json emits one JSON event per line; the final
    ``result`` event carries the clean assistant text.  Other workers keep
    their raw stdout as the summary.
    """

    if worker == "claude_code":
        for event in _json_worker_events(worker, lines):
            if event.get("type") == "result":
                result = event.get("result")
                if isinstance(result, str) and result.strip():
                    return result.strip()
        return ""
    return "".join(lines).strip()


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
    return await registry.execute(name, kwargs)


def default_tool_adapter(**kwargs: Any) -> RemoteToolAdapter:
    return RemoteToolAdapter(**kwargs)
