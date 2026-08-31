"""Veya Bot product-shell endpoints.

These endpoints expose configuration/readiness metadata only.  They do not
replace the existing task, session, approval, or execution authorities.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.product_shell import configure_bot, read_bot_state

router = APIRouter(prefix="/api/v1/bot", tags=["product"])


class BotOnboardingRequest(BaseModel):
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    workspace: str | None = Field(default=None, max_length=4096)
    # Reference only: raw API keys are intentionally not accepted here.
    credential_ref: str | None = Field(default=None, min_length=1, max_length=512)


@router.get("")
async def get_bot() -> dict[str, Any]:
    """Return the secret-free default Bot identity and binding snapshot."""

    return read_bot_state()


@router.post("/onboarding")
async def complete_onboarding(req: BotOnboardingRequest) -> dict[str, Any]:
    """Persist explicit first-run product configuration and return its state."""

    try:
        return configure_bot(
            provider=req.provider,
            model=req.model,
            workspace=req.workspace,
            credential_ref=req.credential_ref,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=503, detail="product configuration is not writable"
        ) from exc
