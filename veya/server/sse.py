"""veya.server.sse — Server-Sent Events streaming bridge (Layer 4).

Bridges ``on_step`` events emitted during ``omodul``/engine execution into the
standard SSE wire format ``data: {json}\\n\\n``.  Gracefully handles client
disconnects: on ``CancelledError`` the decision trail is persisted through
``asyncio.shield`` so a dropped connection never loses the audit log.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from veya.server.manifests import (
    assemble_agentic_loop,
    build_agentic_loop_manifest,
    load_decision_trail,
    register_running_engine,
    save_decision_trail,
    unregister_running_engine,
    validate_manifest,
)

DONE_FRAME = "data: [DONE]\n\n"


def sse_frame(payload: dict[str, Any]) -> str:
    """Escape one event dict into a standard SSE ``data:`` frame."""
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"data: {data}\n\n"


def _stamp(step: dict[str, Any]) -> dict[str, Any]:
    """Add a monotonic timestamp to a step dict (non-mutating)."""
    out = dict(step)
    out.setdefault("ts", time.time())
    return out


def _chunk_text(text: str, max_chunks: int = 4000) -> list[str]:
    """Split assistant text into word-sized chunks for progressive streaming.

    Falls back to a single frame when the token count is excessive, so a
    pathological answer cannot flood the SSE connection.
    """
    parts = [p for p in re.split(r"(\s+)", text) if p]
    if len(parts) > max_chunks or not parts:
        return [text]
    return parts


def _extract_answer(result: dict[str, Any]) -> str:
    """Pull the assistant's final text out of an engine invoke result."""
    turn = result.get("turn_result")
    if isinstance(turn, dict):
        content = turn.get("content")
        if content:
            return str(content)
        result_field = turn.get("result")
        if result_field:
            return str(result_field)
    return ""


async def _run_agent_to_queue(
    engine: Any,
    task: str,
    *,
    session_id: str,
    queue: asyncio.Queue[dict[str, Any] | None],
    trail: list[dict[str, Any]],
) -> None:
    """Execute one agentic run, streaming every on_step event to the queue.

    Emits a ``start`` step for immediate client feedback, then the engine's
    ``on_step`` events (if any), then the final answer as ``text_delta``
    frames (chunked), then ``session_done``.
    """
    engine.run()
    queue.put_nowait(_stamp({"event": "step", "step": {"action": "start", "detail": task}}))
    try:
        result = await engine.invoke({"goal": task, "session_id": session_id})
        status = result.get("status", "completed")
        cost = float(result.get("cost_usd", 0.0))

        # Stream the assistant's answer so chat UIs get actual content.
        content = _extract_answer(result)
        if content:
            for chunk in _chunk_text(content):
                queue.put_nowait(_stamp({"event": "text_delta", "delta": chunk}))
            trail.append({"event": "text", "session_id": session_id, "content": content})

        queue.put_nowait(
            _stamp(
                {
                    "event": "session_done",
                    "session_id": session_id,
                    "status": status,
                    "cost": cost,
                }
            )
        )
        trail.append(
            {
                "event": "session_done",
                "session_id": session_id,
                "status": status,
                "cost": cost,
            }
        )
    except asyncio.CancelledError:  # engine itself was cancelled
        queue.put_nowait(None)
        raise
    except Exception as exc:  # pragma: no cover - defensive
        queue.put_nowait(_stamp({"event": "error", "session_id": session_id, "error": str(exc)}))
    finally:
        queue.put_nowait(None)


async def stream_agent_run(
    task: str,
    *,
    session_id: str,
    config: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Async generator yielding SSE frames for one agentic run.

    ``POST /api/v1/agent/stream`` consumes this generator directly.

    Disconnect handling: the generator awaits ``asyncio.shield`` when the
    consumer is cancelled, so ``save_decision_trail`` completes even if the
    HTTP connection drops mid-stream.
    """
    manifest = build_agentic_loop_manifest(config)
    validate_manifest(manifest)
    engine = assemble_agentic_loop(config)
    # make the live engine steerable / inspectable via the gateway registry
    register_running_engine(session_id, engine)

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    trail: list[dict[str, Any]] = []
    run_task = asyncio.create_task(
        _run_agent_to_queue(engine, task, session_id=session_id, queue=queue, trail=trail)
    )

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield sse_frame(item)
        yield DONE_FRAME
        # Happy path: persist the collected trail so history lookup works.
        # Clearing the list prevents double-persist if a disconnect races in
        # right at the end and the CancelledError branch runs too.
        await asyncio.shield(_persist_trail(session_id, trail))
        trail.clear()
    except asyncio.CancelledError:
        # Persist whatever was collected so far — shielded from this cancellation.
        await asyncio.shield(_persist_trail(session_id, trail))
        raise
    finally:
        unregister_running_engine(session_id)
        if not run_task.done():
            run_task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await run_task


async def _persist_trail(session_id: str, trail: list[dict[str, Any]]) -> None:
    """Append the in-memory trail to the persisted decision trail (shielded)."""
    if not trail:
        return
    existing = load_decision_trail(session_id)
    existing.extend(trail)
    save_decision_trail(session_id, existing)


__all__ = ["DONE_FRAME", "sse_frame", "stream_agent_run"]
