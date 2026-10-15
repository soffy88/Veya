"""Concrete ExecutorAdapter implementations (P4).

Two real adapters over the same interface:

* :class:`Local2ExecutorAdapter` — the Veya native execution substrate. Runs
  one argv command through the canonical direct executor (no LLM, no agent
  loop) and streams stdout/stderr.
* :class:`CLIWorkerExecutorAdapter` — an external CLI provider worker (pi,
  codex, opencode, claude_code, grok, antigravity, dsh). Builds the real
  worker argv and runs it in the worktree.

Neither adapter introduces a second durable authority: lifecycle facts go to
GoalRun / ExecutorBinding via the existing helpers.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from typing import Any

from veya.remote.executor_adapter import (
    AdapterResult,
    AdapterStartRequest,
    ExecutorAdapter,
    ExecutorCapabilities,
    ExecutorHealth,
    ExecutorIdentity,
    ExecutorRuntimeState,
    ProgressReporter,
    register_executor_adapter,
)

# ── Local2 native substrate adapter ─────────────────────────────────


@dataclass
class _Local2Run:
    """In-memory handle for one Local2 execution (observability only)."""

    execution_id: str
    goal_run_id: str
    started_at: float
    argv: list[str]
    cwd: str
    status: str = "RUNNING"  # RUNNING | COMPLETED | FAILED | CANCELLED
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    process: Any = None
    pgid: int | None = None
    activities: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.activities is None:
            self.activities = []


class Local2ExecutorAdapter(ExecutorAdapter):
    """Veya native execution substrate (direct argv, no LLM)."""

    def __init__(self, executor_id: str, project_root: str) -> None:
        super().__init__(executor_id, project_root)
        self._runs: dict[str, _Local2Run] = {}

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id=self.executor_id,
            executor_kind="native_substrate",
            provider="local2",
            model=None,
            version="vnext",
        )

    @property
    def capabilities(self) -> ExecutorCapabilities:
        from veya.remote.executor_adapter import ExecutorCapability

        return ExecutorCapabilities(
            supported_operations=frozenset(
                {"shell.exec", "test.run", "build.run", "file.read", "file.write"}
            ),
            capabilities=frozenset(
                {
                    ExecutorCapability.FILESYSTEM,
                    ExecutorCapability.SHELL,
                    ExecutorCapability.TEST,
                    ExecutorCapability.BUILD,
                    ExecutorCapability.GIT,
                    ExecutorCapability.WORKTREE,
                    ExecutorCapability.NETWORK,
                    ExecutorCapability.STREAMING,
                    ExecutorCapability.CANCEL,
                    ExecutorCapability.LONG_RUNNING,
                    ExecutorCapability.SESSION,
                    ExecutorCapability.PARALLEL,
                }
            ),
            write_qualified=True,
        )

    @property
    def health(self) -> ExecutorRuntimeState:
        reachable = os.path.isdir(self.project_root)
        return ExecutorRuntimeState(
            availability="AVAILABLE" if reachable else "UNAVAILABLE",
            health=ExecutorHealth.HEALTHY if reachable else ExecutorHealth.UNAVAILABLE,
            reachable=reachable,
            authenticated=True,
            credential_valid=True,
            credential_evidence="local_substrate",
        )

    async def probe_health(self) -> ExecutorHealth:
        return self.health.health

    async def start(
        self, request: AdapterStartRequest, reporter: ProgressReporter
    ) -> AdapterResult:
        from veya.remote.direct_exec import DirectDenied, run_direct_command

        started = time.monotonic()
        run = _Local2Run(
            execution_id=request.execution_id,
            goal_run_id=request.goal_run_id,
            started_at=started,
            argv=[request.task_text],
            cwd=request.worktree_path or request.repo_root,
        )
        self._runs[request.execution_id] = run
        reporter.phase("STARTING", message="local2 substrate starting", event="STARTING")

        def on_stdout(line: str) -> None:
            run.activities.append({"type": "OUTPUT", "stream": "stdout", "line": line})
            reporter.output("stdout", line)

        def on_stderr(line: str) -> None:
            run.activities.append({"type": "OUTPUT", "stream": "stderr", "line": line})
            reporter.output("stderr", line)

        try:
            result = await run_direct_command(
                request.worktree_path or request.repo_root,
                request.task_text,
                cwd=request.worktree_path or request.repo_root,
                timeout_s=request.timeout_s,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
        except DirectDenied as exc:
            run.status = "FAILED"
            reporter.failure(failure_class="PERMISSION_DENIED", source="local2", detail=str(exc))
            return AdapterResult(
                exit_code=1,
                stdout="",
                stderr=str(exc),
                duration_ms=(time.monotonic() - started) * 1000,
                failure_class="PERMISSION_DENIED",
                detail=str(exc),
            )
        except Exception as exc:  # pragma: no cover - defensive
            run.status = "FAILED"
            reporter.failure(
                failure_class="PROCESS_START_FAILURE", source="local2", detail=str(exc)
            )
            return AdapterResult(
                exit_code=1,
                stdout="",
                stderr=str(exc),
                duration_ms=(time.monotonic() - started) * 1000,
                failure_class="PROCESS_START_FAILURE",
                detail=str(exc),
            )

        run.exit_code = result.exit_code
        run.stdout = result.stdout_tail
        run.stderr = result.stderr_tail
        run.status = "COMPLETED" if result.status == "passed" else "FAILED"
        reporter.phase("FINALIZING", message="local2 substrate finished", event="FINALIZING")
        return AdapterResult(
            exit_code=result.exit_code or 0,
            stdout=result.stdout_tail,
            stderr=result.stderr_tail,
            duration_ms=(time.monotonic() - started) * 1000,
            failure_class=None if result.status == "passed" else "PROCESS_RUNTIME_FAILURE",
            detail=None if result.status == "passed" else result.stderr_tail[-500:],
        )

    async def resume(
        self, execution_id: str, provider_run_id: str, reporter: ProgressReporter
    ) -> AdapterResult:
        raise NotImplementedError("local2 direct execution is not resumable in place")

    async def wait(
        self, execution_id: str, provider_run_id: str, timeout: float | None = None
    ) -> AdapterResult:
        run = self._runs.get(execution_id)
        if run is None:
            return AdapterResult(
                exit_code=1,
                stdout="",
                stderr="unknown execution",
                duration_ms=0.0,
                failure_class="PROCESS_RUNTIME_FAILURE",
            )
        deadline = time.monotonic() + (timeout or 0)
        while run.status == "RUNNING":
            if timeout and time.monotonic() > deadline:
                raise TimeoutError(f"wait timeout for {execution_id}")
            await asyncio.sleep(0.05)
        return AdapterResult(
            exit_code=run.exit_code or 0,
            stdout=run.stdout,
            stderr=run.stderr,
            duration_ms=(time.monotonic() - run.started_at) * 1000,
            failure_class=None if run.status == "COMPLETED" else "PROCESS_RUNTIME_FAILURE",
        )

    async def stream(
        self, execution_id: str, provider_run_id: str, cursor: int | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        run = self._runs.get(execution_id)
        if run is None:
            return [], cursor or 0
        start = cursor or 0
        events = run.activities[start:]
        return events, start + len(events)

    async def status(self, execution_id: str, provider_run_id: str) -> dict[str, Any]:
        run = self._runs.get(execution_id)
        if run is None:
            return {"execution_id": execution_id, "status": "UNKNOWN"}
        return {
            "execution_id": execution_id,
            "status": run.status,
            "exit_code": run.exit_code,
            "cwd": run.cwd,
            "duration_ms": (time.monotonic() - run.started_at) * 1000,
        }

    async def cancel(self, execution_id: str, provider_run_id: str) -> bool:
        run = self._runs.get(execution_id)
        if run is None or run.status != "RUNNING":
            return True
        run.status = "CANCELLED"
        return True


# ── CLI worker adapter ──────────────────────────────────────────────


class CLIWorkerExecutorAdapter(ExecutorAdapter):
    """External CLI provider worker (pi / codex / opencode / claude_code / ...)."""

    def __init__(self, executor_id: str, project_root: str) -> None:
        super().__init__(executor_id, project_root)
        self._runs: dict[str, dict[str, Any]] = {}

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id=self.executor_id,
            executor_kind="cli_worker",
            provider=self.executor_id,
            model=None,
        )

    @property
    def capabilities(self) -> ExecutorCapabilities:
        from veya.remote.worker_runtime import capabilities_for

        try:
            runtime_caps = capabilities_for(self.executor_id)
        except Exception:
            runtime_caps = None
        from veya.remote.executor_adapter import ExecutorCapability

        caps = {
            ExecutorCapability.FILESYSTEM,
            ExecutorCapability.SHELL,
            ExecutorCapability.STREAMING,
            ExecutorCapability.CANCEL,
            ExecutorCapability.LONG_RUNNING,
        }
        if runtime_caps is not None and getattr(runtime_caps, "supports_write_task", False):
            caps.add(ExecutorCapability.WORKTREE)
        return ExecutorCapabilities(
            supported_operations=frozenset({"worker.dispatch"}),
            capabilities=frozenset(caps),
            write_qualified=bool(
                runtime_caps is not None and getattr(runtime_caps, "supports_write_task", False)
            ),
        )

    @property
    def health(self) -> ExecutorRuntimeState:
        try:
            from veya.remote.executor_registry import get_executor_registry

            identity = get_executor_registry().identity(self.executor_id)
            return ExecutorRuntimeState(
                availability=str(identity.availability),
                health=(
                    ExecutorHealth.HEALTHY if identity.selectable else ExecutorHealth.UNAVAILABLE
                ),
                reachable=bool(identity.reachable),
                authenticated=bool(identity.authenticated),
                credential_valid=identity.credential_valid,
                credential_evidence=identity.credential_evidence,
            )
        except Exception:
            return ExecutorRuntimeState(availability="UNKNOWN", health=ExecutorHealth.UNKNOWN)

    async def probe_health(self) -> ExecutorHealth:
        return self.health.health

    async def start(
        self, request: AdapterStartRequest, reporter: ProgressReporter
    ) -> AdapterResult:
        from veya.remote.tool_adapter import _worker_command

        started = time.monotonic()
        cwd = request.worktree_path or request.repo_root
        coding_mode = str(request.task_contract.get("task_kind", "")).upper() in {
            "WRITE",
            "TEST",
            "BUILD",
        }
        try:
            runtime_id = request.task_contract.get("xrt_runtime_id")
            if runtime_id:
                from runtime.coding.cli_agents import build_xrt_command

                argv, env = build_xrt_command(str(runtime_id), request.task_text)
            else:
                argv, env = _worker_command(
                    self.executor_id,
                    request.task_text,
                    worktree_path=request.worktree_path,
                    coding_mode=coding_mode,
                    agent=request.env.get("VEYA_OPENCODE_AGENT") if request.env else None,
                )
        except Exception as exc:
            reporter.failure(
                failure_class="PROCESS_START_FAILURE", source=self.executor_id, detail=str(exc)
            )
            return AdapterResult(
                exit_code=1,
                stdout="",
                stderr=str(exc),
                duration_ms=0.0,
                failure_class="PROCESS_START_FAILURE",
                detail=str(exc),
            )

        record: dict[str, Any] = {
            "status": "RUNNING",
            "argv": argv,
            "cwd": cwd,
            "started_at": started,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "process": None,
        }
        self._runs[request.execution_id] = record
        reporter.phase("STARTING", message=f"{self.executor_id} starting", event="STARTING")

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            record["status"] = "FAILED"
            reporter.failure(
                failure_class="PROCESS_START_FAILURE", source=self.executor_id, detail=str(exc)
            )
            return AdapterResult(
                exit_code=1,
                stdout="",
                stderr=str(exc),
                duration_ms=(time.monotonic() - started) * 1000,
                failure_class="PROCESS_START_FAILURE",
                detail=str(exc),
            )

        record["process"] = proc
        with contextlib.suppress(OSError):
            record["pgid"] = os.getpgid(proc.pid)

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        async def pump(stream: Any, sink: list[str], name: str) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace")
                sink.append(text)
                reporter.output(name, text)

        readers = [
            asyncio.ensure_future(pump(proc.stdout, stdout_lines, "stdout")),
            asyncio.ensure_future(pump(proc.stderr, stderr_lines, "stderr")),
        ]
        try:
            await asyncio.wait_for(proc.wait(), timeout=request.timeout_s)
        except TimeoutError:
            await self._terminate(record)
            record["status"] = "FAILED"
            reporter.failure(
                failure_class="PROCESS_TIMEOUT", source=self.executor_id, detail="hard timeout"
            )
            return AdapterResult(
                exit_code=124,
                stdout="".join(stdout_lines)[-4000:],
                stderr="".join(stderr_lines)[-4000:],
                duration_ms=(time.monotonic() - started) * 1000,
                failure_class="PROCESS_TIMEOUT",
                detail="hard timeout",
            )
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(*readers, return_exceptions=True), timeout=3.0
                )

        record["exit_code"] = proc.returncode
        record["stdout"] = "".join(stdout_lines)
        record["stderr"] = "".join(stderr_lines)
        record["status"] = "COMPLETED" if proc.returncode == 0 else "FAILED"
        reporter.phase("FINALIZING", message=f"{self.executor_id} finished", event="FINALIZING")
        return AdapterResult(
            exit_code=proc.returncode or 0,
            stdout=record["stdout"][-4000:],
            stderr=record["stderr"][-4000:],
            duration_ms=(time.monotonic() - started) * 1000,
            failure_class=None if proc.returncode == 0 else "PROCESS_RUNTIME_FAILURE",
            detail=None if proc.returncode == 0 else record["stderr"][-500:],
        )

    async def _terminate(self, record: dict[str, Any]) -> None:
        from veya.remote.direct_exec import terminate_process_group_id

        pgid = record.get("pgid")
        proc = record.get("process")
        if pgid:
            await terminate_process_group_id(pgid)
        if proc is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2.0)

    async def resume(
        self, execution_id: str, provider_run_id: str, reporter: ProgressReporter
    ) -> AdapterResult:
        raise NotImplementedError("cli worker resume is not supported")

    async def wait(
        self, execution_id: str, provider_run_id: str, timeout: float | None = None
    ) -> AdapterResult:
        record = self._runs.get(execution_id)
        if record is None:
            return AdapterResult(
                exit_code=1,
                stdout="",
                stderr="unknown execution",
                duration_ms=0.0,
                failure_class="PROCESS_RUNTIME_FAILURE",
            )
        deadline = time.monotonic() + (timeout or 0)
        while record["status"] == "RUNNING":
            if timeout and time.monotonic() > deadline:
                raise TimeoutError(f"wait timeout for {execution_id}")
            await asyncio.sleep(0.05)
        return AdapterResult(
            exit_code=record.get("exit_code") or 0,
            stdout=record.get("stdout", "")[-4000:],
            stderr=record.get("stderr", "")[-4000:],
            duration_ms=(time.monotonic() - record["started_at"]) * 1000,
            failure_class=None if record["status"] == "COMPLETED" else "PROCESS_RUNTIME_FAILURE",
        )

    async def stream(
        self, execution_id: str, provider_run_id: str, cursor: int | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        # CLI workers stream through the durable job manager in production; this
        # adapter-level stream returns whatever has been buffered.
        record = self._runs.get(execution_id)
        if record is None:
            return [], cursor or 0
        events = [{"type": "OUTPUT", "stream": "stdout", "text": record.get("stdout", "")}]
        start = cursor or 0
        return events[start:], start + len(events)

    async def status(self, execution_id: str, provider_run_id: str) -> dict[str, Any]:
        record = self._runs.get(execution_id)
        if record is None:
            return {"execution_id": execution_id, "status": "UNKNOWN"}
        return {
            "execution_id": execution_id,
            "status": record["status"],
            "exit_code": record.get("exit_code"),
            "cwd": record.get("cwd"),
            "duration_ms": (time.monotonic() - record["started_at"]) * 1000,
        }

    async def cancel(self, execution_id: str, provider_run_id: str) -> bool:
        record = self._runs.get(execution_id)
        if record is None or record["status"] != "RUNNING":
            return True
        await self._terminate(record)
        record["status"] = "CANCELLED"
        return True


# ── Registration ────────────────────────────────────────────────────

_LOCAL2_ID = "local2"


def register_default_adapters() -> None:
    """Register built-in adapters and only opt-in XRT-specific adapters."""
    from runtime.coding.cli_agents import (
        XRT_RUNTIME_PROFILES,
        xrt_feature_enabled,
        xrt_runtime_enabled,
    )
    from veya.remote.executor_adapter import unregister_executor_adapter

    register_executor_adapter(_LOCAL2_ID, Local2ExecutorAdapter)
    for worker in (
        "pi",
        "codex",
        "opencode",
        "claude_code",
        "grok",
        "antigravity",
        "dsh",
    ):
        register_executor_adapter(worker, CLIWorkerExecutorAdapter)

    existing_workers = {"claude_code", "codex", "opencode", "grok"}
    xrt_only_ids = set(XRT_RUNTIME_PROFILES) - existing_workers
    for worker in sorted(xrt_only_ids):
        if xrt_feature_enabled() and xrt_runtime_enabled(worker):
            register_executor_adapter(worker, CLIWorkerExecutorAdapter)
        else:
            unregister_executor_adapter(worker)


register_default_adapters()
