"""HTTP endpoints for CAD sales bot (闲鱼CAD代画销转).

Gated by feature flag `VEYA_CAD_SALES_BOT` (default OFF).
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException

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


TOKEN_ENV = "VEYA_CAD_SALES_TOKEN"


def _check_flag() -> None:
    if not feature_flags.enabled(FLAG_NAME):
        raise HTTPException(
            status_code=409,
            detail=f"CAD sales bot is disabled ({FLAG_NAME}=0)",
        )


def _check_token(x_veya_token: str | None, authorization: str | None) -> None:
    """S2S auth: this prefix bypasses session auth, so a shared token is mandatory (fail closed)."""
    expected = os.environ.get(TOKEN_ENV, "")
    if not expected:
        raise HTTPException(status_code=503, detail=f"{TOKEN_ENV} not configured")
    got = x_veya_token or ""
    if not got and authorization and authorization.lower().startswith("bearer "):
        got = authorization[7:]
    if not hmac.compare_digest(got.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid token")


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
async def generate_sales_reply(
    req: CadSalesRequest,
    x_veya_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> CadSalesResult:
    """Analyze a buyer conversation and attachments, producing tier & reply draft."""
    _check_flag()
    _check_token(x_veya_token, authorization)
    engine = CadSalesEngine()
    return engine.analyze(req)


@router.post("/requirements", response_model=RequirementsResponse)
async def create_requirements(
    req: RequirementsRequest,
    x_veya_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> RequirementsResponse:
    """Analyze a conversation and write the 5-stage order folder and requirements.md."""
    _check_flag()
    _check_token(x_veya_token, authorization)
    if req.output_root:
        raise HTTPException(status_code=400, detail="output_root is not accepted over HTTP")
    return generate_order_folder(
        conversation=req.conversation,
        attachments=req.attachments,
        order_id=req.order_id,
    )
