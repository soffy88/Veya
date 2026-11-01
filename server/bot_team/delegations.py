"""Async bot-to-bot delegation (Codync ``team`` method, adapted).

``message_bot`` (synchronous, Lorca) runs the target's turn and returns
its reply. This module is the Codync shape instead: the request is
queued, the caller continues at once, and a dispatcher runs the
target's turn through the *same* :class:`BotMessenger.message_bot` path
-- same hop limit, same spawn budget, same note in both conversations.
The reply then arrives back as a new turn of the delegating bot, so a
chain of delegations can never deadlock waiting on itself.

The queue is one JSON file under the bots root. Items move
``queued -> running -> done/failed``. The dispatcher is re-entrancy
guarded: a drain triggered inside a follow-up turn is a no-op, and
delegations queued by a follow-up turn wait for the next drain, so
chains cannot recurse unboundedly; the hop limit bounds them anyway.
A drain processes at most ``limit`` items (default 3) and resets
long-running items (older than 10 minutes) back to queued, so a crashed
drain never strands work forever.

Delegations are machine-local, like the queue file: a delegation
created during a turn forwarded to a runner drains on that runner, with
that runner's registry.
"""

from __future__ import annotations

import contextvars
import json
import time
import uuid
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json, default_bots_root

DELEGATIONS_FILE = "delegations.json"

#: A drain processes this many queued items per trigger.
DRAIN_LIMIT = 3

#: A crashing target turn is retried this many times before the
#: delegation fails and an incident attention item goes to a human.
MAX_DELEGATION_RETRIES = 2

#: A running item older than this is presumed stranded and re-queued.
STALE_AFTER_S = 600

#: Item states.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

#: Drain re-entrancy guard: a drain inside a follow-up turn is a no-op.
_draining: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "veya_delegation_draining", default=False
)


class DelegationStore:
    """The delegation queue: one JSON file, atomic writes."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else default_bots_root()

    def _path(self) -> Path:
        return self.root / DELEGATIONS_FILE

    def _load(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return payload if isinstance(payload, list) else []

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path(), items)

    def create(
        self,
        *,
        from_bot_id: str,
        to_bot_id: str,
        text: str,
        depth: int,
        reply_mode: str = "resume",
        consult: bool = False,
        done_when: str = "",
        rework: bool = False,
    ) -> dict[str, Any]:
        """Enqueue one delegation and return it."""

        item = {
            "id": f"dlg_{uuid.uuid4().hex[:16]}",
            "from_bot_id": str(from_bot_id),
            "to_bot_id": str(to_bot_id),
            "text": str(text or ""),
            "depth": int(depth),
            "status": QUEUED,
            "reply": None,
            "error": None,
            "reply_mode": str(reply_mode or "resume"),
            "consult": bool(consult),
            "done_when": str(done_when or ""),
            "rework": bool(rework),
            "retries": 0,
            "created_ts": time.time(),
            "updated_ts": time.time(),
        }
        items = self._load()
        items.append(item)
        self._save(items)
        return item

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        items = self._load()
        if status is None:
            return items
        return [item for item in items if item.get("status") == status]

    def get(self, delegation_id: str) -> dict[str, Any] | None:
        for item in self._load():
            if item.get("id") == delegation_id:
                return item
        return None

    def claim(self, *, limit: int = DRAIN_LIMIT) -> list[dict[str, Any]]:
        """Move up to *limit* queued items to running and return them."""

        items = self._load()
        claimed: list[dict[str, Any]] = []
        now = time.time()
        for item in items:
            if len(claimed) >= limit:
                break
            if item.get("status") == QUEUED:
                item["status"] = RUNNING
                item["updated_ts"] = now
                claimed.append(dict(item))
        if claimed:
            self._save(items)
        return claimed

    def _finish(
        self, delegation_id: str, *, status: str, reply: str | None, error: str | None
    ) -> dict[str, Any] | None:
        items = self._load()
        for item in items:
            if item.get("id") == delegation_id:
                if item.get("status") != RUNNING:
                    return None
                item["status"] = status
                item["reply"] = reply
                item["error"] = error
                item["updated_ts"] = time.time()
                self._save(items)
                return item
        return None

    def complete(self, delegation_id: str, *, reply: str) -> dict[str, Any] | None:
        return self._finish(delegation_id, status=DONE, reply=reply, error=None)

    def fail(self, delegation_id: str, *, error: str) -> dict[str, Any] | None:
        return self._finish(delegation_id, status=FAILED, reply=None, error=error)

    def retry(self, delegation_id: str, *, limit: int = 2) -> dict[str, Any] | None:
        """Re-queue a running item for another attempt, bounded by *limit*.

        Returns the item when re-queued, ``None`` when the retry budget is
        spent (the caller then fails it) or the item is not running.
        """

        items = self._load()
        for item in items:
            if item.get("id") == delegation_id:
                if item.get("status") != RUNNING:
                    return None
                retries = int(item.get("retries") or 0)
                if retries >= limit:
                    return None
                item["status"] = QUEUED
                item["retries"] = retries + 1
                item["updated_ts"] = time.time()
                self._save(items)
                return item
        return None

    def reset_stale(self, *, timeout_s: float = STALE_AFTER_S) -> int:
        """Re-queue running items older than *timeout_s*; returns how many."""

        items = self._load()
        now = time.time()
        reset = 0
        for item in items:
            if item.get("status") != RUNNING:
                continue
            updated = item.get("updated_ts")
            if isinstance(updated, (int, float)) and now - updated > timeout_s:
                item["status"] = QUEUED
                item["updated_ts"] = now
                reset += 1
        if reset:
            self._save(items)
        return reset


def _report_incident(
    store: DelegationStore, item: dict[str, Any], error: str
) -> None:
    """Hand an exhausted delegation to a human; never fail the drain."""

    try:
        from server.bot_team.attention import AttentionStore

        AttentionStore(store.root).create(
            question=(
                f"委托失败：{item['from_bot_id']} 交给 {item['to_bot_id']} "
                "的事连试 3 次都失败了，等你定夺。"
            ),
            bot_id=str(item["from_bot_id"]),
            kind="incident",
            context=f"delegation={item['id']} error={error}",
        )
    except Exception:
        pass


async def process_delegations(
    *,
    limit: int = DRAIN_LIMIT,
    registry: Any | None = None,
    conversations: Any | None = None,
    user: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Drain queued delegations: target turn, then a new turn of the source.

    Returns one outcome per item drained. A drain already in progress in
    this task returns ``[]`` immediately. One bad item never stops the
    drain: it is marked failed and the next item runs.
    """

    if _draining.get():
        return []
    token = _draining.set(True)
    try:
        return await _drain(
            limit=limit,
            registry=registry,
            conversations=conversations,
            user=user,
            workspace_id=workspace_id,
            root=root,
        )
    finally:
        _draining.reset(token)


async def _drain(
    *,
    limit: int,
    registry: Any | None,
    conversations: Any | None,
    user: dict[str, Any] | None,
    workspace_id: str | None,
    root: str | Path | None,
) -> list[dict[str, Any]]:
    from server.bot_team.messaging import BotMessageRefused, BotMessenger
    from server.bot_team.tool import bind_hop_depth, reset_hop_depth
    from server.bot_team.turns import run_bot_turn
    from server.goal_run.bot_identity import BotRegistry

    store = DelegationStore(root=root or getattr(conversations, "root", None))
    store.reset_stale()
    claimed = store.claim(limit=limit)
    if not claimed:
        return []
    registry = registry if registry is not None else BotRegistry()
    messenger = BotMessenger(registry, conversations=conversations)
    caller = dict(user or {"user_id": "bot-message"})
    outcomes: list[dict[str, Any]] = []
    for item in claimed:
        delegation_id = str(item["id"])
        try:
            outcome = await messenger.message_bot(
                str(item["from_bot_id"]),
                str(item["to_bot_id"]),
                str(item["text"]),
                depth=int(item["depth"]),
                user=caller,
                workspace_id=workspace_id,
                consult=bool(item.get("consult")),
                done_when=str(item.get("done_when") or ""),
                rework=bool(item.get("rework")),
            )
        except BotMessageRefused as exc:
            store.fail(delegation_id, error=str(exc))
            outcomes.append(
                {"delegation_id": delegation_id, "status": FAILED, "error": str(exc)}
            )
            continue
        except Exception as exc:  # one bad target turn never stops the drain
            error = f"{exc.__class__.__name__}: {exc}"
            retried = store.retry(delegation_id, limit=MAX_DELEGATION_RETRIES)
            if retried is not None:
                outcomes.append(
                    {
                        "delegation_id": delegation_id,
                        "status": "retrying",
                        "retries": retried["retries"],
                        "error": error,
                    }
                )
                continue
            store.fail(delegation_id, error=error)
            _report_incident(store, item, error)
            outcomes.append(
                {
                    "delegation_id": delegation_id,
                    "status": FAILED,
                    "error": error,
                }
            )
            continue
        reply = str(outcome.reply or "")
        store.complete(delegation_id, reply=reply)
        reply_mode = str(item.get("reply_mode") or "resume")
        # The reply arrives as a new turn of the delegating bot, one hop
        # deeper, so a delegation made inside it counts against the limit.
        # reply_mode "none" is fire-and-forget: the target turn ran, the
        # reply is recorded, and nothing comes back. The follow-up turn
        # runs only for the bot that delegated (source binding): there is
        # no cross-thread notice channel for a reply to leak through.
        source = registry.get(str(item["from_bot_id"]))
        if reply_mode != "none" and source is not None:
            hop_token = bind_hop_depth(int(item["depth"]) + 1)
            try:
                await run_bot_turn(
                    bot=source,
                    text=reply,
                    context=(
                        f"Reply from bot {str(item['to_bot_id'])!r} to the request "
                        f"you delegated (delegation {delegation_id}). Answer or "
                        "act on it as your one job, then stop."
                    ),
                    user=caller,
                    workspace_id=workspace_id,
                    conversations=messenger.conversations,
                )
            except Exception:
                # The delegation itself is done; a failing follow-up turn
                # must not rewrite that or break the drain.
                pass
            finally:
                reset_hop_depth(hop_token)
        outcomes.append(
            {
                "delegation_id": delegation_id,
                "status": DONE,
                "reply": reply,
                "reply_mode": reply_mode,
            }
        )
    return outcomes


__all__ = [
    "DRAIN_LIMIT",
    "DONE",
    "FAILED",
    "QUEUED",
    "RUNNING",
    "DelegationStore",
    "process_delegations",
]
