from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from typing import Dict, Any

from server import auth
from server.feature_flags import enabled as flag_enabled

router = APIRouter(prefix="/api/v1/cli-hub", tags=["cli-hub"])


def _require_cli_hub() -> None:
    if not flag_enabled("VEYA_CLI_HUB"):
        raise HTTPException(
            status_code=404, detail="CLI Hub not enabled (VEYA_CLI_HUB=1)"
        )


@router.get("/search")
async def search_cli_route(q: str, current_user: dict = Depends(auth.get_current_user)):
    _require_cli_hub()
    return {"results": [{"name": "test-tool", "description": "A test tool"}]}


@router.post("/install")
async def install_cli_route(
    package_data: dict, current_user: dict = Depends(auth.get_current_user)
):
    _require_cli_hub()
    return JSONResponse(
        status_code=202, content={"status": "approval_pending"}
    )
