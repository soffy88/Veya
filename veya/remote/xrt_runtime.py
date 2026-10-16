"""XRT orchestration on the canonical CLI executor and worktree primitives."""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from typing import Any

from runtime.coding.cli_agents import (
    XRT_RUNTIME_PROFILES,
    probe_xrt_runtimes,
    probe_xrt_runtime,
    xrt_feature_enabled,
    xrt_runtime_enabled,
)
from runtime.coding.worktree import WorktreeManager
from runtime.execution.cancellation import CancellationTree
from veya.remote.executor_adapter import AdapterStartRequest, ProgressReporter
from veya.remote.executor_adapters import CLIWorkerExecutorAdapter

_LOCK = threading.RLock()
_JOBS: dict[str, dict[str, Any]] = {}
_ADAPTERS: dict[tuple[str, str], CLIWorkerExecutorAdapter] = {}
_RUNNING: dict[str, int] = {}
_CANCEL = CancellationTree()


def _limits() -> tuple[int, int]:
    def positive(name: str, default: int) -> int:
        try:
            return max(1, min(16, int(os.getenv(name, str(default)))))
        except ValueError:
            return default
    return positive("VEYA_XRT_MAX_CONCURRENT", 2), positive("VEYA_XRT_MAX_PER_RUNTIME", 1)


def list_runtimes(repo_root: str) -> dict[str, Any]:
    return {"enabled": xrt_feature_enabled(), "runtimes": probe_xrt_runtimes(repo_root)}


def _adapter(runtime_id: str, repo_root: str) -> CLIWorkerExecutorAdapter:
    key = (runtime_id, str(repo_root))
    with _LOCK:
        if key not in _ADAPTERS:
            _ADAPTERS[key] = CLIWorkerExecutorAdapter(runtime_id, repo_root)
        return _ADAPTERS[key]


def _public(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if not key.startswith("_")}


def get_status(execution_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(execution_id)
        return _public(job) if job else None


async def dispatch(runtime_id: str, task: str, repo_root: str, timeout_s: float) -> dict[str, Any]:
    if not xrt_feature_enabled() or runtime_id not in XRT_RUNTIME_PROFILES or not xrt_runtime_enabled(runtime_id):
        raise LookupError("XRT runtime is disabled or unknown")
    if not task.strip():
        raise ValueError("task must not be empty")
    if not 1 <= timeout_s <= 1800:
        raise ValueError("timeout_s must be between 1 and 1800")
    status = probe_xrt_runtime(runtime_id, repo_root)
    if not status["installed"]:
        raise FileNotFoundError(f"runtime is not installed: {runtime_id}")
    if not status["logged_in"]:
        raise PermissionError(f"runtime is installed but not logged in: {runtime_id}")
    global_limit, runtime_limit = _limits()
    with _LOCK:
        total = sum(_RUNNING.values())
        if total >= global_limit or _RUNNING.get(runtime_id, 0) >= runtime_limit:
            raise RuntimeError("XRT concurrency limit reached")
        _RUNNING[runtime_id] = _RUNNING.get(runtime_id, 0) + 1
    execution_id = f"xrt-{uuid.uuid4().hex[:12]}"
    try:
        manager = WorktreeManager(repo_root)
        record = await asyncio.to_thread(manager.create, execution_id, task[:160])
        adapter = _adapter(runtime_id, repo_root)
        _CANCEL.register(execution_id)
        job = {
            "execution_id": execution_id, "runtime_name": runtime_id,
            "status": "RUNNING", "exit_code": None, "stdout": "", "stderr": "",
            "git_diff": "", "changed_files": [], "git_diff_stat": "",
            "worktree_path": record.path, "duration_ms": None,
            "_started": time.monotonic(), "_runtime_id": runtime_id,
            "_repo_root": str(repo_root), "_manager": manager,
        }
        with _LOCK:
            _JOBS[execution_id] = job
        task_runner = asyncio.create_task(_execute(job, adapter, task, timeout_s))
        job["_task"] = task_runner
        task_runner.add_done_callback(lambda _finished: job.pop("_task", None))
        return _public(job)
    except Exception:
        with _LOCK:
            _RUNNING[runtime_id] = max(0, _RUNNING.get(runtime_id, 1) - 1)
        raise


async def _execute(job: dict[str, Any], adapter: CLIWorkerExecutorAdapter, task: str, timeout_s: float) -> None:
    from server.events import append_canonical_event

    execution_id = job["execution_id"]
    runtime_id = job["_runtime_id"]
    reporter = ProgressReporter(execution_id, execution_id, runtime_id)
    request = AdapterStartRequest(
        goal_run_id=execution_id, goal_task_id=execution_id, execution_id=execution_id,
        task_text=task, worktree_path=job["worktree_path"], repo_root=job["_repo_root"],
        task_contract={"task_kind": "WRITE", "xrt_runtime_id": runtime_id},
        timeout_s=timeout_s,
    )
    try:
        append_canonical_event("xrt_execution_started", {"execution_id": execution_id, "runtime": runtime_id})
        if _CANCEL.is_cancelled(execution_id):
            with _LOCK:
                job.update({"status": "CANCELLED", "exit_code": 130, "duration_ms": (time.monotonic() - job["_started"]) * 1000})
            return
        result = await adapter.start(request, reporter)
        diff = await asyncio.to_thread(job["_manager"].diff, path=job["worktree_path"])
        from runtime.execution.artifacts import ArtifactStore

        artifact_store = ArtifactStore(job["_repo_root"], execution_id)
        diff_path = artifact_store.path("outputs/worktree.diff")
        diff_path.write_text(diff.get("patch", ""), encoding="utf-8")
        diff_ref = artifact_store.register(
            "outputs/worktree.diff", kind="text/x-diff", producer=runtime_id, status="draft"
        )
        artifact_store.write_json(
            "evidence/execution_result.json",
            {
                "execution_id": execution_id,
                "runtime_id": runtime_id,
                "exit_code": result.exit_code,
                "failure_class": result.failure_class,
                "duration_ms": result.duration_ms,
                "changed_files": diff.get("changed_files", []),
                "git_diff_stat": diff.get("stat", ""),
            },
        )
        result_ref = artifact_store.register(
            "evidence/execution_result.json", kind="application/json", producer=runtime_id, status="draft"
        )
        manifest_path = artifact_store.write_manifest()
        artifact_refs = [
            {"path": diff_ref.path, "sha256": diff_ref.sha256, "size_bytes": diff_ref.size_bytes},
            {"path": result_ref.path, "sha256": result_ref.sha256, "size_bytes": result_ref.size_bytes},
            {"manifest_path": str(manifest_path)},
        ]
        with _LOCK:
            job.update({
                "status": "CANCELLED" if _CANCEL.is_cancelled(execution_id) else (
                    "COMPLETED" if result.exit_code == 0 else (
                        "TIMEOUT" if result.exit_code == 124 or result.failure_class == "PROCESS_TIMEOUT" else "FAILED"
                    )
                ),
                "exit_code": result.exit_code, "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:], "git_diff": diff.get("patch", "")[:12000],
                "git_diff_stat": diff.get("stat", ""),
                "changed_files": diff.get("changed_files", []), "artifacts": artifact_refs,
                "artifact_manifest": str(manifest_path),
                "duration_ms": (time.monotonic() - job["_started"]) * 1000,
            })
        append_canonical_event("xrt_execution_finished", _public(job))
    except asyncio.CancelledError:
        with _LOCK:
            job.update({"status": "CANCELLED", "exit_code": 130})
        raise
    except Exception as exc:
        with _LOCK:
            job.update({"status": "FAILED", "exit_code": 1, "stderr": str(exc)[-4000:]})
    finally:
        with _LOCK:
            _RUNNING[runtime_id] = max(0, _RUNNING.get(runtime_id, 1) - 1)


async def cancel(execution_id: str) -> bool:
    with _LOCK:
        job = _JOBS.get(execution_id)
        if not job or job["status"] != "RUNNING":
            return False
        _CANCEL.cancel(execution_id)
        adapter = _ADAPTERS.get((job["_runtime_id"], job["_repo_root"]))
    if adapter is not None:
        await adapter.cancel(execution_id, execution_id)
    with _LOCK:
        job["status"] = "CANCELLED"
    return True


__all__ = ["cancel", "dispatch", "get_status", "list_runtimes"]
