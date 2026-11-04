"""Event hub: declarative routing of pointer-only envelopes to bots.

Design: ~/workspace/veya-event-hub-design.md section 2. Decisions D2/D3/D5
were settled by delegation (see the design doc): rules live in
``~/.veya/event_rules.json`` (volume, survives container recreates); the
long-poll worker interface is reserved only (no route); the dedup window
is 600s and a queued follow-up waits behind the running round (never
merged); ``timeout_s`` bounds the queue wait, not the run.

The hub only routes work. It never invents delivery: tell=needed summaries
go through the existing WakeRouter (itself flag-gated), digest/silent add
nothing but the canonical event. With ``VEYA_EVENT_HUB`` off, ``ingest``
returns ``passthrough`` without touching rules, the seen store, or any
dispatch callable, so every entry point behaves exactly as before.

Runner targets are rejected at load in v1: only registered bots can be
routed to. Wiring runner dispatch needs a live runner to verify against;
refusing loudly beats a dispatch that cannot be proven.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

_log = logging.getLogger("veya.nano_w10.event_hub")

EVENT_HUB_FLAG = "VEYA_EVENT_HUB"

#: Duplicate window (D5): the same event_id seen again inside this many
#: seconds is a duplicate. Older repeats are accepted as new.
DEDUP_WINDOW_S = 600.0

#: Seen rows older than this are pruned on ingest.
SEEN_RETENTION_S = 30 * 24 * 3600.0

#: Placeholders a rule's ask template may use. Anything else is a load error.
ASK_SLOTS = ("summary", "link", "subject")

#: Tell levels a rule may carry. ``always`` is rejected at load: a rule
#: must never promote itself to the highest level.
RULE_TELLS = ("silent", "digest", "needed")

_TYPE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]+)\}")


def event_hub_enabled() -> bool:
    """Is the hub on? Default OFF; off means passthrough, never a drop."""

    try:
        from server.feature_flags import enabled as _flag_enabled

        return bool(_flag_enabled(EVENT_HUB_FLAG))
    except Exception:
        return False


def default_rules_path() -> str:
    return os.environ.get(
        "VEYA_EVENT_RULES_PATH", str(Path.home() / ".veya" / "event_rules.json")
    )


def default_seen_path() -> str:
    return os.environ.get(
        "VEYA_EVENT_HUB_DB", str(Path.home() / ".veya" / "event_hub.db")
    )


def default_skills_dir() -> Path:
    return Path(
        os.environ.get("VEYA_SKILLS_DIR", str(Path.home() / ".veya" / "skills"))
    ).expanduser()


# ── envelope ─────────────────────────────────────────────────────────────


def validate_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Validate raw input into an envelope. Raises ValueError on bad shape.

    The envelope carries pointers only: no payload body is ever accepted
    here. ``summary`` is truncated to 200 chars (it is a summary by
    construction); missing ``event_id`` is generated server-side.
    """

    if not isinstance(data, dict):
        raise ValueError("envelope must be an object")
    source = str(data.get("source") or "").strip()
    if not source or not _SOURCE_RE.match(source) or len(source) > 64:
        raise ValueError("source must be a non-empty [A-Za-z0-9_-] token")
    etype = str(data.get("type") or "").strip()
    if (
        not etype
        or not _TYPE_RE.match(etype)
        or len(etype) > 128
        or any(not seg for seg in etype.split("."))
    ):
        raise ValueError("type must be a non-empty dotted token")
    subject = str(data.get("subject") or "").strip().replace("\n", " ")
    if not subject or len(subject) > 256:
        raise ValueError("subject must be non-empty, at most 256 chars")
    summary = str(data.get("summary") or "").strip().replace("\n", " ")[:200]
    link = str(data.get("link") or "").strip().replace("\n", " ")[:2048]
    event_id = str(data.get("event_id") or "").strip()
    if event_id:
        if len(event_id) > 128 or any(ch.isspace() for ch in event_id):
            raise ValueError("event_id must be at most 128 chars, no whitespace")
    else:
        event_id = uuid.uuid4().hex
    occurred_at = str(data.get("occurred_at") or "").strip()
    if not occurred_at:
        occurred_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "event_id": event_id,
        "source": source,
        "type": etype,
        "subject": subject,
        "summary": summary,
        "link": link,
        "occurred_at": occurred_at,
        "actor": "system",
    }


# ── rules ────────────────────────────────────────────────────────────────


def _skill_exists(name: str, skills_dir: Path) -> bool:
    return (skills_dir / name / "manifest.json").is_file()


def validate_rule(
    raw: Any,
    *,
    bot_exists: Callable[[str], bool],
    skills_dir: Path,
    seen_names: set[str],
) -> dict[str, Any]:
    """Validate one rule dict. Raises ValueError naming the bad field."""

    if not isinstance(raw, dict):
        raise ValueError("rule must be an object")
    allowed = {"name", "when", "to", "skill", "ask", "group", "parallel", "tell", "timeout_s"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"rule has unknown fields: {sorted(unknown)}")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("rule name must be non-empty")
    if name in seen_names:
        raise ValueError(f"duplicate rule name: {name!r}")
    seen_names.add(name)
    when = raw.get("when")
    if not isinstance(when, dict):
        raise ValueError(f"rule {name!r}: when must be an object")
    source = str(when.get("source") or "").strip()
    if not source or not _SOURCE_RE.match(source):
        raise ValueError(f"rule {name!r}: when.source must be a token")
    types = when.get("types")
    if not isinstance(types, list) or not types or not all(
        isinstance(t, str) and t.strip() and _TYPE_RE.match(t.strip()) for t in types
    ):
        raise ValueError(f"rule {name!r}: when.types must be a non-empty list of dotted tokens")
    to = str(raw.get("to") or "").strip()
    if not to:
        raise ValueError(f"rule {name!r}: to must be a bot id")
    if not bot_exists(to):
        raise ValueError(f"rule {name!r}: unknown bot: {to!r} (runner targets are not wired in v1)")
    skill = str(raw.get("skill") or "").strip()
    if skill and not _skill_exists(skill, skills_dir):
        raise ValueError(f"rule {name!r}: unknown skill: {skill!r}")
    ask = str(raw.get("ask") or "")
    if not ask.strip():
        raise ValueError(f"rule {name!r}: ask must be non-empty")
    for slot in _PLACEHOLDER_RE.findall(ask):
        if slot not in ASK_SLOTS:
            raise ValueError(f"rule {name!r}: ask uses unknown placeholder {{{slot}}}")
    group = str(raw.get("group") or "").strip()
    parallel = raw.get("parallel", 1)
    if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
        raise ValueError(f"rule {name!r}: parallel must be an int >= 1")
    tell = str(raw.get("tell") or "silent").strip()
    if tell not in RULE_TELLS:
        raise ValueError(f"rule {name!r}: tell must be one of {RULE_TELLS} (never 'always')")
    timeout_s = raw.get("timeout_s", 1800)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise ValueError(f"rule {name!r}: timeout_s must be a number > 0")
    return {
        "name": name,
        "source": source,
        "types": [str(t).strip() for t in types],
        "to": to,
        "skill": skill,
        "ask": ask,
        "group": group,
        "parallel": parallel,
        "tell": tell,
        "timeout_s": float(timeout_s),
    }


def parse_rules_file(
    data: Any,
    *,
    bot_exists: Callable[[str], bool],
    skills_dir: Path,
) -> list[dict[str, Any]]:
    """Parse rules file content. Accepts {"rules": [...]} or one rule object."""

    if isinstance(data, dict) and "rules" in data:
        extra = set(data) - {"version", "rules"}
        if extra:
            raise ValueError(f"rules file has unknown top-level fields: {sorted(extra)}")
        if "version" in data and data["version"] != 1:
            raise ValueError("rules file version must be 1")
        raws = data["rules"]
        if not isinstance(raws, list):
            raise ValueError("rules must be a list")
    elif isinstance(data, dict):
        raws = [data]
    else:
        raise ValueError("rules file must be an object: {\"rules\": [...]} or one rule")
    seen_names: set[str] = set()
    return [
        validate_rule(raw, bot_exists=bot_exists, skills_dir=skills_dir, seen_names=seen_names)
        for raw in raws
    ]


def render_ask(
    ask: str, envelope: dict[str, Any]
) -> tuple[str, str]:
    """Split an ask template into instruction text and materials text.

    The template's fixed words stay in the instruction part; every
    placeholder becomes a pointer marker there, and the actual values
    land in the materials part (instruction/materials separation: event
    content never enters the instruction part).
    """

    values = {
        "summary": str(envelope.get("summary") or ""),
        "link": str(envelope.get("link") or ""),
        "subject": str(envelope.get("subject") or ""),
    }
    labels = {"summary": "事件摘要", "link": "事件链接", "subject": "事件对象"}

    def _marker(match: re.Match[str]) -> str:
        return f"[{labels[match.group(1)]}见资料区]"

    instruction = _PLACEHOLDER_RE.sub(_marker, ask)
    materials = "\n".join(f"{slot}: {values[slot]}" for slot in ASK_SLOTS if values[slot])
    return instruction, materials


# ── seen store ───────────────────────────────────────────────────────────


class EventSeenStore:
    """Durable idempotency: event_id -> first/last seen + count (SQLite)."""

    def __init__(self, path: str | None = None) -> None:
        self._path = str(path or default_seen_path())
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        with self._lock:
            self._conn.executescript(
                "CREATE TABLE IF NOT EXISTS event_seen ("
                " event_id TEXT PRIMARY KEY,"
                " first_seen REAL NOT NULL,"
                " last_seen REAL NOT NULL,"
                " count INTEGER NOT NULL DEFAULT 0"
                ");"
            )
            self._conn.commit()

    def record(self, event_id: str, *, now: float) -> tuple[bool, int]:
        """Record a sighting. Returns (is_duplicate, count)."""

        with self._lock:
            row = self._conn.execute(
                "SELECT last_seen, count FROM event_seen WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is not None and now - float(row[0]) < DEDUP_WINDOW_S:
                count = int(row[1]) + 1
                self._conn.execute(
                    "UPDATE event_seen SET last_seen = ?, count = ? WHERE event_id = ?",
                    (now, count, event_id),
                )
                self._conn.commit()
                return True, count
            if row is not None:
                self._conn.execute(
                    "UPDATE event_seen SET last_seen = ?, count = count + 1 WHERE event_id = ?",
                    (now, event_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO event_seen (event_id, first_seen, last_seen, count)"
                    " VALUES (?, ?, ?, 1)",
                    (event_id, now, now),
                )
            self._conn.commit()
            count = int(row[1]) + 1 if row is not None else 1
            return False, count

    def prune(self, *, now: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM event_seen WHERE last_seen < ?",
                (now - SEEN_RETENTION_S,),
            )
            self._conn.commit()
            return cur.rowcount

    def count_for(self, event_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM event_seen WHERE event_id = ?", (event_id,)
            ).fetchone()
            return int(row[0]) if row is not None else 0


# ── hub ──────────────────────────────────────────────────────────────────

DispatchFn = Callable[..., Awaitable[dict[str, Any]]]


class EventHub:
    """Route envelopes to bots: dedupe, rule match, single-flight, dispatch."""

    def __init__(
        self,
        *,
        rules_path: str | None = None,
        seen_store: EventSeenStore | None = None,
        event_store: Any | None = None,
        record_event: Callable[[str, dict[str, Any]], None] | None = None,
        bot_exists: Callable[[str], bool] | None = None,
        skills_dir: Path | None = None,
        time_fn: Callable[[], float] | None = None,
        dispatch_bot: DispatchFn | None = None,
        wake: Callable[..., Any] | None = None,
        enabled_fn: Callable[[], bool] | None = None,
    ) -> None:
        self._rules_path = str(rules_path or default_rules_path())
        self._seen = seen_store or EventSeenStore()
        self._event_store = event_store
        self._record_event_fn = record_event
        self._bot_exists = bot_exists or _default_bot_exists
        self._skills_dir = skills_dir or default_skills_dir()
        self._time_fn = time_fn or time.time
        self._dispatch_bot = dispatch_bot
        self._wake = wake
        self._enabled_fn = enabled_fn or event_hub_enabled
        self._rules: list[dict[str, Any]] = []
        self._rules_mtime: float | None = None
        self._rules_error: str | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    # ── rules loading ──────────────────────────────────────────────

    @property
    def rules_error(self) -> str | None:
        return self._rules_error

    def rules_snapshot(self) -> list[dict[str, Any]]:
        self._ensure_rules()
        return [dict(rule) for rule in self._rules]

    def _load_rules(self) -> tuple[list[dict[str, Any]], str | None]:
        path = Path(self._rules_path)
        if not path.exists():
            return [], None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [], f"rules file unreadable: {type(exc).__name__}: {exc}"
        try:
            return parse_rules_file(
                data, bot_exists=self._bot_exists, skills_dir=self._skills_dir
            ), None
        except ValueError as exc:
            return [], str(exc)

    def _ensure_rules(self) -> None:
        path = Path(self._rules_path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            # A missing file never clears loaded rules: a transient delete
            # must not flip the hub into drop-everything. First load with
            # no file simply stays at zero rules.
            if self._rules_mtime is not None:
                _log.warning(
                    "event rules file missing, keeping previous %d rules", len(self._rules)
                )
            return
        if self._rules_mtime is not None and mtime == self._rules_mtime:
            return
        rules, error = self._load_rules()
        if error is not None:
            _log.error("event rules rejected, keeping previous %d rules: %s", len(self._rules), error)
            self._rules_error = error
            self._rules_mtime = mtime
            self._record_event(
                "event.rules_rejected",
                {"path": self._rules_path, "reason": error, "kept": len(self._rules)},
            )
            return
        self._rules = rules
        self._rules_error = None
        self._rules_mtime = mtime

    def reload_rules(self) -> dict[str, Any]:
        """Force a reload. A bad or missing file keeps the previous rules."""

        if not Path(self._rules_path).exists():
            return {
                "reloaded": False,
                "rules": len(self._rules),
                "error": "rules file missing, previous rules kept",
            }
        self._rules_mtime = None
        self._ensure_rules()
        return {
            "reloaded": self._rules_error is None,
            "rules": len(self._rules),
            "error": self._rules_error,
        }

    # ── events ─────────────────────────────────────────────────────

    def _record_event(self, topic: str, payload: dict[str, Any]) -> None:
        if self._record_event_fn is not None:
            try:
                self._record_event_fn(topic, payload)
            except Exception:
                _log.debug("event-hub record hook failed", exc_info=True)
            return
        try:
            from server.events import append_canonical_event

            append_canonical_event(topic, payload)
        except Exception:
            _log.debug("event-hub canonical event failed", exc_info=True)

    # ── ingest ─────────────────────────────────────────────────────

    def _match(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        for rule in self._rules:
            if rule["source"] == envelope["source"] and envelope["type"] in rule["types"]:
                return rule
        return None

    async def ingest(
        self,
        data: dict[str, Any],
        *,
        dispatch: DispatchFn | None = None,
        user: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Route one envelope. Never raises for a bad envelope or a failed
        dispatch: refusals and failures are statuses, not exceptions."""

        if not self._enabled_fn():
            return {"status": "passthrough", "reason": "VEYA_EVENT_HUB is off"}
        try:
            envelope = validate_envelope(data)
        except ValueError as exc:
            return {"status": "refused", "reason": str(exc)}
        now = self._time_fn()
        try:
            is_dup, count = self._seen.record(envelope["event_id"], now=now)
            self._seen.prune(now=now)
        except Exception as exc:
            _log.error("event-hub seen store failed: %s", exc)
            return {"status": "unavailable", "reason": f"seen store unavailable: {type(exc).__name__}"}
        run_id = f"ev_{uuid.uuid4().hex[:12]}"
        if is_dup:
            self._record_event(
                "event.duplicate",
                {"event_id": envelope["event_id"], "count": count, "run_id": run_id},
            )
            return {"status": "duplicate", "count": count, "run_id": run_id}
        self._ensure_rules()
        rule = self._match(envelope)
        if rule is None:
            self._record_event(
                "event.dropped_no_rule",
                {
                    "event_id": envelope["event_id"],
                    "source": envelope["source"],
                    "type": envelope["type"],
                    "run_id": run_id,
                },
            )
            return {"status": "dropped_no_rule", "run_id": run_id}
        self._record_event(
            "event.ingested",
            {
                "event_id": envelope["event_id"],
                "source": envelope["source"],
                "type": envelope["type"],
                "rule": rule["name"],
                "run_id": run_id,
            },
        )
        outcome = await self._run_guarded(envelope, rule, run_id, dispatch=dispatch, user=user)
        return outcome

    async def _run_guarded(
        self,
        envelope: dict[str, Any],
        rule: dict[str, Any],
        run_id: str,
        *,
        dispatch: DispatchFn | None,
        user: dict[str, Any] | None,
    ) -> dict[str, Any]:
        deadline = self._time_fn() + rule["timeout_s"]
        lock = None
        if rule["group"]:
            lock = self._locks.setdefault(rule["group"], asyncio.Lock())
        sem = self._semaphores.setdefault(rule["name"], asyncio.Semaphore(rule["parallel"]))
        lock_held = False
        sem_held = False
        try:
            if lock is not None:
                remaining = deadline - self._time_fn()
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.wait_for(lock.acquire(), timeout=remaining)
                lock_held = True
            remaining = deadline - self._time_fn()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(sem.acquire(), timeout=remaining)
            sem_held = True
            return await self._dispatch(envelope, rule, run_id, dispatch=dispatch, user=user)
        except TimeoutError:
            self._record_event(
                "event.dropped_stale",
                {
                    "event_id": envelope["event_id"],
                    "rule": rule["name"],
                    "run_id": run_id,
                    "reason": "queued past timeout_s",
                },
            )
            return {"status": "dropped_stale", "reason": "queued past timeout_s", "run_id": run_id}
        finally:
            # Release only what this task provably acquired. A lock whose
            # acquire raced the timeout is never released here: releasing
            # a lock owned by another task would break mutual exclusion,
            # while a stuck group lock stays visible via dropped_stale
            # events and clears on restart.
            if sem_held:
                sem.release()
            if lock_held:
                lock.release()

    async def _dispatch(
        self,
        envelope: dict[str, Any],
        rule: dict[str, Any],
        run_id: str,
        *,
        dispatch: DispatchFn | None,
        user: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            if dispatch is not None:
                result = await dispatch(envelope, rule)
            elif self._dispatch_bot is not None:
                result = await self._dispatch_bot(envelope, rule, user=user)
            else:
                result = await self._default_bot_dispatch(envelope, rule, user=user)
        except Exception as exc:
            _log.warning("event-hub dispatch failed: %s", exc, exc_info=True)
            self._record_event(
                "event.dispatch_failed",
                {
                    "event_id": envelope["event_id"],
                    "rule": rule["name"],
                    "run_id": run_id,
                    "error": type(exc).__name__,
                },
            )
            return {
                "status": "failed",
                "reason": f"dispatch failed: {type(exc).__name__}",
                "run_id": run_id,
            }
        self._record_event(
            "event.dispatched",
            {
                "event_id": envelope["event_id"],
                "rule": rule["name"],
                "run_id": run_id,
                "to": rule["to"],
                "result": _short_result(result),
            },
        )
        self._record_event(
            "event.completed",
            {"event_id": envelope["event_id"], "rule": rule["name"], "run_id": run_id},
        )
        if rule["tell"] == "needed":
            self._wake_needed(envelope, rule, run_id, result, user=user)
        return {"status": "accepted", "rule": rule["name"], "run_id": run_id, "result": result}

    async def _default_bot_dispatch(
        self,
        envelope: dict[str, Any],
        rule: dict[str, Any],
        *,
        user: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if user is None:
            raise ValueError("no dispatch user: the ingest caller must supply one")
        from server.goal_run.bot_identity import BotRegistry

        spec = BotRegistry().get(rule["to"])
        if spec is None:
            raise ValueError(f"unknown bot: {rule['to']!r}")
        from server.bot_team.turns import run_bot_turn

        text, materials = render_ask(rule["ask"], envelope)
        turn = await run_bot_turn(
            bot=spec,
            text=text,
            materials=materials,
            user=dict(user),
            context=(
                f"事件 {envelope['event_id']}（{envelope['source']}/{envelope['type']}），"
                f"规则 {rule['name']}。"
                + (f" 技能：{rule['skill']}。" if rule["skill"] else "")
            ),
        )
        return {
            "task_id": turn.task_id,
            "status": str(getattr(turn.status, "value", turn.status)),
            "reply_chars": len(turn.reply or ""),
        }

    def _wake_needed(
        self,
        envelope: dict[str, Any],
        rule: dict[str, Any],
        run_id: str,
        result: dict[str, Any],
        *,
        user: dict[str, Any] | None,
    ) -> None:
        try:
            if self._wake is not None:
                self._wake(envelope, rule, run_id, result)
                return
            from server.nano_w10.wake_router import default_wake_router

            owner_ref = ""
            if user is not None:
                owner_ref = str(user.get("user_id") or "")
            if not owner_ref:
                from server.goal_run.bot_identity import BotRegistry

                spec = BotRegistry().get(rule["to"])
                owner_ref = str(getattr(spec, "owner_id", "") or rule["to"]) if spec else rule["to"]
            default_wake_router().ingest(
                owner_ref=owner_ref,
                source="event_hub",
                summary=(
                    f"事件已处理：{envelope['source']}/{envelope['type']} "
                    f"（{envelope['subject']}），规则 {rule['name']}"
                ),
                signal_id=run_id,
                novel=True,
                payload={"event_id": envelope["event_id"], "rule": rule["name"]},
            )
        except Exception:
            _log.debug("event-hub wake summary failed", exc_info=True)


def _default_bot_exists(bot_id: str) -> bool:
    try:
        from server.goal_run.bot_identity import BotRegistry

        return BotRegistry().get(bot_id) is not None
    except Exception:
        return False


def _short_result(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    return {key: str(value)[:200] for key, value in list(result.items())[:8]}


_HUB: EventHub | None = None


def get_event_hub() -> EventHub:
    """Process-wide hub (entry points share one rules cache + seen store)."""

    global _HUB
    if _HUB is None:
        _HUB = EventHub()
    return _HUB
