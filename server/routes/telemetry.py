"""server.routes.telemetry — P3-04 Telemetry v1 API.

GET  /api/v1/telemetry/traces        列出最近 trace 清单
GET  /api/v1/telemetry/traces/{id}   回放单条 trace
POST /api/v1/telemetry/export        导出 trace (jsonl / otlp)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from server.telemetry import TelemetryAPI

router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry"])

_api = TelemetryAPI()


@router.get("/traces")
async def list_traces(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """列出最近 trace 清单 (P3-04 Telemetry v1)。"""
    return _api.traces(limit=limit)


@router.get("/metrics")
async def metrics() -> dict[str, Any]:
    """Return runtime success/failure counters without inventing missing samples."""
    result = _api.metrics()
    try:
        from runtime.execution.runtime import get_durable_runtime

        runtime = get_durable_runtime()
        if runtime.config.enabled and runtime._started:
            result["execution_runtime"] = await runtime.health()
    except Exception:
        # Existing telemetry remains available while the optional projection
        # is unavailable; the durable health endpoint is the authoritative
        # readiness signal.
        pass
    return result


@router.get("/traces/{trace_id}")
async def replay_trace(trace_id: str) -> dict[str, Any]:
    """回放一条 trace 的完整决策链路。"""
    result = _api.replay(trace_id)
    if result["event_count"] == 0:
        raise HTTPException(status_code=404, detail=f"trace not found: {trace_id}")
    return result


class ExportRequest(BaseModel):
    trace_id: str
    format: str = "jsonl"  # jsonl | otlp
    output: str | None = None


@router.post("/export")
async def export_trace(req: ExportRequest) -> dict[str, Any]:
    """导出 trace 为 JSONL 或 OTLP 格式。"""
    if req.format not in ("jsonl", "otlp"):
        raise HTTPException(status_code=422, detail=f"unsupported format: {req.format}")
    return _api.export_trace(req.trace_id, fmt=req.format, output=req.output)
