"""server.bot_team.why — turn reconstruction (read-only).

Autoloop graft, design item 2 (see ~/workspace/veya-autoloop-graft-design.md):
given one turn (task_id), rebuild its timeline from records that already
exist — the canonical event log, the approvals table, the decision ledger
and the attention store.  This module writes nothing: it only reads through
the same handles the query tools use, and anything it cannot find is
reported as a gap instead of inferred.

``explain_turn`` is shared by the HTTP route and the ``turn_why`` tool so
there is exactly one reconstruction implementation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

EVENT_LIMIT = 500
APPROVAL_LIMIT = 100
LEDGER_SCAN_LIMIT = 200
ATTENTION_LIMIT = 20

# Payload keys that must never appear in a reconstruction: tool arguments and
# anything credential-shaped.  Canonical events already carry no argument
# values (sentinel design); this is the second line of defence, not the first.
_DENY_KEYS = frozenset(
    {
        "args",
        "tool_args",
        "tool_args_json",
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "credential",
        "credentials",
        "authorization",
        "cookie",
    }
)

_MAX_STR = 300


def _safe_payload(payload: Any, *, _depth: int = 0) -> Any:
    """Copy a payload with denied keys removed and long strings capped."""
    if isinstance(payload, dict):
        return {
            key: _safe_payload(value, _depth=_depth + 1)
            for key, value in payload.items()
            if str(key).lower() not in _DENY_KEYS
        }
    if isinstance(payload, list):
        return [_safe_payload(item, _depth=_depth + 1) for item in payload[:50]]
    if isinstance(payload, str) and len(payload) > _MAX_STR:
        return payload[:_MAX_STR] + "…"
    return payload


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _timeline_entry(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return {
        "event_id": event.get("event_id"),
        "ts": event.get("ts"),
        "time": _iso(event.get("ts")),
        "topic": event.get("topic") or event.get("type") or event.get("event"),
        "actor": event.get("actor"),
        "trace_id": event.get("trace_id"),
        "payload": _safe_payload(payload),
    }


def _rule_version(timeline: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Strongest authority evidence in the timeline, else none.

    A standing match outranks a Sentinel settlement: standing rules are the
    owner's own words, Sentinel is the gate that applied them.
    """
    standing_hits = []
    sentinel_hits = []
    for entry in timeline:
        topic = str(entry.get("topic") or "")
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        if topic == "standing.refused" or topic.startswith("standing."):
            standing_hits.append(
                {
                    "topic": topic,
                    "time": entry.get("time"),
                    "standing_id": payload.get("standing_id") or payload.get("id"),
                }
            )
        elif topic == "sentinel.decision":
            sentinel_hits.append(
                {
                    "topic": topic,
                    "time": entry.get("time"),
                    "settled_by": payload.get("settled_by"),
                }
            )
    if standing_hits:
        return "standing", standing_hits
    if sentinel_hits:
        return "sentinel", sentinel_hits
    return "无", []


def explain_turn(
    task_id: str,
    *,
    session_id: str | None = None,
    event_limit: int = EVENT_LIMIT,
    event_store: Any | None = None,
    approval_store: Any | None = None,
    ledger: Any | None = None,
    attention_store: Any | None = None,
) -> dict[str, Any]:
    """Rebuild one turn from existing records.  Read-only; gaps are named.

    ``ledger`` here is the ``server.decision_ledger`` module (or anything
    exposing ``export_ledger``/``trace_decision_chain``); the other stores
    default to their production handles when not injected (tests inject
    fakes or temp-path stores).
    """
    task_id = (task_id or "").strip()
    gaps: list[str] = []

    # ── 1. timeline from the canonical event log ─────────────────────
    if event_store is None:
        from server.events import EventStore

        event_store = EventStore()
    try:
        raw_events = event_store.read_all(task_id=task_id)
    except Exception as exc:  # read failure must not mask the other sections
        raw_events = []
        gaps.append(f"事件库读取失败: {exc}")
    # read_all pre-filters on the raw line; keep only exact task_id matches.
    events = [e for e in raw_events if isinstance(e, dict) and e.get("task_id") == task_id]
    events.sort(key=lambda e: float(e.get("ts") or 0))
    truncated = len(events) > event_limit
    if truncated:
        events = events[-event_limit:]
    timeline = [_timeline_entry(e) for e in events]
    if not timeline:
        gaps.append("timeline")

    resolved_session = session_id
    trace_ids: list[str] = []
    for event in events:
        if resolved_session is None and event.get("session_id"):
            resolved_session = str(event.get("session_id"))
        trace = event.get("trace_id")
        if trace and trace not in trace_ids:
            trace_ids.append(str(trace))

    rule_name, rule_evidence = _rule_version(timeline)
    if rule_name == "无":
        gaps.append("rule_version")

    started_at = timeline[0]["time"] if timeline else None
    ended_at = timeline[-1]["time"] if timeline else None
    outcome = timeline[-1]["topic"] if timeline else "无记录"
    if not timeline:
        gaps.append("outcome")

    # ── 2. approvals for the same session ────────────────────────────
    approvals: list[dict[str, Any]] = []
    if resolved_session:
        if approval_store is None:
            from veya.obase.approvals import ApprovalStore

            approval_store = ApprovalStore()
        try:
            rows = approval_store.list_approvals(session_id=resolved_session, limit=APPROVAL_LIMIT)
        except TypeError:
            # Older store without the session filter: fall back to a
            # bounded scan rather than failing the whole report.
            rows = [
                row
                for row in approval_store.list_approvals()
                if row.get("session_id") == resolved_session
            ]
        except Exception as exc:
            rows = []
            gaps.append(f"审批表读取失败: {exc}")
        approvals = [
            {
                "request_id": row.get("request_id"),
                "tool": row.get("tool"),
                "status": row.get("status"),
                "decision": row.get("decision"),
                "created_at": row.get("created_at"),
                "resolved_at": row.get("resolved_at"),
            }
            for row in rows
        ]
    if not approvals:
        gaps.append("approvals")

    # ── 3. decision records mentioning this turn ─────────────────────
    if ledger is None:
        from server import decision_ledger as dl_mod

        ledger = dl_mod
    decision_records: list[dict[str, Any]] = []
    try:
        exported = ledger.export_ledger(limit=LEDGER_SCAN_LIMIT) or []
    except Exception as exc:
        exported = []
        gaps.append(f"决策账本读取失败: {exc}")
    needles = {task_id} | ({resolved_session} if resolved_session else set())
    for row in exported:
        if not isinstance(row, dict):
            continue
        blob = json.dumps(row, ensure_ascii=False, default=str)
        if any(needle and needle in blob for needle in needles):
            record: dict[str, Any] = {
                "id": row.get("id"),
                "category": row.get("category"),
                "scenario": row.get("scenario"),
                "outcome": row.get("outcome"),
            }
            chain: list[dict[str, Any]] = []
            if row.get("id"):
                try:
                    chain = ledger.trace_decision_chain(str(row["id"]), limit=20) or []
                except Exception:
                    chain = []
            if chain:
                record["chain"] = chain
            decision_records.append(record)
    if not decision_records:
        gaps.append("decision_records")

    # ── 4. attention items for the same matter ───────────────────────
    if attention_store is None:
        from server.bot_team.attention import AttentionStore

        attention_store = AttentionStore()
    standing_ids = {str(hit.get("standing_id")) for hit in rule_evidence if hit.get("standing_id")}
    attention: list[dict[str, Any]] = []
    try:
        items = attention_store.list() or []
    except Exception as exc:
        items = []
        gaps.append(f"attention 读取失败: {exc}")
    for item in items:
        if not isinstance(item, dict):
            continue
        text = f"{item.get('question') or ''}\n{item.get('context') or ''}"
        if item.get("standing_id") in standing_ids or any(
            needle and needle in text for needle in needles
        ):
            attention.append(
                {
                    "id": item.get("id"),
                    "question": item.get("question"),
                    "status": item.get("status"),
                    "answer": item.get("answer"),
                    "standing_id": item.get("standing_id"),
                    "created_ts": item.get("created_ts"),
                }
            )
    attention = attention[-ATTENTION_LIMIT:]
    if not attention:
        gaps.append("attention")

    return {
        "task_id": task_id,
        "session_id": resolved_session,
        "trace_ids": trace_ids,
        "started_at": started_at,
        "ended_at": ended_at,
        "outcome": outcome,
        "rule_version": rule_name,
        "rule_evidence": rule_evidence,
        "timeline": timeline,
        "timeline_truncated": truncated,
        "timeline_limit": event_limit,
        "approvals": approvals,
        "decision_records": decision_records,
        "attention": attention,
        "gaps": gaps,
    }
