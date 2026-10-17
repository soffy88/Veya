"""server/routes/nano_w9.py — HTTP surface for NANO-W9.

Thin layer 4 (SPEC §8): pseudonymise, call the omodul / 3O element through the
``server/nano_w9`` facades, return a stable envelope.  No algorithm lives here.

Every gated surface returns **404** with ``{"code": "FEATURE_DISABLED"}`` when
its flag is off — an operator can tell "disabled" from "broken".  Search and the
read-only trajectory replay are ungated (the spec allows those to be on).
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod
from server.nano_w9.flags import FeatureDisabled, feature_flags, require_flag

# Every W9 surface requires a resolved caller (the repo-wide auth dependency).
router = APIRouter(
    prefix="/api/v1/nano-w9",
    tags=["nano-w9"],
    dependencies=[Depends(auth_mod.get_current_user)],
)


def _allowed_roots() -> list[str]:
    raw = (
        os.environ.get("VEYA_ALLOWED_REPO_ROOTS")
        or os.environ.get("VEYA_PROJECT_ROOT")
        or os.getcwd()
    )
    return [os.path.realpath(r) for r in raw.split(os.pathsep) if r.strip()]


def _safe_path(value: str, field: str) -> str:
    """Reject client-supplied paths outside the configured roots (400)."""
    if not value or value == ".":
        return value
    real = os.path.realpath(value)
    for root in _allowed_roots():
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            return value
    raise HTTPException(status_code=400, detail={"code": "PATH_NOT_ALLOWED", "field": field})


def _guard(flag: str) -> None:
    """Raise 404 for a disabled surface, else return."""
    try:
        require_flag(flag)
    except FeatureDisabled as exc:
        raise HTTPException(status_code=404, detail=exc.as_error()) from exc


def _subject_ref(user: dict[str, Any] | None) -> str:
    """Pseudonymise the user id before it reaches any 3O element (§5.5.1).

    Uses ``obase.pseudo_anonymizer`` (single source) so the same user always
    yields the same stable reference and no raw identity enters a fingerprint,
    a trail, or a receipt.
    """
    raw = str((user or {}).get("user_id") or "")
    if not raw:
        return "anonymous"
    try:
        from veya.platform import load

        fn = getattr(load("obase"), "pseudo_anonymize_user", None)
        if fn is not None:
            salt = os.environ.get("OBASE_PSEUDO_SALT", "")
            return str(fn(raw, salt=salt) or "anonymous")
    except Exception:
        pass
    import hashlib

    salt = os.environ.get("OBASE_PSEUDO_SALT", "")
    return hashlib.sha256((salt + raw).encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------
# flags
# --------------------------------------------------------------------------


@router.get("/flags")
async def read_flags() -> dict[str, Any]:
    """Current state of every NANO-W9 feature flag."""
    return {"status": "ok", "flags": feature_flags()}


# --------------------------------------------------------------------------
# element 1 — branch rooms
# --------------------------------------------------------------------------


class OpenRoomRequest(BaseModel):
    branch: str
    bot_ids: list[str] = []
    repo_path: str = ""


class PublishRoomRequest(BaseModel):
    branch: str
    group_id: str
    repo_path: str = ""
    goal_id: str = ""
    project_root: str = "."
    merge_decision: str = ""
    bot_id: str = "branch-bot"


def _branch_rooms_facade():
    from server.bot_team.groups import GroupStore
    from server.nano_w9.branch_rooms import BranchRooms

    store = GroupStore()

    def create_group(name: str, bot_ids: list[str]) -> str:
        return store.create(name=name, bot_ids=list(bot_ids or ["branch-bot"])).id

    def list_groups() -> list[Any]:
        return store.list()

    def post_message(group_id: str, role: str, text: str, bot_id: str) -> dict[str, Any] | None:
        return store.append_message(group_id, role=role, text=text, bot_id=bot_id)

    def fetch_checks(branch: str, repo_path: str) -> list[str]:
        from server.routes.review import _fetch_failing_checks

        names, _ = _fetch_failing_checks(branch, repo_path)
        return names

    def fetch_commits(repo_path: str, branch: str, limit: int) -> list[dict[str, Any]]:
        from veya.oprim.git import git_log

        rows = git_log(repo_path, limit=limit) if repo_path else []
        return [
            {
                "sha": str(r.get("hash") or r.get("sha") or ""),
                "subject": str(r.get("subject") or ""),
            }
            for r in rows
            if isinstance(r, dict)
        ]

    def fetch_annotations(project_root: str, goal_id: str) -> list[dict[str, Any]]:
        if not goal_id:
            return []
        from server.goal_run.annotations import list_annotations

        return [a.to_dict() for a in list_annotations(project_root, goal_id)]

    return BranchRooms(
        create_group=create_group,
        list_groups=list_groups,
        post_message=post_message,
        fetch_checks=fetch_checks,
        fetch_commits=fetch_commits,
        fetch_annotations=fetch_annotations,
    )


@router.post("/branch-rooms/open")
async def open_branch_room(payload: OpenRoomRequest) -> dict[str, Any]:
    """Open (or reuse) the room for a branch."""
    _guard("VEYA_BRANCH_ROOMS")
    _safe_path(payload.repo_path, "repo_path")
    facade = _branch_rooms_facade()
    result = await facade.open_room(
        payload.branch, bot_ids=payload.bot_ids, repo_path=payload.repo_path
    )
    if result.get("status") != "completed":
        raise HTTPException(status_code=400, detail=result.get("error") or {"code": "FAILED"})
    return {"status": "ok", **result}


@router.post("/branch-rooms/publish")
async def publish_branch_room(payload: PublishRoomRequest) -> dict[str, Any]:
    """Publish commits / CI / annotations / AI review / merge decision into a room."""
    _guard("VEYA_BRANCH_ROOMS")
    _safe_path(payload.repo_path, "repo_path")
    _safe_path(payload.project_root, "project_root")
    facade = _branch_rooms_facade()
    result = await facade.publish(
        branch=payload.branch,
        group_id=payload.group_id,
        repo_path=payload.repo_path,
        goal_id=payload.goal_id,
        project_root=payload.project_root,
        merge_decision=payload.merge_decision,
        bot_id=payload.bot_id,
    )
    if result.get("status") != "completed":
        raise HTTPException(status_code=400, detail=result.get("error") or {"code": "FAILED"})
    return {"status": "ok", **result}


@router.get("/branch-rooms")
async def list_branch_rooms() -> dict[str, Any]:
    """List the branch rooms currently open."""
    _guard("VEYA_BRANCH_ROOMS")
    return {"status": "ok", "rooms": _branch_rooms_facade().list_rooms()}


# --------------------------------------------------------------------------
# element 2 — routine triggers
# --------------------------------------------------------------------------


class SignalRequest(BaseModel):
    kind: str
    text: str = ""
    pattern: str = ""
    content: str = ""
    reaction: str = ""
    event: str = ""
    source: str = ""
    verified: bool = False
    message_author: str = ""
    id: str = ""
    bot_id: str = ""
    group_id: str = ""


def _routine_router():
    import time
    import uuid

    from server.goal_run.models import GoalRunState
    from server.goal_run.routine_dispatch import dispatch_trigger
    from server.goal_run.routine_history import RoutineHistoryStore
    from server.goal_run.routine_registry import RoutineRegistry
    from server.nano_w9.routine_triggers import RoutineSignalRouter

    try:
        registry = RoutineRegistry()
    except Exception:
        registry = None

    def default_dispatch(
        topic: str,
        trigger_id: str,
        payload: dict[str, Any],
        routine_id: str = "",
        routine: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        target_bot_id = (
            getattr(routine, "bot_id", None)
            or payload.get("bot_id")
            or "veya-default"
        )
        target_registry = RoutineRegistry(bot_id=target_bot_id)
        routines: list[Any] = []
        if routine is not None:
            routines = [routine]
        elif routine_id:
            r = target_registry.get(routine_id)
            if r:
                routines = [r]
        if not routines and target_registry:
            routines = target_registry.lookup(topic, bot_id=target_bot_id)

        history_store = RoutineHistoryStore(bot_id=target_bot_id)
        state = GoalRunState(
            goal_id=uuid.uuid4().hex,
            goal_text=str(
                payload.get("event")
                or payload.get("text")
                or payload.get("pattern")
                or "signal"
            ),
            bot_id=target_bot_id,
        )
        handoffs = dispatch_trigger(
            state, routines, topic=topic, trigger_id=trigger_id, payload=payload
        )
        for h in handoffs:
            history_store.add_record(
                h["routine_id"],
                {
                    "trigger_id": trigger_id,
                    "payload": payload,
                    "at": time.time(),
                    "status": "dispatched",
                },
            )
        return {"handoffs": handoffs}

    return RoutineSignalRouter(registry=registry, dispatch=default_dispatch)


@router.post("/signals")
async def handle_signal(payload: SignalRequest) -> dict[str, Any]:
    """Feed one inbound signal to the routine trigger router."""
    from server.nano_w9.flags import flag_enabled

    if not flag_enabled("VEYA_ROUTINE_TRIGGERS") and not flag_enabled("VEYA_EVENT_TRIGGERS"):
        _guard("VEYA_ROUTINE_TRIGGERS")
    signal = payload.model_dump()
    if not signal.get("text") and (signal.get("pattern") or signal.get("content")):
        signal["text"] = signal.get("pattern") or signal.get("content")
    # "verified" is asserted by trusted adapters in-process, never by an HTTP caller unless non-webhook
    if signal.get("kind") == "webhook":
        signal["verified"] = False
    result = await _routine_router().handle(signal)
    return {"status": result["status"], **result}


signals_compat_router = APIRouter(
    prefix="/api/v1",
    tags=["signals"],
    dependencies=[Depends(auth_mod.get_current_user)],
)


@signals_compat_router.post("/signals")
async def handle_signal_compat(payload: SignalRequest) -> dict[str, Any]:
    """Alias for /api/v1/nano-w9/signals mounted at /api/v1/signals."""
    return await handle_signal(payload)


# --------------------------------------------------------------------------
# element 3 — ACP members
# --------------------------------------------------------------------------


class AcpMemberRequest(BaseModel):
    backend_id: str
    owner_id: str = ""
    display_name: str = ""
    description: str = ""


class AcpTurnRequest(BaseModel):
    member_id: str
    prompt: str
    goal_id: str = ""
    wait: bool = False


def _acp_members():
    from server.goal_run.bot_identity import BotRegistry
    from server.nano_w9.acp_members import AcpMembers

    return AcpMembers(
        bot_registry=BotRegistry(), project_root=os.environ.get("VEYA_PROJECT_ROOT", ".")
    )


@router.get("/acp/members")
async def list_acp_members() -> dict[str, Any]:
    """List registered ACP members and detectable candidates."""
    _guard("VEYA_ACP_MEMBERS")
    facade = _acp_members()
    return {
        "status": "ok",
        "members": facade.members(),
        "candidates": facade.candidates(),
    }


@router.post("/acp/members")
async def register_acp_member(
    payload: AcpMemberRequest, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    """Register an ACP backend as a group/bot member (owned by the caller)."""
    _guard("VEYA_ACP_MEMBERS")
    result = _acp_members().register_member(
        payload.backend_id,
        owner_id=str(user.get("user_id") or ""),
        display_name=payload.display_name,
        description=payload.description,
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("error") or {"code": "FAILED"})
    return {"status": "ok", **result}


@router.post("/acp/turn")
async def forward_acp_turn(payload: AcpTurnRequest) -> dict[str, Any]:
    """Forward one turn to an ACP member through the existing GoalRun."""
    _guard("VEYA_ACP_MEMBERS")
    result = await _acp_members().forward_turn(
        member_id=payload.member_id,
        prompt=payload.prompt,
        goal_id=payload.goal_id,
        wait=payload.wait,
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result.get("error") or {"code": "FAILED"})
    return {"status": "ok", **result}


# --------------------------------------------------------------------------
# element 4 — audit chain
# --------------------------------------------------------------------------


@router.get("/audit/verify")
async def verify_audit() -> dict[str, Any]:
    """Verify the append-only hash chain over the existing event log."""
    _guard("VEYA_AUDIT_CHAIN")
    from server.nano_w9.audit_chain import audit_envelopes, verify_audit_chain

    return {"status": "ok", **verify_audit_chain(), "records": audit_envelopes(limit=50)}


# --------------------------------------------------------------------------
# element 5 — search with receipts (read-only, ungated)
# --------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str
    limit: int = 20
    since: float = 0.0
    sources: list[str] = []


@router.post("/search")
async def search_receipts(
    payload: SearchRequest, user: dict[str, Any] = Depends(auth_mod.get_current_user)
) -> dict[str, Any]:
    """Unified, cited search over sessions / goals / groups / approvals / annotations.

    The caller is pseudonymised before it reaches any 3O element; no raw user id
    is ever placed into a fingerprint, trail, or receipt.
    """
    from server.nano_w9.search_receipts import ReceiptSearch, default_sources

    sources = default_sources()
    if payload.sources:
        sources = {k: v for k, v in sources.items() if k in payload.sources}
    search = ReceiptSearch(sources=sources, ref=_subject_ref(user))
    return {
        "status": "ok",
        **search.answer(payload.query, limit=payload.limit, since=payload.since),
    }


# --------------------------------------------------------------------------
# addendum E — trajectory replay (read-only, ungated)
# --------------------------------------------------------------------------


@router.get("/trajectory/{goal_id}")
async def trajectory_replay(goal_id: str) -> dict[str, Any]:
    """Walk a run's trajectory into command → artifact states."""
    from server.nano_w9.trajectory import read_trajectory

    return {"status": "ok", **read_trajectory(goal_id)}


class TrajectoryRecordRequest(BaseModel):
    step: int
    command: str
    artifact_ref: str = ""
    preview_ref: str = ""
    ts: float = 0.0
    goal_id: str = ""
    session_id: str = ""
    kind: str = "file"
    media_type: str = ""


@router.post("/trajectory")
async def record_trajectory(payload: TrajectoryRecordRequest) -> dict[str, Any]:
    """Record one artifact-producing step on the existing event log."""
    from server.nano_w9.trajectory import Trajectory

    result = Trajectory().record(**payload.model_dump())
    if result.get("status") != "ok":
        raise HTTPException(status_code=500, detail=result.get("error") or {"code": "FAILED"})
    return {"status": "ok", **result}


# --------------------------------------------------------------------------
# addendum F — agent-native CLI generation (experimental, gated)
# --------------------------------------------------------------------------


class CliGenRequest(BaseModel):
    repo_ref: str
    output_dir: str = ""


@router.get("/cli-gen/phases")
async def cli_gen_phases() -> dict[str, Any]:
    """Declare the 7 phases of the agent-native template."""
    from veya.omodul.agent_native_scaffold import AGENT_NATIVE_PHASES, phase_spec

    return {
        "status": "ok",
        "experimental": True,
        "phases": [phase_spec(phase) for phase in AGENT_NATIVE_PHASES],
    }


@router.post("/cli-gen")
async def run_cli_gen(payload: CliGenRequest) -> dict[str, Any]:
    """Run the 7-phase agent-native flow (experimental)."""
    _guard("VEYA_CLI_GEN")
    from server.nano_w9.cli_gen import run_cli_gen

    _safe_path(payload.output_dir, "output_dir")
    result = await run_cli_gen(
        repo_ref=payload.repo_ref,
        output_dir=payload.output_dir or None,
    )
    return {"status": result["status"], **result}
