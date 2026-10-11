from fastapi import APIRouter, Depends, HTTPException
from server import auth

router = APIRouter(prefix="/api/v1/runners", tags=["runner-ops"])

@router.get("/check-update")
async def check_update(current_user: dict = Depends(auth.get_current_user)):
    return {"update_available": True}

@router.post("/{runner_id}/request-update")
async def request_update(runner_id: str, current_user: dict = Depends(auth.get_current_user)):
    # Mocking approval requirement
    return {"status": "approval_pending"}, 202

@router.post("/{runner_id}/service/install")
async def service_install(runner_id: str, current_user: dict = Depends(auth.get_current_user)):
    return {"status": "installed"}

@router.get("/{runner_id}/service/status")
async def service_status(runner_id: str, current_user: dict = Depends(auth.get_current_user)):
    return {"status": "running"}

