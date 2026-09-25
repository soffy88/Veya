"""Thin bridge: orchestration Subtask -> canonical L1 ``worker.dispatch``.

It duplicates **nothing**: no worker implementation, no worker registry, no
durable execution engine. It builds exactly one canonical internal mission
context and drives the existing :class:`~veya.remote.tool_adapter.RemoteToolAdapter`
(``worker.dispatch`` / ``process.status`` / ``process.cancel``), so child
executions live in the same ExecutionStore, workspace binding and isolated
worktrees as every other L1 execution.

The internal context is an explicit, non-external session kind
(``SESSION_KIND=internal_mission``): it is not an external MCP client and is not
counted as a new external session or a shadow Mission.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from runtime.coding.worktree import teardown_worktree

from .orchestrated import Dispatch, Subtask, SubtaskResult

INTERNAL_TOKEN_ID = "internal-mission"
INTERNAL_PRINCIPAL = "internal_runtime"
_TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED"}
_FAILURE_TEXT_LIMIT = 4_000


def _bounded_text(value: Any, limit: int = _FAILURE_TEXT_LIMIT) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit] if text else None


def internal_session(workspace: str) -> Any:
    """Canonical internal mission context (not an external MCP session)."""

    from veya.remote.models import RemotePermissions, RemoteSession

    now = time.time()
    return RemoteSession(
        session_id=f"internal_mission:{workspace}",
        principal=INTERNAL_PRINCIPAL,
        token_id=INTERNAL_TOKEN_ID,
        workspaces=(workspace,),
        active_workspace=workspace,
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        created_at=now,
        expires_at=now + 86_400,
        client_info={"kind": "internal_mission", "authority": "internal_runtime"},
    )


class L1Bridge:
    """``Dispatch`` implementation over the canonical L1 substrate."""

    def __init__(
        self,
        workspace: str,
        *,
        adapter: Any = None,
        store: Any = None,
        poll_interval_s: float = 1.0,
        timeout_s: float = 900.0,
    ) -> None:
        from veya.remote.execution import ExecutionStore
        from veya.remote.tool_adapter import RemoteToolAdapter

        self.workspace = workspace
        self.adapter = adapter or RemoteToolAdapter(
            execution_store=store if store is not None else ExecutionStore(None)
        )
        self.session = internal_session(workspace)
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s
        self._active_parent_execution_ids: set[str] = set()
        self._cancel_requested_execution_ids: set[str] = set()

    async def _status(self, execution_id: str) -> dict[str, Any]:
        result = await self.adapter.call(
            self.session, "process.status", {"execution_id": execution_id}
        )
        return result.result if result.ok and isinstance(result.result, dict) else {}

    async def __call__(self, subtask: Subtask) -> SubtaskResult:
        # Isolation guard: the planner must not hand workers absolute owner-repo
        # paths (that would escape the isolated worktree). Rewrite any absolute
        # workspace path to a relative path before dispatch.
        started_at = time.monotonic()
        task_text = _sanitize_objective(subtask.objective, self.workspace)
        # Snapshot the isolation repo *before* dispatch. The child worktree is
        # created from it with ``git worktree add``, i.e. a clean checkout at
        # HEAD that never inherits the workspace's uncommitted state, so this
        # snapshot is the child's pre-worker state and cannot race the worker.
        baseline = await asyncio.to_thread(_git_baseline, self.workspace)
        dispatch_inputs = dict(subtask.inputs)
        if subtask.required_artifacts:
            dispatch_inputs["required_artifacts"] = list(subtask.required_artifacts)
            task_text += (
                "\n\nRequired deliverables (part of acceptance): create and verify these "
                "relative paths in the current execution worktree; do not only describe them: "
                + ", ".join(subtask.required_artifacts)
            )
        result = await self.adapter.call(
            self.session,
            "worker.dispatch",
            {
                "tasks": [
                    {
                        "worker": subtask.worker,
                        "task": task_text,
                        "timeout_sec": int(subtask.timeout_s or 0),
                        "inputs": dispatch_inputs,
                    }
                ]
            },
        )
        if not result.ok or not result.execution_id:
            return SubtaskResult(
                task_id=subtask.task_id,
                worker=subtask.worker.upper(),
                status="FAILED",
                error=result.message or "worker.dispatch rejected the subtask",
                failure_class="L1_DISPATCH_REJECTED",
                failure_source="l1_bridge",
                failure_message=result.message or "worker.dispatch rejected the subtask",
                failure_detail=result.message or "worker.dispatch rejected the subtask",
                phase="DISPATCH",
                elapsed_ms=round((time.monotonic() - started_at) * 1000, 3),
            )
        parent_execution_id = result.execution_id
        self._active_parent_execution_ids.add(parent_execution_id)
        try:
            deadline = time.time() + self.timeout_s
            while time.time() < deadline:
                payload = await self._status(parent_execution_id)
                children = payload.get("children") or []
                if children and all(str(c.get("status")) in _TERMINAL for c in children):
                    child = children[0]
                    worktree = str(child.get("worker_workspace") or "")
                    execution_id = str(child.get("execution_id") or "")
                    artifacts = await asyncio.to_thread(
                        _artifact_manifest,
                        execution_id,
                        subtask.task_id,
                        str(child.get("worker_type") or subtask.worker.upper()),
                        worktree,
                        str(Path(self.workspace) / ".veya" / "artifacts"),
                        baseline=baseline,
                    )
                    status = str(child.get("status"))
                    cleanup_evidence = (
                        await asyncio.to_thread(
                            teardown_worktree, worktree, execution_status=status
                        )
                        if worktree
                        else None
                    )
                    failure_message = (
                        _bounded_text(
                            child.get("failure_message")
                            or child.get("message")
                            or child.get("error")
                        )
                        if status != "COMPLETED"
                        else None
                    )
                    failure_detail = (
                        _bounded_text(child.get("failure_detail"))
                        if status != "COMPLETED"
                        else None
                    )
                    child_evidence = {
                        "kind": "l1_child",
                        "subtask_id": subtask.task_id,
                        "worker": child.get("worker_type"),
                        "execution_id": execution_id,
                        "status": status,
                        "workspace": worktree,
                        "worktree_cleanup": cleanup_evidence,
                        "failure_class": child.get("failure_class"),
                        "failure_source": child.get("failure_source"),
                        "failure_message": failure_message,
                        "failure_detail": failure_detail,
                        "provider_error_code": child.get("provider_error_code"),
                        "exit_code": child.get("exit_code"),
                        "phase": child.get("phase"),
                        "last_event": child.get("last_event"),
                        "model_request_count": child.get("model_request_count", 0),
                        "tool_call_count": child.get("tool_call_count", 0),
                    }
                    child_failure = (
                        {
                            "kind": "child_failure",
                            "subtask_id": subtask.task_id,
                            "execution_id": execution_id,
                            "worker": child.get("worker_type"),
                            "status": status,
                            "failure_class": child.get("failure_class"),
                            "failure_source": child.get("failure_source"),
                            "failure_message": failure_message,
                            "failure_detail": failure_detail,
                            "provider_error_code": child.get("provider_error_code"),
                            "exit_code": child.get("exit_code"),
                            "phase": child.get("phase"),
                            "last_event": child.get("last_event"),
                            "model_request_count": child.get("model_request_count", 0),
                            "tool_call_count": child.get("tool_call_count", 0),
                        }
                        if status != "COMPLETED"
                        else None
                    )
                    required_artifacts = list(subtask.required_artifacts)
                    materialized_paths = {
                        str(item.get("relative_path"))
                        for item in artifacts
                        if item.get("kind") == "artifact" and item.get("materialized_path")
                    }
                    missing_required_artifacts = [
                        path for path in required_artifacts if path not in materialized_paths
                    ]
                    if required_artifacts:
                        artifact_requirement = (
                            "SATISFIED" if not missing_required_artifacts else "UNSATISFIED"
                        )
                    else:
                        artifact_requirement = "OPTIONAL"
                    if status == "COMPLETED" and missing_required_artifacts:
                        status = "BLOCKED"
                        failure_message = "required artifacts missing: " + ", ".join(
                            missing_required_artifacts
                        )
                        failure_detail = failure_message
                        child_evidence["provider_status"] = "COMPLETED"
                        child_evidence["status"] = status
                        child_evidence["failure_class"] = "ARTIFACT_REQUIREMENT_UNSATISFIED"
                        child_evidence["failure_source"] = "l1_bridge"
                        child_evidence["failure_message"] = failure_message
                        child_evidence["failure_detail"] = failure_detail
                        child_failure = {
                            "kind": "child_failure",
                            "subtask_id": subtask.task_id,
                            "execution_id": execution_id,
                            "worker": child.get("worker_type"),
                            "status": status,
                            "failure_class": "ARTIFACT_REQUIREMENT_UNSATISFIED",
                            "failure_source": "l1_bridge",
                            "failure_message": failure_message,
                            "failure_detail": failure_detail,
                        }
                    dependency_ready = status == "COMPLETED" and not missing_required_artifacts
                    artifact_evidence = {
                        "kind": "artifact_requirement",
                        "subtask_id": subtask.task_id,
                        "execution_id": execution_id,
                        "worker": child.get("worker_type") or subtask.worker.upper(),
                        "artifact_requirement": artifact_requirement,
                        "required_artifacts": required_artifacts,
                        "missing_required_artifacts": missing_required_artifacts,
                        "dependency_ready": dependency_ready,
                    }
                    return SubtaskResult(
                        task_id=subtask.task_id,
                        worker=str(child.get("worker_type") or subtask.worker.upper()),
                        status=status,
                        execution_id=execution_id,
                        parent_execution_id=parent_execution_id,
                        summary=str(child.get("result_summary") or ""),
                        evidence=[
                            child_evidence,
                            *([child_failure] if child_failure is not None else []),
                            artifact_evidence,
                            *artifacts,
                        ],
                        error=child.get("error"),
                        elapsed_ms=child.get("elapsed_ms")
                        or round((time.monotonic() - started_at) * 1000, 3),
                        failure_class=child_evidence.get("failure_class"),
                        failure_source=child_evidence.get("failure_source"),
                        failure_message=failure_message,
                        failure_detail=failure_detail,
                        provider_error_code=child.get("provider_error_code"),
                        exit_code=child.get("exit_code"),
                        phase=child.get("phase"),
                        last_event=child.get("last_event"),
                        model_request_count=int(child.get("model_request_count") or 0),
                        tool_call_count=int(child.get("tool_call_count") or 0),
                        artifact_requirement=artifact_requirement,
                        dependency_ready=dependency_ready,
                        required_artifacts=required_artifacts,
                        missing_required_artifacts=missing_required_artifacts,
                    )
                await asyncio.sleep(self.poll_interval_s)
            return SubtaskResult(
                task_id=subtask.task_id,
                worker=subtask.worker.upper(),
                status="FAILED",
                parent_execution_id=parent_execution_id,
                error="l1 dispatch timed out waiting for the child execution",
                failure_class="L1_CHILD_TIMEOUT",
                failure_source="l1_bridge",
                failure_message="l1 dispatch timed out waiting for the child execution",
                failure_detail="l1 dispatch timed out waiting for the child execution",
                phase="WAITING_WORKERS",
                elapsed_ms=round((time.monotonic() - started_at) * 1000, 3),
            )
        finally:
            self._active_parent_execution_ids.discard(parent_execution_id)

    async def cancel(self, execution_id: str) -> bool:
        if execution_id in self._cancel_requested_execution_ids:
            return True
        self._cancel_requested_execution_ids.add(execution_id)
        result = await self.adapter.call(
            self.session, "process.cancel", {"execution_id": execution_id}
        )
        return bool(result.ok)

    async def cancel_active(self) -> int:
        """Cancel each active parent once and preserve completed child records."""

        execution_ids = sorted(self._active_parent_execution_ids)
        if not execution_ids:
            return 0
        results = await asyncio.gather(
            *(self.cancel(execution_id) for execution_id in execution_ids),
            return_exceptions=True,
        )
        return sum(result is True for result in results)


def make_l1_dispatch(workspace: str, **kwargs: Any) -> Dispatch:
    return L1Bridge(workspace, **kwargs)


def _sanitize_objective(objective: str, workspace: str) -> str:
    """Remove absolute owner-workspace paths from a subtask objective.

    A planner that names the owner repo root would otherwise let a worker write
    outside its isolated worktree (observed as owner-repo contamination).
    """

    if not workspace:
        return objective
    try:
        root = str(Path(workspace).resolve())
    except OSError:
        return objective
    return objective.replace(root + "/", "").replace(root, ".")


def _artifact_manifest(
    execution_id: str,
    subtask_id: str,
    worker_type: str,
    worktree: str,
    artifacts_root: str,
    *,
    baseline: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Per-child manifest of files the child really produced/modified.

    Reads only the child's own isolated worktree; materializes each artifact
    under ``<artifacts_root>/<execution_id>/<relative_path>`` so Review can read
    a controlled filesystem view without merging into the owner/mission repo.
    """

    if not worktree:
        return []
    root = Path(worktree)
    if not (root / ".git").exists():
        return []
    if baseline is None:
        baseline = _git_baseline(worktree)
    try:
        status_proc = subprocess.run(
            ["git", "-C", worktree, "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        diff_proc = subprocess.run(
            ["git", "-C", worktree, "diff", "--name-status", "HEAD", "--"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if status_proc.returncode != 0:
        return []
    final_status = status_proc.stdout.splitlines()
    status_by_path = _status_by_path(final_status)
    for line in diff_proc.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            status_by_path.setdefault(parts[1].strip(), parts[0].strip())
    entries: list[dict[str, Any]] = []
    for rel, status in sorted(status_by_path.items()):
        relative = Path(rel)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        source = (root / relative).resolve()
        if root.resolve() not in source.parents and source != root.resolve():
            continue
        # No baseline exclusion here on purpose. A child worktree is created by
        # ``git worktree add`` as a clean checkout at HEAD, so every path git
        # reports in it was produced by this lane's worker. Excluding paths seen
        # in a baseline snapshot is unsound: a snapshot taken after the worker
        # wrote (the observed race) silently dropped the real artifact, which
        # made required artifacts look missing and blocked dependent subtasks.
        exists = source.is_file()
        try:
            data = source.read_bytes() if exists else b""
        except OSError:
            continue
        digest = hashlib.sha256(data).hexdigest()
        materialized: Path | None = Path(artifacts_root).resolve() / execution_id / relative
        change = (
            "deleted" if "D" in status or not exists else "created" if "?" in status else "modified"
        )
        if exists and materialized is not None:
            try:
                materialized.parent.mkdir(parents=True, exist_ok=True)
                materialized.write_bytes(data)
            except OSError:
                materialized = None
        entries.append(
            {
                "kind": "artifact",
                "execution_id": execution_id,
                "subtask_id": subtask_id,
                "worker_type": worker_type,
                "source_worktree": worktree,
                "relative_path": rel,
                "artifact_type": Path(rel).suffix.lstrip(".") or "file",
                "size": len(data) if exists else None,
                "hash": digest if exists else None,
                "change": change,
                "materialized_path": str(materialized) if materialized and exists else None,
            }
        )
    manifest_payload = [
        {key: item.get(key) for key in ("relative_path", "size", "hash", "change")}
        for item in entries
    ]
    manifest_hash = hashlib.sha256(
        json.dumps(manifest_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    entries.insert(
        0,
        {
            "kind": "artifact_manifest",
            "execution_id": execution_id,
            "subtask_id": subtask_id,
            "worker_type": worker_type,
            "baseline_head": baseline.get("head"),
            "baseline_status": list(baseline.get("status") or []),
            "baseline_clean": bool(baseline.get("clean")),
            "final_status": final_status,
            "file_count": len(manifest_payload),
            "manifest_hash": manifest_hash,
        },
    )
    try:
        full_diff_proc = subprocess.run(
            ["git", "-C", worktree, "diff", "HEAD", "--"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if full_diff_proc.returncode == 0 and full_diff_proc.stdout:
            patch_dest = Path(artifacts_root).resolve() / execution_id / "diff.patch"
            try:
                patch_dest.parent.mkdir(parents=True, exist_ok=True)
                patch_dest.write_text(full_diff_proc.stdout, encoding="utf-8")
            except OSError:
                pass
    except (OSError, subprocess.SubprocessError):
        pass
    return entries


def _status_by_path(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        status = line[:2].strip() or "??"
        rel = line[3:].strip() if len(line) > 3 else line.strip()
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        result[rel] = status
    return result


def _git_baseline(worktree: str) -> dict[str, Any]:
    root = Path(worktree).resolve()
    if not root.exists():
        return {}
    try:
        head = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        status = subprocess.run(
            ["git", "-C", worktree, "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if head.returncode != 0 or status.returncode != 0:
        return {}
    lines = status.stdout.splitlines()
    return {
        "head": head.stdout.strip(),
        "status": lines,
        "paths": list(_status_by_path(lines)),
        "clean": not lines,
    }


__all__ = [
    "INTERNAL_PRINCIPAL",
    "INTERNAL_TOKEN_ID",
    "L1Bridge",
    "internal_session",
    "make_l1_dispatch",
]
