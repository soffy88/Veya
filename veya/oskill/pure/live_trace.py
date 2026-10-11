"""3O-PURE — live_trace: live tool results, plan list, corrections (W4).

Three pure projections over what a run has already emitted:

* :func:`step_from_tool_event` — one UI-visible step out of a tool call /
  result / error event. The UI shows each tool call **and its result** as it
  arrives, so the result is a first-class step rather than something the
  client has to reconstruct.
* :func:`plan_rows` — the per-task plan list as ``done / doing / todo`` rows
  from the existing todo events. The plan is derived, never stored: there is
  no second plan store.
* :func:`correction_action` — what a message typed into the correction box
  while a run is in flight should do. It returns ``steer`` / ``queue`` /
  ``reject`` plus the reason, and it reuses the **item 1** vocabulary
  (``turn_priority``) rather than inventing a second one.

No IO, no clock, no ``user_id``: the caller passes the run's state.
"""

from __future__ import annotations

from typing import Any

from veya.oskill.pure.turn_priority import STATE_DELIVERED, STATE_UNCONFIRMED, classify_inbound

__all__ = [
    "PLAN_STATUSES",
    "STEP_KINDS",
    "correction_action",
    "plan_rows",
    "step_from_tool_event",
]

#: The plan vocabulary the UI renders. Exactly three buckets; anything the
#: caller reports as blocked still shows up as "doing" with its reason, so the
#: list never silently shrinks a step the user was watching.
PLAN_STATUSES: tuple[str, ...] = ("done", "doing", "todo")

#: Step kinds this projection emits. Anything else returns ``None`` and is not
#: shown — an unknown event is not worth inventing a row for.
STEP_KINDS: tuple[str, ...] = (
    "tool_call",
    "tool_result",
    "tool_error",
    "plan_update",
    "correction",
)

#: Tool results are truncated for the stream; the full result stays on the
#: task trace. 2400 chars is enough to read an answer, short enough that a
#: 50-tool run does not push megabytes down the SSE pipe.
RESULT_PREVIEW_CHARS = 2400

_ERROR_PREFIXES = ("ERROR:", "Traceback", "error:")


def step_from_tool_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Project one stream event into a UI-visible step.

    Args:
        event: the existing ``fire_step`` / ``on_step`` event dict.

    Returns:
        ``{"type", "tool_name", "detail", "status", "seq"}`` or ``None`` when
        the event is not one this UI shows.

    Example:
        >>> step_from_tool_event({"type": "tool_result", "tool_name": "read",
        ...     "result": "ok"})["type"]
        'tool_result'
    """

    kind = str(event.get("type") or event.get("event") or "")
    if kind not in STEP_KINDS:
        return None

    seq = event.get("seq")
    tool_name = str(event.get("tool_name") or event.get("tool") or "")

    if kind == "tool_result":
        return {
            "type": "tool_result",
            "tool_name": tool_name,
            "detail": _preview(event.get("result") or event.get("result_preview") or ""),
            "status": "error" if _is_error(event) else "ok",
            "seq": seq,
        }
    if kind == "tool_error":
        return {
            "type": "tool_error",
            "tool_name": tool_name,
            "detail": _preview(event.get("error") or event.get("detail") or ""),
            "status": "error",
            "seq": seq,
        }
    if kind == "tool_call":
        return {
            "type": "tool_call",
            "tool_name": tool_name,
            "detail": _args_preview(event.get("tool_args")),
            "status": "running",
            "seq": seq,
        }
    if kind == "plan_update":
        return {
            "type": "plan_update",
            "tool_name": "",
            "detail": "",
            "status": "ok",
            "rows": plan_rows(event.get("todos")),
            "seq": seq,
        }
    return {
        "type": "correction",
        "tool_name": "",
        "detail": _preview(event.get("detail") or event.get("text") or ""),
        "status": str(event.get("state") or STATE_DELIVERED),
        "seq": seq,
    }


def plan_rows(todos: Any) -> list[dict[str, str]]:
    """Normalise a todo payload into the three-bucket plan list.

    Args:
        todos: the existing ``plan_update`` todos list
            (``{"id", "title", "status"}`` with status in
            ``pending``/``in_progress``/``done``/``blocked``).

    Returns:
        ``[{"id", "title", "status"}]`` where status is ``done``/``doing``/
        ``todo``, in the order given.

    Example:
        >>> plan_rows([{"id": "a", "title": "x", "status": "done"}])[0]["status"]
        'done'
    """

    rows: list[dict[str, str]] = []
    for index, todo in enumerate(list(todos or [])):
        if not isinstance(todo, dict):
            continue
        raw = str(todo.get("status") or "pending").strip().lower()
        if raw in ("done", "completed", "complete", "succeeded"):
            status = "done"
        elif raw in ("in_progress", "running", "doing", "blocked", "error", "failed"):
            status = "doing"
        else:
            status = "todo"
        rows.append(
            {
                "id": str(todo.get("id") or f"step-{index}"),
                "title": str(todo.get("title") or todo.get("text") or ""),
                "status": status,
            }
        )
    return rows


def correction_action(
    correction: dict[str, Any],
    *,
    run_state: str,
    kind: str = "direct",
) -> dict[str, Any]:
    """What should happen to a message typed into the correction box?

    The correction box is not a second control channel: a correction typed
    while the run is live is exactly an inbound **direct user message**, so it
    goes through the item-1 vocabulary and lands as a steer at the next safe
    point.

    Args:
        correction: ``{"text": ..., "external_id": ...}``.
        run_state: ``"none"``, ``"pending"`` or ``"running"``.
        kind: inbound kind; a correction from the user is ``"direct"``.

    Returns:
        ``{"action", "state", "kind", "reason"}`` where ``action`` is
        ``steer`` / ``queue`` / ``reject``.

    Example:
        >>> correction_action({"text": "stop"}, run_state="running")["action"]
        'steer'
    """

    text = str(correction.get("text") or "").strip()
    if not text:
        return {
            "action": "reject",
            "state": STATE_UNCONFIRMED,
            "kind": classify_inbound({"kind": kind}),
            "reason": "an empty correction is not sent to the run",
        }

    state = str(run_state or "none").strip().lower()
    resolved = classify_inbound({"kind": kind})
    if state == "running" and resolved == "direct":
        return {
            "action": "steer",
            # A correction typed while the run is live is delivered into the
            # open turn; delivery is recorded, never assumed (W6 K receipts).
            "state": STATE_DELIVERED,
            "kind": resolved,
            "reason": "correction steers the running task at the next safe point",
        }
    if state == "none":
        return {
            "action": "reject",
            "state": STATE_UNCONFIRMED,
            "kind": resolved,
            "reason": "the run already finished; the correction is a new message",
        }
    return {
        "action": "queue",
        "state": STATE_UNCONFIRMED,
        "kind": resolved,
        "reason": "correction queues behind the pending turn",
    }


def _preview(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= RESULT_PREVIEW_CHARS:
        return text
    return text[:RESULT_PREVIEW_CHARS] + f"… (+{len(text) - RESULT_PREVIEW_CHARS} chars)"


def _args_preview(args: Any) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    parts = [f"{key}={str(value)[:80]}" for key, value in list(args.items())[:4]]
    return " ".join(parts)[:RESULT_PREVIEW_CHARS]


def _is_error(event: dict[str, Any]) -> bool:
    if str(event.get("status") or "").lower() == "error":
        return True
    return str(event.get("result") or event.get("result_preview") or "").startswith(_ERROR_PREFIXES)
