"""External Runtimes (XRT) core module for Veya.

Provides unified runtime specs, detection (PATH, version, login state command/file exit code),
non-interactive execution in isolated worktrees, streaming activity logging, cancellation, timeout,
concurrency limits, and owner-only management endpoints.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.events import append_canonical_event
from runtime.coding.worktree import WorktreeManager
from runtime.execution.cancellation import CancellationTree

# Required runtimes spec list as specified in brief:
# Devin, Claude Code, Codex CLI, Cursor CLI, Gemini CLI, GitHub Copilot CLI,
# Qwen Code, Droid, Cline, OpenCode, Grok, Hermes.
# Local detection check for: claude, codex, gemini, opencode, grok, hermes.

@dataclass
class XRTRuntimeSpec:
    name: str # internal key e.g. "claude_code", "codex", etc.
    display_name: str
    command: str
    version_flag: str = "--version"
    headless_args: list[str] = field(default_factory=list)
    login_check_cmd: list[str] | None = None # e.g. ["claude", "auth", "status"] or None
    login_check_files: list[str] | None = None # relative to Path.home()
    env_vars: dict[str, str] = field(default_factory=dict)


XRT_RUNTIMES_CATALOG: dict[str, XRTRuntimeSpec] = {
    "devin": XRTRuntimeSpec(
        name="devin",
        display_name="Devin",
        command="devin",
        headless_args=["run", "--prompt"],
        login_check_files=[".devin/credentials.json"],
    ),
    "claude_code": XRTRuntimeSpec(
        name="claude_code",
        display_name="Claude Code",
        command="claude",
        headless_args=["-p", "--print", "--permission-mode", "acceptEdits"],
        login_check_files=[".claude.json", ".config/claude/config.json"],
    ),
    "codex": XRTRuntimeSpec(
        name="codex",
        display_name="Codex CLI",
        command="codex",
        headless_args=["exec", "--skip-git-repo-check"],
        login_check_files=[".codex/auth.json", ".config/codex/credentials.json"],
    ),
    "cursor_agent": XRTRuntimeSpec(
        name="cursor_agent",
        display_name="Cursor CLI",
        command="cursor-agent",
        headless_args=["-p"],
        login_check_files=[".cursor/credentials.json"],
    ),
    "gemini": XRTRuntimeSpec(
        name="gemini",
        display_name="Gemini CLI",
        command="gemini",
        headless_args=["-p"],
        login_check_files=[".gemini/credentials.json"],
    ),
    "copilot": XRTRuntimeSpec(
        name="copilot",
        display_name="GitHub Copilot CLI",
        command="copilot",
        headless_args=["auth", "status"], # or prompt
        login_check_files=[".github/copilot/hosts.json", ".config/github/hosts.json"],
    ),
    "qwen": XRTRuntimeSpec(
        name="qwen",
        display_name="Qwen Code",
        command="qwen",
        headless_args=["-p"],
        login_check_files=[".qwen/credentials.json"],
    ),
    "droid": XRTRuntimeSpec(
        name="droid",
        display_name="Droid",
        command="droid",
        headless_args=["run"],
        login_check_files=[".droid/config.json"],
    ),
    "cline": XRTRuntimeSpec(
        name="cline",
        display_name="Cline",
        command="cline",
        headless_args=["run"],
        login_check_files=[".cline/config.json"],
    ),
    "opencode": XRTRuntimeSpec(
        name="opencode",
        display_name="OpenCode",
        command="opencode",
        headless_args=["run"],
        login_check_files=[".config/opencode/auth.json", ".opencode/auth.json"],
    ),
    "grok": XRTRuntimeSpec(
        name="grok",
        display_name="Grok",
        command="grok",
        headless_args=["-p"],
        login_check_files=[".grok/sessions", ".config/grok/auth.json"],
    ),
    "hermes": XRTRuntimeSpec(
        name="hermes",
        display_name="Hermes",
        command="hermes",
        headless_args=["run"],
        login_check_files=[".hermes/config.json"],
    ),
}


def is_xrt_enabled(runtime_name: str) -> bool:
    """Check if XRT system and specific runtime are enabled via env vars.
    VEYA_XRT_RUNTIMES=1 (default OFF / False)
    VEYA_XRT_ENABLED=claude_code,opencode (comma separated list)
    VEYA_XRT_RUNTIME_<RUNTIME_UPPER>=1
    """
    global_on = os.getenv("VEYA_XRT_RUNTIMES", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not global_on:
        return False
    
    # Check comma-separated VEYA_XRT_ENABLED list if set
    enabled_csv = os.getenv("VEYA_XRT_ENABLED", "").strip()
    if enabled_csv:
        allowed = {item.strip().lower() for item in enabled_csv.split(",") if item.strip()}
        if runtime_name.lower() not in allowed:
            return False

    # Check individual runtime toggle e.g. VEYA_XRT_RUNTIME_CLAUDE_CODE=1
    env_key = f"VEYA_XRT_RUNTIME_{runtime_name.upper()}"
    val = os.getenv(env_key, "1").strip().lower()
    if val in {"0", "false", "no", "off"}:
        return False

    return True


def probe_runtime(spec: XRTRuntimeSpec) -> dict[str, Any]:
    """Probe installed status, version, and login state (exit code or file existence).
    State = not_installed | installed_not_logged_in | ready | disabled
    """
    if not is_xrt_enabled(spec.name):
        return {
            "name": spec.name,
            "display_name": spec.display_name,
            "state": "disabled",
            "installed": False,
            "version": None,
            "logged_in": False,
            "path": None,
        }

    detected_path = shutil.which(spec.command)
    if not detected_path:
        return {
            "name": spec.name,
            "display_name": spec.display_name,
            "state": "not_installed",
            "installed": False,
            "version": None,
            "logged_in": False,
            "path": None,
        }

    # Version check
    version = None
    try:
        res = subprocess.run(
            [detected_path, spec.version_flag],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            version = res.stdout.strip().splitlines()[0]
    except Exception:
        pass

    # Login check (no secret reading, only exit code or file existence)
    logged_in = False
    home = Path.home()
    if spec.login_check_files:
        for f_rel in spec.login_check_files:
            p = home / f_rel
            if p.exists():
                logged_in = True
                break
    
    if not logged_in and spec.login_check_cmd:
        try:
            lc_res = subprocess.run(
                [detected_path] + spec.login_check_cmd[1:],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if lc_res.returncode == 0:
                logged_in = True
        except Exception:
            pass

    # Fallback heuristic for mock / test runners if no config files exist:
    # If binary exists in test environment or PATH, treat as logged in if configured or test stub
    if not logged_in and os.getenv("VEYA_XRT_FORCE_LOGGED_IN", "0") == "1":
        logged_in = True

    # If no credential files defined for a spec, assume installed & logged in if binary exists
    if not spec.login_check_files and not spec.login_check_cmd:
        logged_in = True

    state = "ready" if logged_in else "installed_not_logged_in"

    return {
        "name": spec.name,
        "display_name": spec.display_name,
        "state": state,
        "installed": True,
        "version": version,
        "logged_in": logged_in,
        "path": detected_path,
    }


# Active run tracking and global/per-runtime concurrency limiters
_XRT_ACTIVE_LOCK = asyncio.Lock()
_XRT_ACTIVE_COUNTS: dict[str, int] = {}
_XRT_TOTAL_ACTIVE = 0

MAX_GLOBAL_CONCURRENCY = int(os.getenv("VEYA_XRT_MAX_GLOBAL_CONCURRENCY", "2"))
MAX_RUNTIME_CONCURRENCY = int(os.getenv("VEYA_XRT_MAX_RUNTIME_CONCURRENCY", "1"))


@dataclass
class XRTRunExecution:
    execution_id: str
    runtime_name: str
    task_text: str
    worktree_path: str
    started_at: float
    status: str = "RUNNING"
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    process: asyncio.subprocess.Process | None = None
    pgid: int | None = None


_XRT_RUNS: dict[str, XRTRunExecution] = {}


async def run_xrt_task(
    execution_id: str,
    runtime_name: str,
    task_text: str,
    repo_root: str,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Execute task in isolated worktree using non-interactive CLI arguments,
    streaming output to canonical events, supporting timeout and cancellation.
    """
    global _XRT_TOTAL_ACTIVE
    spec = XRTRuntime_CATALOG.get(runtime_name) if 'XRTRuntime_CATALOG' in globals() else XRT_RUNTIMES_CATALOG.get(runtime_name)
    if not spec:
        raise HTTPException(status_code=404, detail=f"Unknown XRT runtime: {runtime_name}")

    if not is_xrt_enabled(runtime_name):
        raise HTTPException(status_code=404, detail=f"XRT runtime {runtime_name} is disabled or VEYA_XRT_RUNTIMES is OFF")

    status_info = probe_runtime(spec)
    if not status_info["installed"]:
        raise HTTPException(status_code=400, detail=f"Runtime {runtime_name} is not installed")

    # Concurrency control
    async with _XRT_ACTIVE_LOCK:
        if _XRT_TOTAL_ACTIVE >= MAX_GLOBAL_CONCURRENCY:
            raise HTTPException(status_code=429, detail="Global XRT concurrency limit exceeded")
        current_rt = _XRT_ACTIVE_COUNTS.get(runtime_name, 0)
        if current_rt >= MAX_RUNTIME_CONCURRENCY:
            raise HTTPException(status_code=429, detail=f"Runtime concurrency limit exceeded for {runtime_name}")
        _XRT_TOTAL_ACTIVE += 1
        _XRT_ACTIVE_COUNTS[runtime_name] = current_rt + 1

    # Create isolated worktree using WorktreeManager
    manager = WorktreeManager(repo_root)
    wt_record = manager.create(task_id=execution_id, objective=task_text)
    worktree_path = wt_record.path

    started = time.monotonic()
    bin_path = status_info["path"]
    
    # Build argument list without shell=True
    # Command template: bin_path + headless_args + [task_text]
    argv = [bin_path] + spec.headless_args + [task_text]

    run_exec = XRTRunExecution(
        execution_id=execution_id,
        runtime_name=runtime_name,
        task_text=task_text,
        worktree_path=worktree_path,
        started_at=started,
    )
    _XRT_RUNS[execution_id] = run_exec

    append_canonical_event(
        "xrt_execution_started",
        {
            "execution_id": execution_id,
            "runtime_name": runtime_name,
            "worktree_path": worktree_path,
            "argv": argv,
        },
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    # Cancellation handler registration via CancellationTree
    _cancellation_tree = CancellationTree()
    _cancellation_tree.register(execution_id)
    async def cancel_handler() -> None:
        _cancellation_tree.cancel(execution_id)
        await cancel_xrt_execution(execution_id)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=worktree_path,
            env={**os.environ, **spec.env_vars},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        run_exec.process = proc
        with contextlib.suppress(OSError):
            run_exec.pgid = os.getpgid(proc.pid)

        async def read_stream(stream: Any, sink: list[str], stream_name: str) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace")
                sink.append(text)
                append_canonical_event(
                    "xrt_output",
                    {
                        "execution_id": execution_id,
                        "stream": stream_name,
                        "line": text,
                    },
                )

        readers = [
            asyncio.create_task(read_stream(proc.stdout, stdout_lines, "stdout")),
            asyncio.create_task(read_stream(proc.stderr, stderr_lines, "stderr")),
        ]

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            await _kill_run(run_exec)
            run_exec.status = "TIMEOUT"
            run_exec.exit_code = 124
        except Exception:
            await _kill_run(run_exec)
            run_exec.status = "FAILED"
            run_exec.exit_code = 1

        with contextlib.suppress(Exception):
            await asyncio.gather(*readers, return_exceptions=True)

        if run_exec.status == "RUNNING":
            run_exec.exit_code = proc.returncode if proc.returncode is not None else 0
            run_exec.status = "COMPLETED" if run_exec.exit_code == 0 else "FAILED"

        run_exec.stdout = "".join(stdout_lines)
        run_exec.stderr = "".join(stderr_lines)

    except Exception as exc:
        run_exec.status = "FAILED"
        run_exec.exit_code = 1
        run_exec.stderr = str(exc)
    finally:
        async with _XRT_ACTIVE_LOCK:
            _XRT_TOTAL_ACTIVE = max(0, _XRT_TOTAL_ACTIVE - 1)
            c = _XRT_ACTIVE_COUNTS.get(runtime_name, 1)
            _XRT_ACTIVE_COUNTS[runtime_name] = max(0, c - 1)

    # Git diff relative to baseline
    git_diff = ""
    try:
        diff_res = subprocess.run(
            ["git", "diff"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if diff_res.returncode == 0:
            git_diff = diff_res.stdout
    except Exception:
        pass

    duration_ms = (time.monotonic() - started) * 1000

    result_payload = {
        "execution_id": execution_id,
        "runtime_name": runtime_name,
        "status": run_exec.status,
        "exit_code": run_exec.exit_code,
        "stdout": run_exec.stdout[-4000:],
        "stderr": run_exec.stderr[-4000:],
        "git_diff": git_diff[:8000],
        "worktree_path": worktree_path,
        "duration_ms": duration_ms,
    }

    append_canonical_event("xrt_execution_finished", result_payload)
    return result_payload


async def _kill_run(run_exec: XRTRunExecution) -> None:
    from runtime.execution.cancellation import terminate_process_group
    if run_exec.pgid:
        with contextlib.suppress(Exception):
            await terminate_process_group(run_exec.pgid)
    if run_exec.process and run_exec.process.returncode is None:
        with contextlib.suppress(Exception):
            run_exec.process.terminate()
            await asyncio.sleep(0.5)
            if run_exec.process.returncode is None:
                run_exec.process.kill()


async def cancel_xrt_execution(execution_id: str) -> bool:
    run_exec = _XRT_RUNS.get(execution_id)
    if not run_exec or run_exec.status != "RUNNING":
        return True
    run_exec.status = "CANCELLED"
    await _kill_run(run_exec)
    append_canonical_event("xrt_execution_cancelled", {"execution_id": execution_id})
    return True


# Owner verification helper
def require_xrt_owner(user: dict[str, Any] = Depends(auth_mod.require_user)) -> dict[str, Any]:
    """Owner-only check matching server/routes/tailnet.py require_tailnet_owner."""
    user_id = str(user.get("user_id") or "")
    configured_owner = (
        os.getenv("VEYA_TAILNET_OWNER_USER_ID") or os.getenv("VEYA_OWNER_USER_ID") or ""
    ).strip()
    roles = (
        {str(role).lower() for role in user.get("roles", [])}
        if isinstance(user.get("roles", []), (list, tuple, set))
        else set()
    )
    role = str(user.get("role") or "").lower()
    allowed = bool(configured_owner and user_id == configured_owner) or bool(
        user.get("is_owner") is True
        or user.get("is_admin") is True
        or role in {"owner", "admin"}
        or roles.intersection({"owner", "admin"})
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="XRT management requires owner role",
        )
    return user


router = APIRouter(prefix="/api/v1/xrt", tags=["external-runtimes"])


class XRTDispatchRequest(BaseModel):
    runtime: str
    task: str
    timeout_s: float = 120.0


@router.get("/runtimes")
async def list_xrt_runtimes(user: dict[str, Any] = Depends(require_xrt_owner)) -> dict[str, Any]:
    """List all XRT runtimes and their detection state."""
    results = []
    for name, spec in XRT_RUNTIMES_CATALOG.items():
        results.append(probe_runtime(spec))
    return {"runtimes": results, "enabled": os.getenv("VEYA_XRT_RUNTIMES", "0")}


@router.post("/dispatch")
async def dispatch_xrt_task(
    req: XRTDispatchRequest, user: dict[str, Any] = Depends(require_xrt_owner)
) -> dict[str, Any]:
    """Dispatch a task to an external runtime in an isolated worktree."""
    global_on = os.getenv("VEYA_XRT_RUNTIMES", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not global_on:
        raise HTTPException(status_code=404, detail="XRT system is disabled (VEYA_XRT_RUNTIMES != 1)")

    if req.runtime not in XRT_RUNTIMES_CATALOG:
        raise HTTPException(status_code=404, detail=f"Unknown runtime: {req.runtime}")

    if not is_xrt_enabled(req.runtime):
        raise HTTPException(status_code=404, detail=f"Runtime {req.runtime} is disabled")

    import uuid
    execution_id = f"xrt-{uuid.uuid4().hex[:10]}"
    repo_root = os.getenv("VEYA_PROJECT_ROOT", ".")

    # Run asynchronously or await depending on test / sync expectations
    result = await run_xrt_task(
        execution_id=execution_id,
        runtime_name=req.runtime,
        task_text=req.task,
        repo_root=repo_root,
        timeout_s=req.timeout_s,
    )
    return result


@router.post("/cancel/{execution_id}")
async def cancel_xrt_task(
    execution_id: str, user: dict[str, Any] = Depends(require_xrt_owner)
) -> dict[str, Any]:
    """Cancel a running XRT execution."""
    success = await cancel_xrt_execution(execution_id)
    return {"execution_id": execution_id, "cancelled": success}


@router.get("/status/{execution_id}")
async def get_xrt_status(
    execution_id: str, user: dict[str, Any] = Depends(require_xrt_owner)
) -> dict[str, Any]:
    """Get status and results of an XRT execution."""
    run_exec = _XRT_RUNS.get(execution_id)
    if not run_exec:
        raise HTTPException(status_code=404, detail="Execution not found")
    return {
        "execution_id": run_exec.execution_id,
        "runtime_name": run_exec.runtime_name,
        "status": run_exec.status,
        "exit_code": run_exec.exit_code,
        "stdout": run_exec.stdout[-4000:],
        "stderr": run_exec.stderr[-4000:],
        "worktree_path": run_exec.worktree_path,
    }


__all__ = ["router", "XRT_RUNTIMES_CATALOG", "probe_runtime", "run_xrt_task", "is_xrt_enabled"]
