"""veya/obase/audit_chain — append-only, per-tenant SHA-256 audit chains.

Design adapted from block/buzz (Apache-2.0, Copyright 2026 Block, Inc.):
independent chains per tenant, tenant id bound into every digest, locked
append, and a verifier that replays the whole chain.  This is a Veya
implementation on Veya's existing SQLite substrate; no Buzz Rust code is
copied.

Chain rule (v1)::

    payload = canonical_json({tenant, seq, action, actor, object_id,
                              detail, created_at, prev_hash})
    hash    = sha256(payload).hexdigest()

``prev_hash`` is 64 zeroes for a tenant's first entry.  Sequences are monotonic
*within a tenant*; rows from another tenant can never be spliced in because
both ``tenant`` and ``seq`` are hashed.  Appends hold one process lock across
head read + insert + commit, and an ``INSERT`` unique constraint on
``(tenant, seq)`` is the cross-process fence.

Storage: an additive table in the same SQLite database as approvals
(``~/.veya/approvals.db`` or ``VEYA_APPROVALS_PATH``); callers may inject any
``sqlite3.Connection`` (e.g. ``:memory:`` in tests).  A Postgres deployment gets
the equivalent table via the additive migration in
``runtime/execution/schema.py`` — this module never changes existing tables.

Audit writes are best-effort: ``safe_append`` logs and returns ``None`` on any
failure, so an audit problem can never interrupt a user turn.  Verification is
an explicit, fail-closed operation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

__all__ = [
    "AUDIT_ACTIONS",
    "DEFAULT_TENANT",
    "GENESIS_HASH",
    "AuditChainStore",
    "audit_chain_enabled",
    "safe_append",
    "verify_chain",
]

_log = logging.getLogger("veya.audit_chain")

GENESIS_HASH = "0" * 64
DEFAULT_TENANT = "default"

# Explicit action vocabulary; the audit layer never invents an action name.
AUDIT_ACTIONS = frozenset(
    {
        "approval.requested",
        "approval.resolved",
        "risk.asked",
        "risk.allowed",
        "risk.denied",
        "tool.side_effect",
    }
)


def audit_chain_enabled() -> bool:
    """``VEYA_AUDIT_CHAIN`` gates chain writes; default ON."""

    return os.environ.get("VEYA_AUDIT_CHAIN", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "",
    }


def default_audit_chain_path() -> Path:
    """Use the existing approvals database unless explicitly overridden.

    Resolve the path locally to keep this module importable from approvals.py
    without a circular import.
    """

    raw = os.environ.get("VEYA_AUDIT_CHAIN_PATH")
    if raw:
        return Path(raw).expanduser()
    override = os.environ.get("VEYA_APPROVALS_PATH")
    return Path(override).expanduser() if override else Path("~/.veya/approvals.db").expanduser()


def _canonical_json(payload: dict[str, Any]) -> str:
    """One canonical encoding for hashing and storage (sorted, compact)."""

    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _clean(value: Any, *, limit: int = 4000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return str(text)[:limit]


class AuditChainStore:
    """Add-only hash chain store. Not a second persistence authority."""

    def __init__(self, path: str | Path | sqlite3.Connection | None = None) -> None:
        if isinstance(path, sqlite3.Connection):
            self._conn = path
            self._owns_conn = False
        else:
            target = str(path) if path is not None else str(default_audit_chain_path())
            if target != ":memory:":
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(target, check_same_thread=False, timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._owns_conn = True
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_chain (
                    tenant TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    prev_hash TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    object_id TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    PRIMARY KEY (tenant, seq)
                )
                """
            )
            self._conn.commit()

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    def head(self, tenant: str) -> dict[str, Any] | None:
        """Last entry of this tenant's chain (never another tenant's)."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM audit_chain WHERE tenant=? ORDER BY seq DESC LIMIT 1",
                (str(tenant),),
            ).fetchone()
        return dict(row) if row is not None else None

    def append(
        self,
        *,
        action: str,
        tenant: str = DEFAULT_TENANT,
        actor: str = "",
        object_id: str = "",
        detail: dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        """Append one audited event. Raises on invalid input or storage error."""

        if action not in AUDIT_ACTIONS:
            raise ValueError(f"unknown audit action: {action}")
        tid = str(tenant)
        if not tid or len(tid) > 256:
            raise ValueError("invalid audit tenant")
        safe_detail = dict(detail or {})
        stamp = float(created_at) if created_at is not None else time.time()
        # Hash the value at storage precision: SQLite keeps float timestamps as
        # IEEE doubles, so an unrounded stamp could verify differently in memory.
        stamp = float(f"{stamp:.6f}")

        with self._lock:
            head = self.head(tid)
            seq = int(head["seq"]) + 1 if head else 1
            prev_hash = str(head["hash"]) if head else GENESIS_HASH
            payload = {
                "tenant": tid,
                "seq": seq,
                "prev_hash": prev_hash,
                "action": action,
                "actor": _clean(actor),
                "object_id": _clean(object_id),
                "detail": _canonical_json(safe_detail),
                "created_at": stamp,
            }
            digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
            row = {
                **payload,
                "hash": digest,
                "detail": json.dumps(safe_detail, ensure_ascii=False, sort_keys=True),
            }
            self._conn.execute(
                """
                INSERT INTO audit_chain
                (tenant, seq, prev_hash, hash, action, actor, object_id, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["tenant"],
                    row["seq"],
                    row["prev_hash"],
                    row["hash"],
                    row["action"],
                    row["actor"],
                    row["object_id"],
                    row["detail"],
                    row["created_at"],
                ),
            )
            self._conn.commit()
            return row

    def entries(self, tenant: str, *, limit: int = 10000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_chain WHERE tenant=? ORDER BY seq ASC LIMIT ?",
                (str(tenant), max(1, min(int(limit), 100000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def verify_chain(self, tenant: str) -> dict[str, Any]:
        """Replay every stored entry of one tenant; report the first break."""

        entries = self.entries(tenant)
        if not entries:
            return {"tenant": str(tenant), "ok": True, "count": 0, "reason": "empty chain"}
        prev = GENESIS_HASH
        for index, row in enumerate(entries):
            expected_seq = index + 1
            if int(row["seq"]) != expected_seq:
                reason = f"entry {index}: seq {row['seq']} != {expected_seq}"
                return {"tenant": str(tenant), "ok": False, "count": len(entries), "reason": reason}
            if str(row["prev_hash"]) != prev:
                reason = f"entry {index}: prev_hash mismatch"
                return {"tenant": str(tenant), "ok": False, "count": len(entries), "reason": reason}
            try:
                detail = json.loads(row["detail"] or "{}")
            except (ValueError, TypeError):
                detail = {}
            payload = {
                "tenant": str(tenant),
                "seq": int(row["seq"]),
                "prev_hash": str(row["prev_hash"]),
                "action": str(row["action"]),
                "actor": str(row["actor"]),
                "object_id": str(row["object_id"]),
                "detail": _canonical_json(detail),
                "created_at": float(f"{float(row['created_at']):.6f}"),
            }
            derived = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
            if str(row["hash"]) != derived:
                reason = f"entry {index}: hash mismatch"
                return {"tenant": str(tenant), "ok": False, "count": len(entries), "reason": reason}
            prev = derived
        return {
            "tenant": str(tenant),
            "ok": True,
            "count": len(entries),
            "head": prev,
            "reason": "chain intact",
        }


_store: AuditChainStore | None = None


def get_audit_chain_store() -> AuditChainStore:
    global _store
    if _store is None:
        _store = AuditChainStore()
    return _store


def configure_audit_chain_store(store: AuditChainStore | None) -> AuditChainStore | None:
    """Bind an alternate store (tests/isolated deployments)."""

    global _store
    _store = store
    return _store


def safe_append(*, action: str, tenant: str = DEFAULT_TENANT, **fields: Any) -> dict[str, Any] | None:
    """Best-effort audit write: failures are logged, never raised into a turn."""

    if not audit_chain_enabled():
        return None
    try:
        return get_audit_chain_store().append(action=action, tenant=tenant, **fields)
    except Exception:
        _log.warning("audit chain append failed (action=%s)", action, exc_info=True)
        return None


def verify_chain(tenant: str = DEFAULT_TENANT) -> dict[str, Any]:
    """Admin verification entry point (CLI + internal callers)."""

    return get_audit_chain_store().verify_chain(tenant)
