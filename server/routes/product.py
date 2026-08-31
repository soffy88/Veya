"""Veya Bot product-shell endpoints.

These endpoints expose configuration/readiness metadata only.  They do not
replace the existing task, session, approval, or execution authorities.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import auth as auth_mod
from server.events import append_canonical_event
from server.product_shell import configure_bot, read_bot_state
from server.session_identity import new_session_id
from server.task_store import task_store
from veya.history_store import default_history_store

router = APIRouter(prefix="/api/v1/bot", tags=["product"])


class BotOnboardingRequest(BaseModel):
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    workspace: str | None = Field(default=None, max_length=4096)
    # Reference only: raw API keys are intentionally not accepted here.
    credential_ref: str | None = Field(default=None, min_length=1, max_length=512)


class ProductTaskRequest(BaseModel):
    """A real task entry request; ``config`` is request-scoped only."""

    objective: str = Field(..., min_length=1, max_length=20_000)
    title: str | None = Field(default=None, max_length=200)
    workspace_id: str | None = Field(default=None, max_length=512)
    provider: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    # The existing chat contract accepts ephemeral provider configuration.  It
    # is passed to MasterAgent and is never written to task/event state.
    config: dict[str, Any] = Field(default_factory=dict)


_product_tasks: set[asyncio.Task[Any]] = set()


async def _run_product_task(
    *,
    task_id: str,
    session_id: str,
    objective: str,
    provider: str | None,
    model: str | None,
    config: dict[str, Any],
    user: dict[str, Any],
) -> None:
    """Run one accepted task through the existing MasterAgent entry point."""

    # Background asyncio tasks do not run FastAPI dependency cleanup, so bind
    # the same user explicitly before MasterAgent touches history or memory.
    auth_mod.set_user(user)
    from server.coordinator_master import _active_streams, master_coordinator

    runner = asyncio.current_task()
    if runner is not None:
        _active_streams[session_id] = runner
    try:
        await master_coordinator.chat_stream(
            objective,
            session_id=session_id,
            task_id=task_id,
            config=config or None,
            provider=provider,
            model=model,
            # Product tasks are interactive: existing user_control/Workbench
            # approval is used for high-impact actions.
            require_approval=True,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - defensive process boundary
        with contextlib.suppress(Exception):
            task_store.update_status(task_id, "failed")
            task = task_store.get(task_id)
            append_canonical_event(
                "product.task_failed",
                {"error_type": type(exc).__name__},
                actor="system",
                session_id=session_id,
                trace_id=task.trace_id if task is not None else None,
                task_id=task_id,
            )
    finally:
        if runner is not None and _active_streams.get(session_id) is runner:
            _active_streams.pop(session_id, None)


def _retain_product_task(task: asyncio.Task[Any]) -> None:
    """Keep the accepted task alive until completion, then release it."""

    _product_tasks.add(task)
    task.add_done_callback(_product_tasks.discard)


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


@router.post("/tasks")
async def create_product_task(
    req: ProductTaskRequest,
    user: dict[str, Any] = Depends(auth_mod.get_current_user),
) -> dict[str, Any]:
    """Create and start a task through the canonical MasterAgent path.

    Session and Task projections are created before execution starts, so the
    caller can immediately open the existing Workbench by ``task_id``.  The
    task runner is still ``MasterCoordinator.chat_stream``; this endpoint is
    only the Layer-4 product entry adapter.
    """

    objective = req.objective.strip()
    if not objective:
        raise HTTPException(status_code=422, detail="objective must not be empty")

    session_id = new_session_id()
    trace_id = uuid.uuid4().hex
    try:
        await default_history_store().save(session_id, [], user_id=user["user_id"])
        event = append_canonical_event(
            "session.created",
            {"session_id": session_id, "entrypoint": "product_shell"},
            actor=user["user_id"],
            session_id=session_id,
            trace_id=trace_id,
        )
        task = task_store.create(
            session_id=session_id,
            title=(req.title or objective[:40]).strip() or "Veya Bot task",
            objective=objective,
            workspace_id=req.workspace_id,
            trace_id=trace_id,
        )
        append_canonical_event(
            "product.task_submitted",
            {"entrypoint": "product_shell", "session_event_id": event.get("event_id")},
            actor=user["user_id"],
            session_id=session_id,
            trace_id=trace_id,
            task_id=task.id,
        )
    except OSError as exc:
        raise HTTPException(status_code=503, detail="task state is not writable") from exc

    background = asyncio.create_task(
        _run_product_task(
            task_id=task.id,
            session_id=session_id,
            objective=objective,
            provider=req.provider,
            model=req.model,
            config=dict(req.config),
            user=dict(user),
        )
    )
    _retain_product_task(background)
    return {
        "status": "accepted",
        "task_id": task.id,
        "session_id": session_id,
        "trace_id": trace_id,
        "workbench_url": f"/workbench/{task.id}",
    }
