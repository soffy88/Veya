import asyncio
import contextlib
import json
import time
import uuid

from runtime.execution.runtime import get_durable_runtime


class DurableSessionEventStore:
    def __init__(self):
        self._runtime = get_durable_runtime()
        self._live_subscribers: dict[str, set[asyncio.Queue[dict]]] = {}

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

    async def append_event(self, session_id: str, event_type: str, payload: dict) -> dict:
        """Appends to durable journal, strictly monotonic seq. Returns the event with id."""
        await self._ensure_started()
        repo = self._runtime.repository
        event_id = str(uuid.uuid4())
        now = time.time()

        def op(conn):
            # CAS stream head
            row = conn.execute(
                "SELECT epoch, seq_head FROM session_streams WHERE session_id=?", (session_id,)
            ).fetchone()
            if not row:
                epoch, seq = 1, 1
                conn.execute(
                    "INSERT INTO session_streams (session_id, epoch, seq_head, created_at, updated_at) VALUES (?, 1, 1, ?, ?)",
                    (session_id, now, now),
                )
            else:
                epoch, seq_head = row["epoch"], row["seq_head"]
                seq = seq_head + 1
                conn.execute(
                    "UPDATE session_streams SET seq_head=?, updated_at=? WHERE session_id=? AND epoch=?",
                    (seq, now, session_id, epoch),
                )
            conn.execute(
                "INSERT INTO session_events (event_id, session_id, epoch, seq, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, session_id, epoch, seq, event_type, json.dumps(payload), now),
            )
            return epoch, seq

        if repo.backend == "sqlite":
            epoch, seq = await asyncio.to_thread(repo._sqlite_tx, op)
        else:

            async def op_pg(conn):
                row = await conn.fetchrow(
                    "SELECT epoch, seq_head FROM session_streams WHERE session_id=$1", session_id
                )
                if not row:
                    epoch, seq = 1, 1
                    await conn.execute(
                        "INSERT INTO session_streams (session_id, epoch, seq_head, created_at, updated_at) VALUES ($1, 1, 1, $2, $3)",
                        session_id,
                        now,
                        now,
                    )
                else:
                    epoch, seq_head = row["epoch"], row["seq_head"]
                    seq = seq_head + 1
                    await conn.execute(
                        "UPDATE session_streams SET seq_head=$1, updated_at=$2 WHERE session_id=$3 AND epoch=$4",
                        seq,
                        now,
                        session_id,
                        epoch,
                    )
                await conn.execute(
                    "INSERT INTO session_events (event_id, session_id, epoch, seq, event_type, payload_json, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    event_id,
                    session_id,
                    epoch,
                    seq,
                    event_type,
                    json.dumps(payload),
                    now,
                )
                return epoch, seq

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

        # Broadcast to live subscribers AFTER durable persist (DURABLE_BEFORE_LIVE_DELIVERY)
        subs = self._live_subscribers.get(session_id, set())
        for q in list(subs):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(full_event)
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


durable_session_store = DurableSessionEventStore()
