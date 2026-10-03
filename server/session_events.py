import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any

from runtime.execution.runtime import get_durable_runtime

logger = logging.getLogger("session_events")

# Event types that terminate a stream.  Producers append exactly one of these
# as the final event; consumers treat it as end-of-stream and emit `data: [DONE]`.
# This replaces the old SSEQueue.close() sentinel, which had no durable
# representation and so could not survive a restart.
TERMINAL_EVENT_TYPES = frozenset({"master_done", "task_done", "task_error"})


def split_stream_event(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Canonical producer split: (event_type, payload).

    The payload is the caller's original dict, so the pre-9876111f wire shape
    (`type` at the top level) is preserved verbatim.
    """
    if not isinstance(event, dict):
        return "message", {"value": event}
    event_type = str(event.get("type") or event.get("event") or "message")
    return event_type, event


def wire_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Durable event record -> SSE `data:` object.

    Keeps the original event fields at the top level (as the removed SSEQueue
    envelope did) and adds the durable cursor identity, so clients that read
    `type`/`delta`/`status` off the payload keep working unchanged.
    """
    out = dict(event.get("data") or {})
    out["id"] = event.get("id")
    out["epoch"] = event.get("epoch")
    out["seq"] = event.get("seq")
    if event.get("session_id") is not None:
        out.setdefault("session_id", event.get("session_id"))
    return out


def is_terminal(event: dict[str, Any]) -> bool:
    return str(event.get("event") or "") in TERMINAL_EVENT_TYPES


def lifecycle_to_session_payload(event: Any) -> dict[str, Any]:
    """Convert a LifecycleEvent to a session journal payload."""
    return {
        "lifecycle": True,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
        "causation_id": event.causation_id,
        "correlation_id": event.correlation_id,
        "durability_class": event.durability_class.value,
        "session_id": event.session_id,
        "goal_id": event.goal_id,
        "run_id": event.run_id,
        "task_id": event.task_id,
        "goal_run_id": event.goal_run_id,
        "goal_task_id": event.goal_task_id,
        "execution_id": event.execution_id,
        "agent_id": event.agent_id,
        "workspace_id": event.workspace_id,
        "context_projection_id": event.context_projection_id,
        "tool_call_id": event.tool_call_id,
        "actor": event.actor,
        "data": dict(event.payload),
    }


class _OrderedEventDrain:
    """Ordered durable ingestion for producers that cannot ``await``.

    Some producers are genuinely synchronous and cannot be awaited: event-bus
    bridges registered as plain callbacks, and the ``fire_step`` contextvar
    hook that the engine calls from deep inside sync code.  Rather than
    fire-and-forget one task per event, every such event is handed to a SINGLE
    FIFO drain task per event loop.  That preserves:

    * ordering — one consumer of one FIFO, so journal ``seq`` follows the order
      producers were invoked in;
    * error propagation — a failed append is recorded in ``failures`` and
      republished as an ``error`` event on the same session, so a client
      waiting on the stream observes the failure instead of hanging forever;
    * liveness — the task is strongly referenced, so it is never GC'd mid-drain.

    The journal remains the single authority: consumers only ever read
    ``catch_up`` / ``subscribe_live`` and never this queue.
    """

    def __init__(self) -> None:
        # One FIFO drain per event loop. Production has a single loop, so this
        # behaves as one ordered queue. Tests run a loop per test; separate
        # drains mean events can never strand in a queue whose task belonged
        # to a closed loop.
        self._drains: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue, asyncio.Task]] = {}
        self._in_flight = 0
        self.failures: list[dict[str, Any]] = []

    def submit(
        self,
        store: Any,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        epoch: int | None = None,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop means no SSE consumer can exist either. Record the
            # rejection rather than dropping the event silently.
            self._record_failure(session_id, event_type, RuntimeError("no running event loop"))
            return
        queue, _task = self._for_loop(store, loop)
        queue.put_nowait((session_id, event_type, payload, epoch))

    def _for_loop(
        self, store: Any, loop: asyncio.AbstractEventLoop
    ) -> tuple[asyncio.Queue, asyncio.Task]:
        """This loop's (queue, task), creating or replacing as needed.

        A finished task (error, cancellation, closed loop) is replaced, or
        submitted events would sit in a dead queue and an awaited publish
        would hang. Drains for dead loops are dropped.
        """
        entry = self._drains.get(id(loop))
        if entry is not None:
            _, queue, task = entry
            if not task.done():
                return queue, task
        for key in [k for k, (lp, _q, t) in self._drains.items() if lp.is_closed() or t.done()]:
            del self._drains[key]
        queue: asyncio.Queue = asyncio.Queue()
        task = loop.create_task(self._drain(store, queue))
        self._drains[id(loop)] = (loop, queue, task)
        return queue, task

    async def _drain(self, store: Any, queue: asyncio.Queue) -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            session_id, event_type, payload, epoch = item
            self._in_flight += 1
            try:
                await store.append_event(session_id, event_type, payload, epoch=epoch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_failure(session_id, event_type, exc)
                # Surface on the stream so a waiting client is not left hanging.
                with contextlib.suppress(Exception):
                    await store.append_event(
                        session_id,
                        "error",
                        {
                            "type": "error",
                            "session_id": session_id,
                            "error": f"durable append failed: {exc}",
                            "failed_event_type": event_type,
                        },
                        epoch=epoch,
                    )
            finally:
                self._in_flight -= 1

    def _record_failure(self, session_id: str, event_type: str, exc: BaseException) -> None:
        record = {
            "session_id": session_id,
            "event_type": event_type,
            "error": f"{type(exc).__name__}: {exc}",
        }
        self.failures.append(record)
        logger.error("durable session event append failed: %s", record)


class DurableSessionEventStore:
    def __init__(self):
        self._runtime = get_durable_runtime()
        self._live_subscribers: dict[str, set[asyncio.Queue[dict]]] = {}
        self._drain = _OrderedEventDrain()

    async def _ensure_started(self):
        if not getattr(self, "_migrated", False):
            if not self._runtime._started:
                if self._runtime.config.enabled:
                    await self._runtime.start()
                else:
                    await self._runtime.repository.connect()
                    await self._runtime.repository.migrate()
            self._migrated = True
            await self._runtime.start()
            await self._runtime.start()

    async def get_stream_head(self, session_id: str) -> tuple[int, int]:
        """Return (epoch, seq_head). If not found, (1, 0)"""
        await self._ensure_started()
        repo = self._runtime.repository

        def op(conn):
            row = conn.execute(
                "SELECT epoch, seq_head FROM session_streams WHERE session_id=?", (session_id,)
            ).fetchone()
            if row:
                return row["epoch"], row["seq_head"]
            return 1, 0

        if repo.backend == "sqlite":
            import typing

            return typing.cast(
                tuple[int, int], await asyncio.to_thread(lambda: repo._sqlite_read(op))
            )

        async def op_pg(conn):
            row = await conn.fetchrow(
                "SELECT epoch, seq_head FROM session_streams WHERE session_id=$1", session_id
            )
            if row:
                return row["epoch"], row["seq_head"]
            return 1, 0

        import typing

        return typing.cast(tuple[int, int], await repo._pg_tx(op_pg))

    async def begin_stream(self, session_id: str) -> int:
        """Open a NEW turn on ``session_id`` and return its epoch.

        The in-memory SSEQueue this replaced was popped from a per-session
        registry on close, so a second turn started from a clean buffer. The
        durable journal is per-session and never truncated, so turn separation
        is carried by the ``epoch`` column the schema already reserves:

        * a new turn increments the epoch and resets ``seq_head``;
        * ``catch_up(session_id, epoch, seq)`` therefore only replays the
          current turn;
        * a cursor from a previous turn is detected as stale (epoch mismatch),
          which is exactly the check ``server.sse.events_generator`` already
          performs.

        Reconnecting to a *running* turn must NOT call this.
        """
        await self._ensure_started()
        repo = self._runtime.repository
        now = time.time()

        def op(conn):
            row = conn.execute(
                "SELECT epoch FROM session_streams WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                epoch = 1
                conn.execute(
                    "INSERT INTO session_streams (session_id, epoch, seq_head, created_at, updated_at)"
                    " VALUES (?, 1, 0, ?, ?)",
                    (session_id, now, now),
                )
            else:
                epoch = row["epoch"] + 1
                conn.execute(
                    "UPDATE session_streams SET epoch=?, seq_head=0, updated_at=? WHERE session_id=?",
                    (epoch, now, session_id),
                )
            return epoch

        if repo.backend == "sqlite":
            return await asyncio.to_thread(repo._sqlite_tx, op)

        async def op_pg(conn):
            row = await conn.fetchrow(
                "SELECT epoch FROM session_streams WHERE session_id=$1", session_id
            )
            if row is None:
                epoch = 1
                await conn.execute(
                    "INSERT INTO session_streams (session_id, epoch, seq_head, created_at, updated_at)"
                    " VALUES ($1, 1, 0, $2, $3)",
                    session_id,
                    now,
                    now,
                )
            else:
                epoch = row["epoch"] + 1
                await conn.execute(
                    "UPDATE session_streams SET epoch=$1, seq_head=0, updated_at=$2 WHERE session_id=$3",
                    epoch,
                    now,
                    session_id,
                )
            return epoch

        return await repo._pg_tx(op_pg)

    async def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict,
        epoch: int | None = None,
        event_id: str | None = None,
    ) -> dict:
        """Appends to durable journal, strictly monotonic seq. Returns the event with id.

        ``epoch`` pins the event to the turn that created it. With no pin (or a
        pin that is still current) the normal CAS path runs and the stream head
        advances. A pin to a *superseded* turn appends into that turn's history
        instead: the event gets the next seq within the pinned epoch and the
        current turn's head is untouched, so a late producer can neither
        terminate the next turn nor move its cursor.
        """
        await self._ensure_started()
        repo = self._runtime.repository
        event_id = event_id or str(uuid.uuid4())
        now = time.time()

        def op(conn):
            row = conn.execute(
                "SELECT epoch, seq_head FROM session_streams WHERE session_id=?", (session_id,)
            ).fetchone()
            if epoch is not None and row is not None and row["epoch"] != epoch:
                hist = conn.execute(
                    "SELECT MAX(seq) AS m FROM session_events WHERE session_id=? AND epoch=?",
                    (session_id, epoch),
                ).fetchone()
                seq = (hist["m"] or 0) + 1
                conn.execute(
                    "INSERT INTO session_events (event_id, session_id, epoch, seq, event_type,"
                    " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event_id, session_id, epoch, seq, event_type, json.dumps(payload), now),
                )
                return epoch, seq
            if not row:
                head_epoch, seq = 1, 1
                conn.execute(
                    "INSERT INTO session_streams (session_id, epoch, seq_head, created_at, updated_at) VALUES (?, 1, 1, ?, ?)",
                    (session_id, now, now),
                )
            else:
                head_epoch, seq_head = row["epoch"], row["seq_head"]
                seq = seq_head + 1
                conn.execute(
                    "UPDATE session_streams SET seq_head=?, updated_at=? WHERE session_id=? AND epoch=?",
                    (seq, now, session_id, head_epoch),
                )
            conn.execute(
                "INSERT INTO session_events (event_id, session_id, epoch, seq, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, session_id, head_epoch, seq, event_type, json.dumps(payload), now),
            )
            return head_epoch, seq

        if repo.backend == "sqlite":
            epoch, seq = await asyncio.to_thread(repo._sqlite_tx, op)
        else:

            async def op_pg(conn):
                row = await conn.fetchrow(
                    "SELECT epoch, seq_head FROM session_streams WHERE session_id=$1", session_id
                )
                if epoch is not None and row is not None and row["epoch"] != epoch:
                    hist = await conn.fetchrow(
                        "SELECT MAX(seq) AS m FROM session_events WHERE session_id=$1 AND epoch=$2",
                        session_id,
                        epoch,
                    )
                    seq = (hist["m"] or 0) + 1
                    await conn.execute(
                        "INSERT INTO session_events (event_id, session_id, epoch, seq, event_type,"
                        " payload_json, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                        event_id,
                        session_id,
                        epoch,
                        seq,
                        event_type,
                        json.dumps(payload),
                        now,
                    )
                    return epoch, seq
                if not row:
                    head_epoch, seq = 1, 1
                    await conn.execute(
                        "INSERT INTO session_streams (session_id, epoch, seq_head, created_at, updated_at) VALUES ($1, 1, 1, $2, $3)",
                        session_id,
                        now,
                        now,
                    )
                else:
                    head_epoch, seq_head = row["epoch"], row["seq_head"]
                    seq = seq_head + 1
                    await conn.execute(
                        "UPDATE session_streams SET seq_head=$1, updated_at=$2 WHERE session_id=$3 AND epoch=$4",
                        seq,
                        now,
                        session_id,
                        head_epoch,
                    )
                await conn.execute(
                    "INSERT INTO session_events (event_id, session_id, epoch, seq, event_type, payload_json, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    event_id,
                    session_id,
                    head_epoch,
                    seq,
                    event_type,
                    json.dumps(payload),
                    now,
                )
                return head_epoch, seq

            epoch, seq = await repo._pg_tx(op_pg)

        full_event = {
            "id": f"{epoch}:{seq}",
            "event_id": event_id,
            "session_id": session_id,
            "epoch": epoch,
            "seq": seq,
            "event": event_type,
            "data": payload,
            "created_at": now,
        }

        # Broadcast to live subscribers AFTER durable persist (DURABLE_BEFORE_LIVE_DELIVERY).
        # The event is already durable, so live delivery is best-effort: a
        # subscriber bound to a dead loop is dropped, and any other queue error
        # is isolated to that subscriber. One bad subscriber must never break
        # delivery to the rest or fail the producer.
        subs = self._live_subscribers.get(session_id, set())
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        for q in list(subs):
            q_loop = getattr(q, "_loop", None)
            if loop is not None and q_loop is not None and q_loop is not loop:
                subs.discard(q)
                continue
            try:
                q.put_nowait(full_event)
            except asyncio.QueueFull:
                pass
            except Exception:
                subs.discard(q)
        return full_event

    async def catch_up(self, session_id: str, epoch: int, seq_after: int) -> list[dict]:
        """Read durable events > seq_after for the given epoch."""
        await self._ensure_started()
        repo = self._runtime.repository

        def op(conn):
            rows = conn.execute(
                "SELECT * FROM session_events WHERE session_id=? AND epoch=? AND seq > ? ORDER BY seq ASC",
                (session_id, epoch, seq_after),
            ).fetchall()
            return rows

        if repo.backend == "sqlite":
            rows = await asyncio.to_thread(lambda: repo._sqlite_read(op))
        else:

            async def op_pg(conn):
                return await conn.fetch(
                    "SELECT * FROM session_events WHERE session_id=$1 AND epoch=$2 AND seq > $3 ORDER BY seq ASC",
                    session_id,
                    epoch,
                    seq_after,
                )

            rows = await repo._pg_tx(op_pg)

        results = []
        for row in rows:
            results.append(
                {
                    "id": f"{row['epoch']}:{row['seq']}",
                    "event_id": row["event_id"],
                    "session_id": row["session_id"],
                    "epoch": row["epoch"],
                    "seq": row["seq"],
                    "event": row["event_type"],
                    "data": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
            )
        return results

    def subscribe_live(self, session_id: str) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=500)
        if session_id not in self._live_subscribers:
            self._live_subscribers[session_id] = set()
        self._live_subscribers[session_id].add(q)
        return q

    def unsubscribe_live(self, session_id: str, q: asyncio.Queue[dict]):
        if session_id in self._live_subscribers:
            self._live_subscribers[session_id].discard(q)
            if not self._live_subscribers[session_id]:
                del self._live_subscribers[session_id]

    # ------------------------------------------------------------------
    # Canonical producer entry points.
    #
    # `append_event` remains the single writer to the journal. Everything
    # below is a thin, explicit front door for the two producer shapes the
    # codebase actually has: awaited (async) producers, and synchronous
    # producers that go through the ordered drain.
    # ------------------------------------------------------------------

    async def publish(
        self,
        session_id: str,
        event: dict[str, Any],
        event_type: str | None = None,
        epoch: int | None = None,
    ) -> dict[str, Any]:
        """Awaited producer. Use this wherever `await` is available."""
        etype, payload = split_stream_event(event)
        if not payload.get("session_id"):
            payload["session_id"] = session_id
        return await self.append_event(session_id, event_type or etype, payload, epoch=epoch)

    def publish_sync(
        self,
        session_id: str,
        event: dict[str, Any],
        event_type: str | None = None,
        epoch: int | None = None,
    ) -> None:
        """Synchronous producer.

        Hands the event to the ordered drain (see `_OrderedEvent Drain`). This
        is NOT fire-and-forget: ordering is preserved, the task is tracked, and
        failures are recorded and republished as an `error` event.
        """
        etype, payload = split_stream_event(event)
        if not payload.get("session_id"):
            payload["session_id"] = session_id
        self._drain.submit(self, session_id, event_type or etype, payload, epoch)

    async def publish_terminal(
        self, session_id: str, event: dict[str, Any], epoch: int | None = None
    ) -> dict[str, Any]:
        """Append the final event for a stream.

        The old SSEQueue.close() pushed an in-memory sentinel that could not
        survive a restart. End-of-stream is now expressed as a durable
        terminal event (`master_done` / `task_done` / `task_error`), so a
        reconnecting client learns the stream ended from the journal.
        """
        if not is_terminal({"event": str(event.get("type") or "")}):
            raise ValueError(
                f"publish_terminal requires a terminal event type, got {event.get('type')!r}"
            )
        return await self.publish(session_id, event, epoch=epoch)

    async def persist_lifecycle_event(self, session_id: str, event: Any) -> dict[str, Any]:
        """Persist a LifecycleEvent to the session journal."""
        payload = lifecycle_to_session_payload(event)
        return await self.append_event(
            session_id, event.event_type, payload, event_id=event.event_id
        )

    async def replay_lifecycle_events(
        self, session_id: str, epoch: int, seq_after: int
    ) -> list[Any]:
        """Replay lifecycle events from the session journal."""
        from types import SimpleNamespace

        events = await self.catch_up(session_id, epoch, seq_after)
        result = []
        for e in events:
            data = e.get("data")
            if isinstance(data, dict) and data.get("lifecycle") is True:
                merged = {**e, **{k: v for k, v in data.items() if k != "data"}}
                result.append(SimpleNamespace(**merged))
        return result

    @property
    def drain_failures(self) -> list[dict[str, Any]]:
        """Append failures recorded by the ordered drain (observability)."""
        return self._drain.failures

    def reset_transient_state(self) -> None:
        """Drop live subscribers and per-loop drain state.

        Production runs a single event loop and never needs this. Test suites
        run a fresh loop per test while this store is a module-level singleton,
        so without an explicit reset, subscriber queues bound to a closed loop
        leak into the next test and break live delivery.
        """
        self._live_subscribers.clear()
        self._drain._drains.clear()


durable_session_store = DurableSessionEventStore()
