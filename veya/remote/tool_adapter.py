"""MCP tool -> canonical Veya runtime adapter (spec §2).

The single rule this module must never break: an MCP call *only* produces a
Veya tool name plus arguments for ``MasterToolRegistry.execute``. It does not
spawn processes, open files, run git or touch artifacts directly. Long-running
work is turned into a background task over the same canonical executor so a
dropped client connection cannot kill it.

Mutation is routed through Veya's canonical isolated coding worktree
(``coding_worktree_create`` / ``coding_run_command`` / ``coding_run_tests`` /
``coding_build`` / ``coding_diff``), matching the project rule that coding
changes happen in an isolated worktree.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import (
    EffectClass,
    JobState,
    RemoteCallResult,
    RemoteErrorCode,
    RemoteSession,
    ToolBinding,
)
from .workspace_policy import WorkspacePolicy, WorkspacePolicyError

VeyaExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]

_DEFAULT_OUTPUT_LIMIT = 200_000
_TASK_OUTPUTS = ".veya/runs"


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
        "git.status",
        "coding_worktree_status",
        EffectClass.READ,
        "Branch, cleanliness and changed files of the session worktree.",
        _obj({}),
        needs_git=True,
    ),
    ToolBinding(
        "git.diff",
        "coding_diff",
        EffectClass.READ,
        "Unified diff of the session worktree against HEAD.",
        _obj({}),
        needs_git=True,
    ),
    ToolBinding(
        "git.log",
        "coding_run_command",
        EffectClass.READ,
        "Recent commit history of the session worktree.",
        _obj({"limit": {"type": "integer"}}),
        needs_git=True,
    ),
    ToolBinding(
        "test.run",
        "coding_run_tests",
        EffectClass.WRITE,
        "Run the workspace test command in the session worktree and return evidence.",
        _obj({"command": _STR, "timeout_s": {"type": "number"}, "wait": {"type": "boolean"}}),
        long_running=True,
    ),
    ToolBinding(
        "build.run",
        "coding_build",
        EffectClass.WRITE,
        "Run the workspace build command in the session worktree and return evidence.",
        _obj({"command": _STR, "timeout_s": {"type": "number"}, "wait": {"type": "boolean"}}),
        long_running=True,
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
            {"task": _STR, "max_steps": {"type": "integer"}, "timeout_sec": {"type": "integer"}},
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
    return tuple(
        ToolBinding(
            name,
            veya_tool,
            effect,
            description,
            json.loads(json.dumps(schemas[veya_tool])) if veya_tool in schemas else _obj({}, []),
            long_running=long_running,
            needs_shell=long_running,
        )
        for name, veya_tool, effect, description, long_running in _SUPERVISION_BINDING_SPECS
    )


BINDINGS = BINDINGS + _supervision_bindings()

BINDING_INDEX: dict[str, ToolBinding] = {b.name: b for b in BINDINGS}


# ── long-running job manager ───────────────────────────────────────────
@dataclass
class RemoteJob:
    execution_id: str
    session_id: str
    tool: str
    veya_tool: str
    workspace: str
    state: JobState = JobState.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: str | None = None
    error_code: str | None = None
    message: str | None = None
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "session_id": self.session_id,
            "tool": self.tool,
            "veya_tool": self.veya_tool,
            "workspace": self.workspace,
            "state": str(self.state),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error_code": self.error_code,
            "message": self.message,
        }


class RemoteJobManager:
    """Background execution handles keyed by ``execution_id``.

    Jobs are owned by a session; status/cancel from another session is refused.
    """

    def __init__(self, *, max_jobs: int = 128) -> None:
        self._jobs: dict[str, RemoteJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._max_jobs = max(1, int(max_jobs))
        self._lock = threading.Lock()

    def start(
        self,
        *,
        session_id: str,
        tool: str,
        veya_tool: str,
        workspace: str,
        runner: Callable[[], Awaitable[str]],
    ) -> RemoteJob:
        job = RemoteJob(
            execution_id=f"ex_{uuid.uuid4().hex}",
            session_id=session_id,
            tool=tool,
            veya_tool=veya_tool,
            workspace=workspace,
        )
        with self._lock:
            self._evict_locked()
            self._jobs[job.execution_id] = job
            self._tasks[job.execution_id] = asyncio.ensure_future(self._run(job, runner))
        return job

    async def _run(self, job: RemoteJob, runner: Callable[[], Awaitable[str]]) -> None:
        job.state = JobState.RUNNING
        job.started_at = time.time()
        try:
            job.result = await runner()
            job.state = JobState.CANCELLED if job.cancelled else JobState.SUCCEEDED
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            job.error_code = str(RemoteErrorCode.CANCELLED)
            job.message = "execution cancelled"
        except TimeoutError:
            job.state = JobState.TIMEOUT
            job.error_code = str(RemoteErrorCode.TIMEOUT)
            job.message = "execution timed out"
        except Exception as exc:
            job.state = JobState.FAILED
            job.error_code = str(RemoteErrorCode.EXECUTION_FAILED)
            job.message = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = time.time()
            with self._lock:
                self._tasks.pop(job.execution_id, None)

    def status(self, execution_id: str, session_id: str) -> RemoteJob:
        with self._lock:
            job = self._jobs.get(execution_id)
        if job is None:
            raise RemoteToolAdapterError(RemoteErrorCode.NOT_FOUND, "unknown execution_id")
        if job.session_id != session_id:
            raise RemoteToolAdapterError(
                RemoteErrorCode.TOOL_DENIED, "execution belongs to another session"
            )
        return job

    async def cancel(self, execution_id: str, session_id: str) -> RemoteJob:
        job = self.status(execution_id, session_id)
        job.cancelled = True
        with self._lock:
            task = self._tasks.pop(execution_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if job.state in (JobState.PENDING, JobState.RUNNING):
            job.state = JobState.CANCELLED
            job.error_code = str(RemoteErrorCode.CANCELLED)
        return job

    async def wait(self, job: RemoteJob, *, timeout_s: float | None) -> None:
        with self._lock:
            task = self._tasks.get(job.execution_id)
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except TimeoutError:
            return

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [j.to_dict() for j in self._jobs.values() if j.session_id == session_id]

    def _evict_locked(self) -> None:
        if len(self._jobs) < self._max_jobs:
            return
        finished = [
            j for j in self._jobs.values() if j.state not in (JobState.PENDING, JobState.RUNNING)
        ]
        for job in sorted(finished, key=lambda j: j.created_at)[: max(1, len(finished) // 2)]:
            self._jobs.pop(job.execution_id, None)


# ── adapter ────────────────────────────────────────────────────────────
class RemoteToolAdapter:
    def __init__(
        self,
        executor: VeyaExecutor | None = None,
        *,
        output_limit: int = _DEFAULT_OUTPUT_LIMIT,
        redact: Callable[[Any], Any] | None = None,
        default_timeout_s: float = 900.0,
    ) -> None:
        self._executor = executor
        self._output_limit = max(1024, int(output_limit))
        self._redact = redact or (lambda value: value)
        self._default_timeout_s = default_timeout_s
        self.jobs = RemoteJobManager()

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
        started = time.time()
        args = dict(arguments or {})
        binding = BINDING_INDEX.get(name)
        if binding is None:
            return self._fail(
                name, session, RemoteErrorCode.TOOL_DENIED, "unknown or unavailable tool"
            )

        workspace = session.active_workspace
        try:
            policy = WorkspacePolicy(
                Path(workspace), session.permissions, extra_roots=_canonical_extra_roots()
            )
        except WorkspacePolicyError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)

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

        try:
            veya_tool, kwargs, write_root = await self._prepare(session, policy, binding, args)
        except RemoteToolAdapterError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        except WorkspacePolicyError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)

        # Long-running tools always run as a durable background job; ``wait``
        # only controls whether this HTTP call blocks for the result.
        if binding.long_running:
            wait_s = self._wait_seconds(args)
            job = self.jobs.start(
                session_id=session.session_id,
                tool=name,
                veya_tool=veya_tool,
                workspace=workspace,
                runner=lambda: self._invoke(veya_tool, kwargs, write_root),
            )
            if wait_s is None or wait_s > 0:
                await self.jobs.wait(job, timeout_s=wait_s)
            snapshot = self._redact(job.to_dict())
            if job.state in (JobState.FAILED, JobState.TIMEOUT, JobState.CANCELLED):
                failure = self._fail(
                    name,
                    session,
                    _error_code(job.error_code),
                    job.message or "execution did not succeed",
                )
                failure.result = snapshot
                failure.execution_id = job.execution_id
                failure.duration_ms = (time.time() - started) * 1000
                return failure
            return RemoteCallResult(
                ok=True,
                tool=name,
                session_id=session.session_id,
                workspace=workspace,
                result=snapshot,
                execution_id=job.execution_id,
                duration_ms=(time.time() - started) * 1000,
            )

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

    # ── argument preparation ────────────────────────────────────────
    async def _prepare(
        self,
        session: RemoteSession,
        policy: WorkspacePolicy,
        binding: ToolBinding,
        args: dict[str, Any],
    ) -> tuple[str, dict[str, Any], Path | None]:
        workspace = policy.root
        ensure_readable(str(workspace))
        name = binding.name
        if binding.veya_tool is None:
            raise RemoteToolAdapterError(RemoteErrorCode.TOOL_DENIED, f"{name} is adapter-owned")

        if name.startswith("veya."):
            # Supervision surface: orchestration metadata, bound to the session
            # workspace; execution still goes through the canonical runtime.
            return binding.veya_tool, {**args, "project_root": str(workspace)}, None

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
            return binding.veya_tool, self._hicode_args(args), None

        # Mutations resolve the owning git repo from the target path, so a client
        # can pass any absolute path under an authorized root without knowing
        # about (or naming) the isolation worktree.
        if name in {"file.write", "file.patch"}:
            target = self._resolve_in(
                policy, str(workspace), args["path"], must_exist=None, for_write=True
            )
            repo = self._git_repo_root(target)
            if repo is not None:
                worktree = Path(await self._ensure_worktree(session, str(repo)))
                mapped = worktree / target.relative_to(repo)
            else:
                # Non-git workspace: keep the legacy bound-workspace worktree.
                worktree = Path(await self._ensure_worktree(session, str(workspace)))
                mapped = self._resolve_in(
                    policy, str(worktree), args["path"], must_exist=None, for_write=True
                )
            if name == "file.write":
                kwargs = {
                    "filepath": str(mapped),
                    "content": str(args["content"]),
                    "overwrite": bool(args.get("overwrite", True)),
                }
                return binding.veya_tool, kwargs, worktree
            kwargs = {
                "filepath": str(mapped),
                "start_tag": str(args["start_tag"]),
                "new_text": str(args["new_text"]),
            }
            if args.get("end_tag"):
                kwargs["end_tag"] = str(args["end_tag"])
            return binding.veya_tool, kwargs, None

        repo = self._repo_for_workspace(session, str(workspace))
        worktree = await self._ensure_worktree(session, repo)
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

    def _hicode_args(self, args: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"task": str(args["task"])}
        if args.get("max_steps"):
            kwargs["max_steps"] = int(args["max_steps"])
        if args.get("timeout_sec"):
            kwargs["timeout_sec"] = int(args["timeout_sec"])
        return kwargs

    # ── helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _git_repo_root(path: Path) -> Path | None:
        """Nearest ancestor containing a ``.git`` entry (repo root), else None."""

        for candidate in (path, *path.parents):
            if (candidate / ".git").exists():
                return candidate
        return None

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

        target = self._resolve_in(
            policy,
            base,
            path,
            must_exist=None if must_exist is True else must_exist,
            for_write=for_write,
        )
        repo = self._git_repo_root(target)
        if repo is not None:
            worktree = session.worktrees.get(str(repo))
            if worktree:
                target = Path(worktree) / target.relative_to(repo)
        if must_exist is True and not target.exists():
            raise RemoteToolAdapterError(RemoteErrorCode.NOT_FOUND, f"path not found: {path}")
        if must_exist is False and target.exists():
            raise RemoteToolAdapterError(
                RemoteErrorCode.POLICY_BLOCKED, f"path already exists: {path}"
            )
        return target

    def _repo_for_workspace(self, session: RemoteSession, workspace: str) -> str:
        """Resolve the git repo a worktree-based tool should operate on."""

        if (Path(workspace) / ".git").exists():
            return workspace
        if len(session.worktrees) == 1:
            # After a path-resolved write, follow that repo for shell/test/git.
            return next(iter(session.worktrees))
        # Non-git workspace with no resolved repo yet: keep the legacy path and
        # let worktree creation surface the real error.
        return workspace

    def _base_dir(self, session: RemoteSession, workspace: str) -> str:
        return session.worktrees.get(workspace, workspace)

    def _task_id(self, session: RemoteSession, workspace: str) -> str:
        # Deterministic per token+workspace so a re-initialized session (the
        # tunnel re-initializes per request) or a service restart reuses the
        # same isolated worktree instead of orphaning a pending mutation.
        seed = f"{session.token_id}:{workspace}".encode()
        return "remote-" + hashlib.sha1(seed).hexdigest()[:20]

    def _task_outputs(self, session: RemoteSession, policy: WorkspacePolicy) -> Path:
        return (
            policy.root / _TASK_OUTPUTS / self._task_id(session, str(policy.root)) / "outputs"
        ).resolve()

    async def _ensure_worktree(self, session: RemoteSession, workspace: str) -> str:
        existing = session.worktrees.get(workspace)
        if existing and Path(existing).exists():
            return existing
        task_id = self._task_id(session, workspace)
        expected = Path(workspace) / ".veya" / "worktrees" / f"task-{task_id}"
        if expected.is_dir():
            session.worktrees[workspace] = str(expected)
            return str(expected)
        output = await self._invoke(
            "coding_worktree_create",
            {
                "workspace_path": workspace,
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
        if not path:
            raise RemoteToolAdapterError(
                RemoteErrorCode.EXECUTION_FAILED, "worktree create returned no path"
            )
        session.worktrees[workspace] = str(path)
        return str(path)

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
        # wait=False -> return the execution_id immediately; wait=True -> block to
        # completion; otherwise block for a bounded default so most clients get a
        # final result without polling.
        if args.get("wait") is False:
            return 0.0
        if "wait_timeout_s" in args:
            return max(0.0, float(args["wait_timeout_s"]))
        if args.get("wait") is True:
            return None
        return 120.0

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

    # ── process.* ───────────────────────────────────────────────────
    def _process_status(
        self, session: RemoteSession, name: str, args: dict[str, Any], started: float
    ) -> RemoteCallResult:
        execution_id = str(args.get("execution_id", ""))
        try:
            job = self.jobs.status(execution_id, session.session_id)
        except RemoteToolAdapterError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        return RemoteCallResult(
            ok=True,
            tool=name,
            session_id=session.session_id,
            workspace=session.active_workspace,
            result=self._redact(job.to_dict()),
            execution_id=job.execution_id,
            duration_ms=(time.time() - started) * 1000,
        )

    async def _process_cancel(
        self, session: RemoteSession, name: str, args: dict[str, Any], started: float
    ) -> RemoteCallResult:
        execution_id = str(args.get("execution_id", ""))
        try:
            job = await self.jobs.cancel(execution_id, session.session_id)
        except RemoteToolAdapterError as exc:
            return self._fail(name, session, RemoteErrorCode(exc.code), exc.message)
        return RemoteCallResult(
            ok=True,
            tool=name,
            session_id=session.session_id,
            workspace=session.active_workspace,
            result=self._redact(job.to_dict()),
            execution_id=job.execution_id,
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
