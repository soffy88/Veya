"""Optional OLV Phase 0 status route (default OFF).

Mounted on the root app like the other flag-gated surfaces (NANO-W7 channels,
W13 remote control): the router is always included, the endpoint itself 404s
while the flag is off.  Read-only — no write, no side effect, no tool
registration, and never a token value in the response.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from server import feature_flags
from server.vtuber_bridge import status as bridge_status

router = APIRouter(prefix="/api/v1/vtuber", tags=["vtuber-bridge"])

FLAG_NAME = "VEYA_OLV"


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """Read-only projection of the OLV bypass integration.

    404 while disabled so a disabled deployment does not advertise the surface.
    """
    if not feature_flags.enabled(FLAG_NAME):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"vtuber bridge disabled ({FLAG_NAME}=0)",
        )
    return bridge_status()
