"""GBP2: Teach once save as skill REST endpoints (VEYA_TEACH_SKILL)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from runtime.personal import PersonalRuntimeError, get_personal_runtime
from server import auth as auth_mod
from server.approvals import get_approval_service
from server.feature_flags import enabled as flag_enabled

router = APIRouter(prefix="/api/v1/skills/teach", tags=["skill-teach"])


def _check_flag():
    try:
        if not flag_enabled("VEYA_TEACH_SKILL"):
            raise HTTPException(status_code=404, detail="feature disabled")
    except KeyError:
        raise HTTPException(status_code=404, detail="feature disabled")


class TeachDraftRequest(BaseModel):
    name: str
    description: str
    trajectory: dict[str, Any] | list[Any] = Field(default_factory=dict)
    scope_type: str = "user"
    scope_id: str | None = None


@router.post("/draft", status_code=201)
async def create_skill_draft_from_trajectory(
    req: TeachDraftRequest,
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    _check_flag()
    user_id = str(user.get("user_id") or "user-1")
    scope_id = (user_id if req.scope_type == "user" else req.scope_id) or user_id

    judgment_suffix = "\n\n[需要你补的判断规则]\n请补充触发该技能的条件、边界与预期校验规则。"
    full_description = req.description.strip() + judgment_suffix

    runtime = get_personal_runtime()
    try:
        created = await runtime.create_skill_candidate(
            req.name,
            full_description,
            scope_type=req.scope_type,
            scope_id=scope_id,
            execution_type="prompt",
            execution_ref=str(req.trajectory),
            created_by=user_id,
        )
    except PersonalRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    skill_id = created.get("skill_id")
    version_id = created.get("id")

    eval_gate_passed = None
    eval_result = None
    if flag_enabled("VEYA_SKILL_EVAL_GATE"):
        from server.skill_eval_gate import evaluate_skill_candidate

        eval_result = await evaluate_skill_candidate(
            candidate={
                "name": req.name,
                "description": full_description,
                "skill_id": skill_id,
                "version_id": version_id,
            },
            trajectory=req.trajectory,
            user_id=user_id,
            scope_type=req.scope_type,
            scope_id=scope_id,
        )
        eval_gate_passed = eval_result.get("passed", False)
        if not eval_gate_passed:
            return {
                "status": "eval_rejected",
                "skill_id": skill_id,
                "version_id": version_id,
                "approval_id": None,
                "eval_gate_passed": False,
                "eval_result": eval_result,
                "description": full_description,
                "reason": "Skill evaluation gate failed: candidate regressed or had critical errors against parent/baseline",
            }

    approval_svc = get_approval_service()
    approval_id = None
    try:
        approval_args: dict[str, Any] = {
            "skill_id": skill_id,
            "skill_version_id": version_id,
            "name": req.name,
        }
        if eval_result:
            approval_args["eval_evidence"] = eval_result
        approval_res = await approval_svc.request(
            tool="teach_skill_draft",
            args=approval_args,
            user_id=user_id,
            reason=f"Approve skill draft '{req.name}' from trajectory",
        )
        approval_id = approval_res.get("approval_id") or approval_res.get("approval", {}).get(
            "request_id"
        )
    except Exception:
        pass

    out: dict[str, Any] = {
        "status": "draft",
        "skill_id": skill_id,
        "version_id": version_id,
        "approval_id": approval_id,
        "description": full_description,
    }
    if eval_gate_passed is not None:
        out["eval_gate_passed"] = eval_gate_passed
        out["eval_result"] = eval_result
    return out


@router.get("/drafts")
async def list_skill_drafts(
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    _check_flag()
    user_id = str(user.get("user_id") or "user-1")
    runtime = get_personal_runtime()
    skills = await runtime.search_skills(
        "", scope_type="user", scope_id=user_id, include_candidates=True, limit=100
    )
    drafts = [s for s in skills if s.get("status") in {"candidate", "draft"}]
    return {"drafts": drafts, "count": len(drafts)}
