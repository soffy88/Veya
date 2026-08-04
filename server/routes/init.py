"""POST /init — M-04 init_project: scan repo + LLM → AGENTS.md"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/init", tags=["init"])


class InitRequest(BaseModel):
    repo: str = "."
    max_files: int = 500


@router.post("")
async def init_project_route(req: InitRequest) -> dict[str, Any]:
    from server.assembly import _llm_caller_adapter
    from veya.compat import init_project

    root = Path(req.repo).resolve()
    if not root.exists():
        raise HTTPException(status_code=400, detail=f"Path not found: {root}")

    # Write AGENTS.md to project root, trail artefacts to temp dir
    output_dir = root

    config = {"max_files": req.max_files}
    input_data = {
        "root_path": str(root),
        "llm_caller": _llm_caller_adapter,
    }

    result = await init_project(config, input_data, output_dir)
    if result.get("status") != "completed":
        raise HTTPException(status_code=500, detail=result.get("error"))

    agents_md = root / "AGENTS.md"
    return {
        "status": "success",
        "agents_md": str(agents_md),
        "exists": agents_md.exists(),
        "size_bytes": agents_md.stat().st_size if agents_md.exists() else 0,
        "cost_usd": result.get("cost_usd", 0.0),
    }
