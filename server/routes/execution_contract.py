from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from veya.remote.execution import DurableJobManager, ExecutionStore
from veya.remote.execution_contract import probe_runtime_capability_manifest
from veya.remote.executor_health import registry_order
from veya.remote.runtime_profile import discover_runtime_profile

router = APIRouter(prefix="/api/v1/execution", tags=["Execution Contract"])

_CANONICAL_MANAGER: DurableJobManager | None = None


def get_job_manager() -> DurableJobManager:
    global _CANONICAL_MANAGER
    if _CANONICAL_MANAGER is None:
        store = ExecutionStore.from_env(default_persistent=True)
        _CANONICAL_MANAGER = DurableJobManager(store)
    return _CANONICAL_MANAGER


@router.get("/runtime/list")
async def runtime_list() -> dict[str, Any]:
    """List runtime capability manifests for all canonical executors."""
    manager = get_job_manager()
    manifests: list[dict[str, Any]] = []
    active_count = len([t for t in manager._tasks.values() if not t.done()])
    for executor_id in registry_order():
        manifest = probe_runtime_capability_manifest(
            executor_id,
            active_executions=active_count,
        )
        manifests.append(asdict(manifest))
    return {"runtimes": manifests}


@router.get("/runtime/{runtime_id}")
async def runtime_get(runtime_id: str) -> dict[str, Any]:
    """Get active runtime profile for a runtime/workspace."""
    target_path = Path(runtime_id).expanduser() if os.path.exists(runtime_id) else Path.cwd()
    profile = discover_runtime_profile(str(target_path))
    return {"runtime": profile.to_dict()}


@router.get("/{execution_id}")
async def execution_get(execution_id: str) -> dict[str, Any]:
    """Get execution status and metadata from canonical execution store."""
    manager = get_job_manager()
    record = manager.lookup(execution_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    return {"execution": record.to_json()}


@router.get("/{execution_id}/capabilities")
async def execution_capabilities(execution_id: str) -> dict[str, Any]:
    """Probe runtime capabilities for an active execution's target and workspace."""
    manager = get_job_manager()
    record = manager.lookup(execution_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    worker = record.worker_type or "hicode"
    manifest = probe_runtime_capability_manifest(
        worker,
        workspace_path=record.requested_realpath,
        active_executions=len([t for t in manager._tasks.values() if not t.done()]),
    )
    return {"capabilities": asdict(manifest)}


@router.post("/{execution_id}/suspend")
async def execution_suspend(execution_id: str) -> dict[str, Any]:
    """Suspend an active execution preserving durable lineage."""
    manager = get_job_manager()
    record = manager.lookup(execution_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    token = record.token_id or "system"
    suspended = await manager.suspend(execution_id, token_id=token)
    return {
        "execution_id": suspended.execution_id,
        "status": suspended.status,
        "phase": str(suspended.phase),
    }


@router.post("/{execution_id}/resume")
async def execution_resume(execution_id: str) -> dict[str, Any]:
    """Resume a suspended execution on the same durable lineage."""
    manager = get_job_manager()
    record = manager.lookup(execution_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    token = record.token_id or "system"
    resumed = await manager.resume(execution_id, token_id=token)
    return {
        "execution_id": resumed.execution_id,
        "status": resumed.status,
        "phase": str(resumed.phase),
    }


@router.post("/{execution_id}/recover")
async def execution_recover(execution_id: str) -> dict[str, Any]:
    """Recover an execution after process restart or crash."""
    manager = get_job_manager()
    record = manager.lookup(execution_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    recovered_count = await manager.recover_unfinished()
    refreshed = manager.lookup(execution_id) or record
    return {
        "execution_id": refreshed.execution_id,
        "status": refreshed.status,
        "phase": str(refreshed.phase),
        "recovered_count": recovered_count,
    }


@router.post("/{execution_id}/cancel")
async def execution_cancel(execution_id: str) -> dict[str, Any]:
    """Cancel an active execution."""
    manager = get_job_manager()
    record = manager.lookup(execution_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    token = record.token_id or "system"
    cancelled = await manager.cancel(execution_id, token_id=token)
    return {
        "execution_id": cancelled.execution_id,
        "status": cancelled.status,
        "phase": str(cancelled.phase),
    }


@router.post("/{execution_id}/terminate")
async def execution_terminate(execution_id: str) -> dict[str, Any]:
    """Terminate an active execution and release all resources."""
    manager = get_job_manager()
    record = manager.lookup(execution_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    token = record.token_id or "system"
    terminated = await manager.cancel(execution_id, token_id=token)
    return {
        "execution_id": terminated.execution_id,
        "status": "TERMINATED",
        "phase": "TERMINATED",
    }
