"""GSK evidence and egress receipt endpoints; gated by existing auth and flags."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from server import auth as auth_mod
from veya.gsk import is_enabled
from veya.gsk.evidence import EvidenceLedger
from veya.gsk.receipts import EgressReceiptLedger

router = APIRouter(prefix="/api/v1/gsk", tags=["gsk"])


def _require_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Resolve the bearer token through Veya's existing session authority."""
    return auth_mod.require_user(authorization)


def require_owner(user: dict[str, Any] = Depends(_require_user)) -> dict[str, Any]:
    """Require an authenticated user whose ID is configured as the owner."""
    owner_id = os.getenv("VEYA_OWNER_USER_ID", "").strip()
    if not owner_id or str(user.get("user_id", "")) != owner_id:
        raise HTTPException(status_code=403, detail="Forbidden: owner-only route")
    return user


def _require_receipts_enabled() -> None:
    if not is_enabled("VEYA_GSK_EGRESS_RECEIPTS"):
        raise HTTPException(status_code=404, detail="GSK egress receipts are disabled")


def _require_evidence_enabled() -> None:
    if not is_enabled("VEYA_GSK_EVIDENCE"):
        raise HTTPException(status_code=404, detail="GSK evidence ledger is disabled")


@router.get("/receipts", dependencies=[Depends(_require_receipts_enabled), Depends(require_owner)])
async def list_receipts() -> dict[str, Any]:
    ledger = EgressReceiptLedger()
    return {"receipts": ledger.list_receipts(), "verification": ledger.verify()}


@router.get("/evidence", dependencies=[Depends(_require_evidence_enabled), Depends(require_owner)])
async def get_evidence(label: str) -> dict[str, Any]:
    ledger = EvidenceLedger()
    return {"label": label, "status": ledger.check(label), "record": ledger.get_record(label)}


__all__ = ["router", "require_owner"]
