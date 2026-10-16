"""GSK evidence and egress receipt endpoints; gated by existing auth and flags."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from server.routes.tailnet import require_tailnet_owner
from veya.gsk import is_enabled
from veya.gsk.evidence import EvidenceLedger
from veya.gsk.receipts import EgressReceiptLedger

router = APIRouter(prefix="/api/v1/gsk", tags=["gsk"])



def _require_receipts_enabled() -> None:
    if not is_enabled("VEYA_GSK_EGRESS_RECEIPTS"):
        raise HTTPException(status_code=404, detail="GSK egress receipts are disabled")


def _require_evidence_enabled() -> None:
    if not is_enabled("VEYA_GSK_EVIDENCE"):
        raise HTTPException(status_code=404, detail="GSK evidence ledger is disabled")


@router.get("/receipts", dependencies=[Depends(_require_receipts_enabled), Depends(require_tailnet_owner)])
async def list_receipts() -> dict[str, Any]:
    ledger = EgressReceiptLedger()
    return {"receipts": ledger.list_receipts(), "verification": ledger.verify()}


@router.get("/evidence", dependencies=[Depends(_require_evidence_enabled), Depends(require_tailnet_owner)])
async def get_evidence(label: str) -> dict[str, Any]:
    ledger = EvidenceLedger()
    return {"label": label, "status": ledger.check(label), "record": ledger.get_record(label)}


__all__ = ["router"]
