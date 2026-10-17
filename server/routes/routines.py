import contextlib
import json
import os
import time
import uuid
from pathlib import Path

from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server import auth as auth_mod
from server.bot_team.routine_clock import _default_handoff_consumer
from server.goal_run.bot_identity import require_same_bot
from server.goal_run.models import RoutineSpec
from server.goal_run.routine_history import RoutineHistoryStore
from server.goal_run.routine_registry import RoutineRegistry


def _require_owned_bot(
    request: Request,
    bot_id: str,
    user: dict | None = Depends(auth_mod.get_current_user),
) -> None:
    """W11 D2: routines belong to the bot's owner; anyone else gets 404.

    Webhooks authenticate via HMAC signature in the handler rather than
    interactive bearer token.
    """
    if request.url.path.endswith("/webhook"):
        return

    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")

    from server.goal_run.bot_identity import BotRegistry
    from server.routes.bot_team import _require_bot

    _require_bot(BotRegistry(), bot_id, owner_id=user["user_id"])


router = APIRouter(
    prefix="/api/v1/bots/{bot_id}/routines",
    tags=["routines"],
    dependencies=[Depends(_require_owned_bot)],
)


def _get_registry(bot_id: str) -> RoutineRegistry:
    return RoutineRegistry(bot_id=bot_id)


class RoutineCreateRequest(BaseModel):
    objective: str
    schedule: str = ""
    timezone: str = "Asia/Shanghai"
    enabled: bool = True  # FIXA: default to enabled=True, respect explicit enabled=False
    routine_id: str | None = None
    probe: dict | None = None  # ORCA-W4: probe spec
    model: str | None = None  # ORCA-W4: per-routine model
    workspace_path: str | None = None  # ORCA-W4: workspace binding
    trigger_topic: str | None = None
    triggers: list[dict[str, Any]] | None = None


class RoutinePatchRequest(BaseModel):
    objective: str | None = None
    schedule: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    probe: dict | None = None  # ORCA-W4: probe spec
    model: str | None = None  # ORCA-W4: per-routine model
    workspace_path: str | None = None  # ORCA-W4: workspace binding
    trigger_topic: str | None = None
    triggers: list[dict[str, Any]] | None = None


@router.get("")
@router.get("/", include_in_schema=False)
def list_routines(bot_id: str):
    registry = _get_registry(bot_id)
    return registry.list(include_disabled=True)


@router.post("")
@router.post("/", include_in_schema=False)
def create_routine(bot_id: str, req: RoutineCreateRequest):
    registry = _get_registry(bot_id)
    rid = req.routine_id or uuid.uuid4().hex

    topic = req.trigger_topic or ("event.arrive" if req.triggers else "schedule.trigger")

    spec = RoutineSpec(
        routine_id=rid,
        trigger_topic=topic,
        objective=req.objective,
        goal_template=req.objective,
        schedule=req.schedule,
        timezone=req.timezone,
        enabled=req.enabled,
        bot_id=bot_id,
        probe=req.probe or {"kind": "none"},
        model=req.model or "",
        workspace_path=req.workspace_path or "",
        triggers=list(req.triggers or []),
    )
    registry.register(spec)
    return spec


@router.patch("/{routine_id}")
def patch_routine(bot_id: str, routine_id: str, req: RoutinePatchRequest):
    registry = _get_registry(bot_id)
    spec = registry.get(routine_id)
    if not spec:
        raise HTTPException(status_code=404, detail="Routine not found")

    require_same_bot(spec.bot_id, bot_id, f"routine:{routine_id}")

    if req.objective is not None:
        spec.objective = req.objective
        spec.goal_template = req.objective
    if req.schedule is not None:
        spec.schedule = req.schedule
    if req.timezone is not None:
        spec.timezone = req.timezone
    if req.enabled is not None:
        spec.enabled = req.enabled
    if req.probe is not None:
        spec.probe = req.probe
    if req.model is not None:
        spec.model = req.model
    if req.workspace_path is not None:
        spec.workspace_path = req.workspace_path
    if req.triggers is not None:
        spec.triggers = list(req.triggers)
    if req.trigger_topic is not None:
        spec.trigger_topic = req.trigger_topic

    registry.register(spec)  # this saves it
    return spec


@router.delete("/{routine_id}")
def delete_routine(bot_id: str, routine_id: str):
    registry = _get_registry(bot_id)
    spec = registry.get(routine_id)
    if not spec:
        raise HTTPException(status_code=404, detail="Routine not found")

    require_same_bot(spec.bot_id, bot_id, f"routine:{routine_id}")

    # RoutineRegistry doesn't have a delete method according to our grep, wait... let's check.
    # Let's remove from _items and save
    if routine_id in registry._items:
        del registry._items[routine_id]
        registry._save()
        return {"deleted": True}
    return {"deleted": False}


@router.post("/{routine_id}/run_now")
async def run_now(
    bot_id: str,
    routine_id: str,
    approval_id: str | None = None,
    user: dict = Depends(auth_mod.require_user),
):
    registry = _get_registry(bot_id)
    spec = registry.get(routine_id)
    if not spec:
        raise HTTPException(status_code=404, detail="Routine not found")

    require_same_bot(spec.bot_id, bot_id, f"routine:{routine_id}")

    # ORCA-W4: run_now works even on disabled routines.
    # Arbitrary command probes require the platform owner and a durable,
    # account-scoped, exact-arguments approval. Approval never trusts a body
    # boolean or command string supplied by the caller.
    from server.goal_run.routine_dispatch import run_probe

    probe = spec.probe or {"kind": "none"}
    command_probe_approved = False
    if probe.get("kind") == "command":
        if not auth_mod.is_owner(user):
            raise HTTPException(status_code=403, detail="command probes are owner-only")
        argv = probe.get("execute_args")
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 32
            or any(
                not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 2048
                for arg in argv
            )
            or probe.get("command") not in (None, "")
        ):
            raise HTTPException(
                status_code=400,
                detail="command probes require execute_args as a bounded string array; shell command strings are disabled",
            )
        if not spec.workspace_path:
            raise HTTPException(
                status_code=400, detail="command probes require an explicitly bound workspace"
            )
        try:
            workspace = Path(spec.workspace_path).expanduser().resolve(strict=True)
            if not workspace.is_dir():
                raise ValueError("workspace is not a directory")
            requested_cwd = Path(probe.get("cwd") or ".").expanduser()
            cwd = (
                requested_cwd if requested_cwd.is_absolute() else workspace / requested_cwd
            ).resolve(strict=True)
            if not cwd.is_relative_to(workspace) or not cwd.is_dir():
                raise ValueError("cwd escapes workspace")
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="command cwd must resolve to a directory inside the bound workspace",
            ) from exc

        from server.approvals import get_approval_service

        service = get_approval_service()
        binding = {
            "bot_id": bot_id,
            "routine_id": routine_id,
            "execute_args": argv,
            "cwd": str(cwd),
        }
        scope = service.scope_for(str(user.get("user_id") or ""))
        if not approval_id:
            request_id = uuid.uuid4().hex
            service.store.create_approval(
                {
                    "request_id": request_id,
                    "scope": scope,
                    "tool": "routine.command_probe",
                    "tool_args": binding,
                    "actor": "human",
                    "session_id": "",
                    "delegate_policy": "",
                }
            )
            return {
                "triggered": False,
                "approval_required": True,
                "approval_id": request_id,
                "message": "Resolve this exact command probe approval, then retry run_now with approval_id.",
            }
        approval = service.get(approval_id)
        if (
            not approval
            or approval.get("scope") != scope
            or approval.get("tool") != "routine.command_probe"
            or approval.get("tool_args") != binding
        ):
            raise HTTPException(status_code=404, detail="matching command probe approval not found")
        if approval.get("status") != "resolved" or approval.get("decision") not in {
            "once",
            "session",
            "always",
        }:
            raise HTTPException(status_code=403, detail="command probe approval is not approved")
        command_probe_approved = True

    probe_result = run_probe(
        probe,
        spec.workspace_path or None,
        owner_authorized=bool(auth_mod.is_owner(user)),
        approved=command_probe_approved,
    )

    # Record run history
    trigger_id = f"run_now:{uuid.uuid4().hex}"
    run_entry = {
        "trigger_id": trigger_id,
        "status": probe_result.get("status", "dispatched"),
        "probe": probe_result,
        "dispatched_at": time.time(),
    }
    if not probe_result["success"]:
        run_entry["status"] = "probe_error"
    elif probe_result["kind"] == "git_changes" and not probe_result["data"].get(
        "has_changes", False
    ):
        run_entry["status"] = "skipped"
    else:
        run_entry["status"] = "dispatched"

    with contextlib.suppress(Exception):
        RoutineHistoryStore(bot_id=bot_id).add_record(routine_id, run_entry)

    # Skip dispatch if probe skipped or errored
    if not probe_result["success"] or (
        probe_result["kind"] == "git_changes" and not probe_result["data"].get("has_changes", False)
    ):
        return {"triggered": False, "reason": run_entry["status"], "probe": probe_result}

    handoff = {
        "routine_id": routine_id,
        "trigger_topic": "schedule.trigger",
        "trigger_id": run_entry["trigger_id"],
        "target": {"goal_template": spec.objective},
        "bot_id": bot_id,
        "model": spec.model,
        "workspace_path": spec.workspace_path,
    }
    await _default_handoff_consumer([handoff])
    return {"triggered": True, "handoff": handoff}


@router.get("/{routine_id}/history")
def get_history(bot_id: str, routine_id: str, limit: int = 50):
    registry = _get_registry(bot_id)
    spec = registry.get(routine_id)
    if not spec:
        raise HTTPException(status_code=404, detail="Routine not found")

    require_same_bot(spec.bot_id, bot_id, f"routine:{routine_id}")

    try:
        history = RoutineHistoryStore(bot_id=bot_id).get_history(routine_id)
        return {"status": "ok", "routine_id": routine_id, "history": history[-limit:]}
    except Exception as exc:
        return {"status": "ok", "routine_id": routine_id, "history": []}


@router.post("/webhook")
async def routine_webhook(
    request: Request,
    bot_id: str,
):
    from server.nano_w9.flags import flag_enabled, FeatureDisabled
    if not flag_enabled("VEYA_EVENT_TRIGGERS") and not flag_enabled("VEYA_ROUTINE_TRIGGERS"):
        raise HTTPException(status_code=404, detail={"code": "FEATURE_DISABLED", "flag": "VEYA_EVENT_TRIGGERS"})

    body = await request.body()
    secret = os.environ.get("VEYA_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=401, detail="Webhook authentication is not configured")
    supplied = (
        request.headers.get("X-Veya-Signature") or request.headers.get("X-Hub-Signature-256") or ""
    ).strip()
    if not supplied:
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    from server.nano_w9.routine_triggers import verify_webhook_signature, RoutineSignalRouter
    if not verify_webhook_signature(body, secret=secret, signature=supplied):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    registry = _get_registry(bot_id)
    history_store = RoutineHistoryStore(bot_id=bot_id)

    # Monkeypatch seam: tests replace ``_default_handoff_consumer`` on
    # this module; rebind it here so the dispatch follows the patch.
    _consume = _default_handoff_consumer

    def _asyncio_iscoroutine(obj: Any) -> bool:
        import asyncio

        return asyncio.iscoroutine(obj) or isinstance(obj, asyncio.Future)

    def _run_sync_coroutine(coro: Any) -> None:
        import asyncio
        import concurrent.futures

        try:
            asyncio.get_event_loop().run_until_complete(coro)
        except RuntimeError:
            # A running loop owns this thread (the webhook handler itself is
            # async): drive the coroutine on a private loop instead.
            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(asyncio.run, coro).result()

    # A triggered webhook must run real work.  Rebuilding a throwaway
    # GoalRunState here (the previous behaviour) produced a ``goal_run_id``
    # that was never persisted or executed, so callers could never observe
    # follow-through: the status route answered 404 for it.  Handing the
    # dispatch through the SAME consumer ``run_now`` and the routine clock use
    # keeps one execution path — no second scheduler, no phantom goal.
    def dispatch_fn(topic: str, trigger_id: str, payload: dict[str, Any]):
        routines = registry.lookup(topic, bot_id=bot_id)
        handoffs = [
            {
                "routine_id": spec.routine_id,
                "trigger_topic": spec.trigger_topic,
                "trigger_id": trigger_id,
                "target": {"goal_template": spec.goal_template or spec.objective},
                "bot_id": bot_id,
                "model": spec.model,
                "workspace_path": spec.workspace_path,
            }
            for spec in routines
            if spec.enabled and spec.trigger_topic == topic
        ]
        if not handoffs:
            return {"handoffs": []}
        # ``_consume`` may be sync or async (tests inject either shape);
        # ``RoutineSignalRouter`` calls dispatch synchronously, so run the
        # coroutine to completion on a private loop when necessary.
        outcome = _consume(handoffs)
        if _asyncio_iscoroutine(outcome):
            _run_sync_coroutine(outcome)
        for spec in routines:
            history_store.add_record(
                spec.routine_id,
                {"trigger_id": trigger_id, "payload": payload, "at": time.time()},
            )
        return {"handoffs": handoffs}

    router = RoutineSignalRouter(registry=registry, dispatch=dispatch_fn)
    signal = {
        "kind": "webhook",
        "event": payload.get("event") or payload.get("action") or "webhook",
        "verified": True,
        "id": payload.get("id") or uuid.uuid4().hex,
        **payload,
    }
    result = await router.handle(signal)
    return {"status": result["status"], **result}
