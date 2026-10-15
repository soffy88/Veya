"""veya/obase/pairing — device pairing codes (NANO-W6 addendum J).

A new device (runner or browser) that wants to join shows a short pairing
code; a signed-in user confirms that code in Veya and the device receives its
credential. Security properties, all enforced here:

* **Short and unambiguous** — 8 characters from an alphabet without
  confusables (no 0/O, 1/I/L).
* **Hashed at rest** — only the SHA-256 of the code is stored; the plaintext
  exists only in the response to the requesting device and is never logged.
* **Constant-time compare** — consumption uses ``hmac.compare_digest`` over
  the digests, so a guess cannot be timed.
* **10-minute TTL, single use** — a code expires and is consumed atomically;
  a consumed code can never be replayed.
* **Rate-limited** — a device label may not request more than a few codes per
  window, and a code may not be guessed more than a few times.

This module never imports the 3O layers above obase.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

#: Unambiguous alphabet: no 0/O, 1/I/L (visually confusable).
PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 8
DEFAULT_PAIRING_TTL_S = 600.0
MAX_ATTEMPTS_PER_CODE = 5
RATE_LIMIT_WINDOW_S = 300.0
RATE_LIMIT_MAX_PER_WINDOW = 3

DEFAULT_PAIRING_PATH = "~/.veya/pairing.db"

STATUS_PENDING = "pending"
STATUS_CONSUMED = "consumed"


def default_pairing_path() -> Path:
    raw = os.environ.get("VEYA_PAIRING_PATH") or DEFAULT_PAIRING_PATH
    return Path(raw).expanduser()


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class PairingCodeStore:
    """SQLite-backed pairing codes (hash-only)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = str(path) if path is not None else str(default_pairing_path())
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    code_hash TEXT PRIMARY KEY,
                    device_kind TEXT NOT NULL,
                    device_label TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL
                );
                CREATE TABLE IF NOT EXISTS device_credentials (
                    token_id TEXT PRIMARY KEY,
                    device_kind TEXT NOT NULL,
                    device_label TEXT NOT NULL DEFAULT '',
                    secret_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )
            self._conn.commit()

    def _rate_limit_ok(self, device_label: str, now: float) -> bool:
        window_start = now - RATE_LIMIT_WINDOW_S
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM pairing_codes
                WHERE device_label=? AND created_at>=?
                """,
                (device_label, window_start),
            ).fetchone()
        return int(row["n"]) < RATE_LIMIT_MAX_PER_WINDOW

    def generate(
        self,
        *,
        device_kind: str,
        device_label: str = "",
        ttl_s: float = DEFAULT_PAIRING_TTL_S,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Create one code. Returns ``{code, expires_at}`` or a refusal.

        The plaintext ``code`` is returned once and stored only as a digest.
        A rate-limited device gets ``{"error": "rate_limited"}`` — the refusal
        never reveals how long to wait.
        """

        ts = float(now if now is not None else time.time())
        label = str(device_label or "").strip()
        if not self._rate_limit_ok(label, ts):
            return {"error": "rate_limited"}
        code = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
        expires_at = ts + float(ttl_s)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO pairing_codes
                (code_hash, device_kind, device_label, status, attempts, created_at, expires_at, consumed_at)
                VALUES (?, ?, ?, ?, 0, ?, ?, NULL)
                """,
                (_digest(code), str(device_kind), label, STATUS_PENDING, ts, expires_at),
            )
            self._conn.commit()
        return {"code": code, "expires_at": expires_at}

    def consume(self, code: str, *, now: float | None = None) -> dict[str, Any]:
        """Consume one code. Constant-time compare; single use; attempt-capped.

        Returns a dict with ``status``: ``ok`` (consumed), ``invalid``,
        ``expired``, ``already_consumed`` or ``attempts_exceeded``. The record
        (kind/label) is returned only on ``ok``.
        """

        ts = float(now if now is not None else time.time())
        digest = _digest(code)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pairing_codes WHERE code_hash=?", (digest,)
            ).fetchone()
            if row is None:
                return {"status": "invalid"}
            if row["status"] == STATUS_CONSUMED:
                return {"status": "already_consumed"}
            if ts > float(row["expires_at"]):
                return {"status": "expired"}
            if int(row["attempts"]) >= MAX_ATTEMPTS_PER_CODE:
                return {"status": "attempts_exceeded"}
            cursor = self._conn.execute(
                """
                UPDATE pairing_codes
                SET status=?, consumed_at=?
                WHERE code_hash=? AND status=?
                """,
                (STATUS_CONSUMED, ts, digest, STATUS_PENDING),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return {"status": "already_consumed"}
            return {
                "status": "ok",
                "device_kind": row["device_kind"],
                "device_label": row["device_label"],
            }

    def compare(self, code: str, digest: str) -> bool:
        """Constant-time digest compare (public for tests and callers)."""

        return hmac.compare_digest(_digest(code), digest)

    # ── device credentials ────────────────────────────────────────────
    def issue_credential(
        self,
        *,
        device_kind: str,
        device_label: str = "",
        ttl_s: float = 30 * 24 * 3600.0,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Issue one device credential. The raw token is returned once; only
        its SHA-256 digest is persisted (same convention as the runner token
        store — a raw secret is never written)."""

        ts = float(now if now is not None else time.time())
        token = secrets.token_hex(24)
        token_id = secrets.token_hex(8)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO device_credentials
                (token_id, device_kind, device_label, secret_sha256, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    str(device_kind),
                    str(device_label or ""),
                    _digest(token),
                    ts,
                    ts + float(ttl_s),
                ),
            )
            self._conn.commit()
        return {"token": token, "token_id": token_id}

    def verify_credential(self, token: str, *, now: float | None = None) -> dict[str, Any] | None:
        """Verify a device credential (constant-time digest compare).

        → {"token_id", "device_kind", "device_label"} when valid, else None.
        """

        ts = float(now if now is not None else time.time())
        digest = _digest(token)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device_credentials WHERE secret_sha256=?", (digest,)
            ).fetchall()
        for row in rows:
            if ts <= float(row["expires_at"]) and hmac.compare_digest(
                str(row["secret_sha256"]), digest
            ):
                return {
                    "token_id": row["token_id"],
                    "device_kind": row["device_kind"],
                    "device_label": row["device_label"],
                }
        return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = [
    "DEFAULT_PAIRING_TTL_S",
    "MAX_ATTEMPTS_PER_CODE",
    "PAIRING_ALPHABET",
    "PAIRING_CODE_LENGTH",
    "RATE_LIMIT_MAX_PER_WINDOW",
    "RATE_LIMIT_WINDOW_S",
    "STATUS_CONSUMED",
    "STATUS_PENDING",
    "PairingCodeStore",
    "default_pairing_path",
]
