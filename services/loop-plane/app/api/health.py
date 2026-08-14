"""loop-plane api.health — /healthz。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

import veya_loop

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    from app.deps import get_audit, get_settings, get_store

    settings = get_settings()
    return {
        "status": "ok",
        "version": "1.0",
        "modules": {
            "state": True,
            "causal": _causal_available(),
            "exec": True,
            "sched": True,
            "skills": False,  # P2 stub
        },
        "veya_loop_version": veya_loop.__version__,
        "data_dir": str(settings.data_dir),
        "events": len(get_store().stream()),
        "audit_events": len(get_audit().by_trace("")),
    }


def _causal_available() -> bool:
    try:
        from veya_loop import causal_fault_diagnose  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = ["router"]
