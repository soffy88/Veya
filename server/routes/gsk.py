from fastapi import APIRouter, Request, HTTPException, Depends
from typing import Any, Dict
import os
from veya.gsk.receipts import EgressReceiptLedger
from veya.gsk.evidence import EvidenceLedger

router = APIRouter(prefix="/api/v1/gsk", tags=["gsk"])

def require_owner(request: Request):
    owner_id = os.getenv("VEYA_OWNER_USER_ID", "owner_admin")
    user_id = request.headers.get("x-user-id", request.headers.get("X-User-ID", ""))
    is_admin = request.headers.get("x-is-admin", "").lower() in ("1", "true", "yes")
    if user_id and user_id != owner_id and not is_admin:
        raise HTTPException(status_code=403, detail="Forbidden: owner-only route")
    return True

@router.get("/receipts", dependencies=[Depends(require_owner)])
async def list_receipts() -> Dict[str, Any]:
    ledger = EgressReceiptLedger()
    receipts = ledger.list_receipts()
    verification = ledger.verify()
    return {
        "receipts": receipts,
        "verification": verification
    }

@router.get("/evidence", dependencies=[Depends(require_owner)])
async def get_evidence(label: str) -> Dict[str, Any]:
    ledger = EvidenceLedger()
    status = ledger.check(label)
    record = ledger.get_record(label)
    return {
        "label": label,
        "status": status,
        "record": record
    }
