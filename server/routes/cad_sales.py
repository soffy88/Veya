"""HTTP endpoints for CAD sales bot (闲鱼CAD代画销转).

Gated by feature flag `VEYA_CAD_SALES_BOT` (default OFF).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from server import feature_flags
from server.cad_sales.engine import CadSalesEngine
from server.cad_sales.models import (
    CadSalesRequest,
    CadSalesResult,
    RequirementsRequest,
    RequirementsResponse,
)
from server.cad_sales.requirements import generate_order_folder

router = APIRouter(prefix="/api/v1/cad-sales", tags=["cad-sales"])

FLAG_NAME = "VEYA_CAD_SALES_BOT"


def _check_flag() -> None:
    if not feature_flags.enabled(FLAG_NAME):
        raise HTTPException(
            status_code=409,
            detail=f"CAD sales bot is disabled ({FLAG_NAME}=0)",
        )


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """Check if CAD sales bot runtime is enabled."""
    return {
        "enabled": feature_flags.enabled(FLAG_NAME),
        "flag": FLAG_NAME,
        "role_preset": "cad-sales",
        "skill_preset": "cad-sales",
    }


@router.post("/reply", response_model=CadSalesResult)
async def generate_sales_reply(req: CadSalesRequest) -> CadSalesResult:
    """Analyze a buyer conversation and attachments, producing tier & reply draft."""
    _check_flag()
    engine = CadSalesEngine()
    return engine.analyze(req)


@router.post("/requirements", response_model=RequirementsResponse)
async def create_requirements(req: RequirementsRequest) -> RequirementsResponse:
    """Analyze a conversation and write the 5-stage order folder and requirements.md."""
    _check_flag()
    return generate_order_folder(
        conversation=req.conversation,
        attachments=req.attachments,
        order_id=req.order_id,
        output_root=req.output_root,
    )
