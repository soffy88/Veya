"""3O-PURE — worker_brief: persistent-worker views and resume briefs (NANO-W10 W1/W2).

A delegated worker keeps its own context across tasks. That context has to be
(a) showable on the existing session/team UI, (b) budgeted against the same
window as the session, and (c) written into the ONE resume brief that compact
and handoff already produce.

All three are deterministic projections, so they live in one pure module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "WORKER_STATUSES",
    "merge_worker_briefs",
    "worker_context_budget",
    "worker_view",
]

#: The statuses a worker row can carry.
WORKER_STATUSES: tuple[str, ...] = ("idle", "running", "retired")


def _int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def worker_view(workers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project worker records into the rows the existing UI renders.

    One row per worker: ``name``, ``status``, ``last_task``, ``context_size``,
    plus the ``summary`` / ``open_items`` W2 writes into the resume brief. Those
    two travel with the row rather than being re-read from disk by the brief
    builder, so the UI and the brief can never disagree about a worker.

    Unknown statuses collapse to ``idle`` rather than surfacing a value the UI
    has never seen; retired workers are kept (the user can see what is left) but
    flagged.

    Args:
        workers: worker records from the existing registry file.

    Returns:
        Rows sorted by name.

    Example:
        >>> worker_view([{"name": "b", "status": "weird"}])[0]["status"]
        'idle'
    """
    rows: list[dict[str, Any]] = []
    for worker in workers or []:
        if not isinstance(worker, Mapping):
            continue
        status = str(worker.get("status") or "idle").strip().lower()
        if status not in WORKER_STATUSES:
            status = "idle"
        open_items = worker.get("open_items")
        if isinstance(open_items, Sequence) and not isinstance(open_items, str):
            items = [str(item).strip() for item in open_items if str(item).strip()]
        else:
            items = []
        rows.append(
            {
                "name": str(worker.get("name") or ""),
                "status": status,
                "last_task": str(worker.get("last_task") or ""),
                "context_size": _int(worker.get("context_size")),
                "summary": str(worker.get("summary") or ""),
                "open_items": items,
            }
        )
    rows.sort(key=lambda row: row["name"])
    return rows


def worker_context_budget(
    total_tokens: int,
    workers: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Split a context budget between the session and each worker.

    Workers that report no context size yet (``0``) are skipped — an empty
    worker has nothing to reserve for, and letting it claim a share would
    starve the ones that are actually carrying state. The session always keeps
    at least half the budget; the rest is split evenly, and rounding remainder
    goes to the session so the total never exceeds *total_tokens*.

    Args:
        total_tokens: tokens available across session + workers.
        workers: worker records.

    Returns:
        ``{"session": n, "<worker name>": n, ...}``.

    Example:
        >>> worker_context_budget(100, [{"name": "w", "context_size": 10}])["session"]
        50
    """
    total = max(0, _int(total_tokens))
    session = (total + 1) // 2
    active = [
        str(worker.get("name") or "")
        for worker in workers or []
        if isinstance(worker, Mapping)
        and str(worker.get("name") or "")
        and _int(worker.get("context_size")) > 0
        and str(worker.get("status") or "idle") != "retired"
    ]
    if not active:
        return {"session": total}
    share = (total - session) // len(active)
    budget: dict[str, int] = {"session": session}
    for name in active:
        budget[name] = share
    return budget


def merge_worker_briefs(
    session_brief: str,
    workers: Sequence[Mapping[str, Any]],
) -> str:
    """Fold every worker's summary and open items into one resume brief.

    W2: compact/handoff writes exactly one brief, and a fresh session must be
    able to continue the session *and* every worker. Retired workers are
    included but marked, because a retired worker with open items is exactly
    what a user needs to see after a restart.

    Args:
        session_brief: the existing session summary text.
        workers: worker records carrying ``summary`` / ``open_items``.

    Returns:
        The combined brief. With no workers it is the session brief unchanged.

    Example:
        >>> merge_worker_briefs("S", [{"name": "w", "summary": "wrote tests"}])
        'S\\n\\n## Workers\\n\\n- w: wrote tests'
    """
    base = str(session_brief or "").strip()
    rows = [
        worker
        for worker in workers or []
        if isinstance(worker, Mapping) and str(worker.get("name") or "")
    ]
    if not rows:
        return base
    lines = ["## Workers", ""]
    for worker in sorted(rows, key=lambda item: str(item.get("name") or "")):
        name = str(worker.get("name") or "")
        status = str(worker.get("status") or "idle")
        summary = str(worker.get("summary") or "").strip() or "(no summary)"
        line = f"- {name} [{status}]: {summary}"
        open_items = worker.get("open_items")
        if isinstance(open_items, Sequence) and not isinstance(open_items, str):
            items = [str(item).strip() for item in open_items if str(item).strip()]
            if items:
                line += " — open: " + "; ".join(items)
        lines.append(line)
    tail = "\n".join(lines)
    return f"{base}\n\n{tail}" if base else tail
