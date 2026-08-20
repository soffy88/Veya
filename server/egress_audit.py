"""Append-only hash-chain ledger for agent egress (outbound tool calls).

Records *attempts* at the tool_guard boundary (who / tool / destination / digest).
Does not replace OpenSandbox network deny; this is the Veya-side audit that
authz does not cover: data leaving via fetch_url / browser / MCP / notify.

Chain: hash = sha256(prev_hash + canonical(payload)). File is JSONL.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_LOCK = threading.Lock()
_GENESIS = "0" * 64
_SECRET_KEY = re.compile(r"(key|token|secret|password|passwd|authorization|api[_-]?key)", re.I)

OUTBOUND_TOOLS = frozenset(
    {
        "fetch_url",
        "browser_run",
        "mcp_hevi",
        "mcp_stratum",
        "mcp_codebase",
        "system_dispatch_omni_channel",
        "produce_wechat_article",
    }
)

_URL_KEYS = ("url", "href", "endpoint", "target", "webhook")


def log_path() -> Path:
    override = os.environ.get("VEYA_EGRESS_LOG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".veya" / "audit" / "egress.jsonl"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _redact(kwargs: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (kwargs or {}).items():
        if str(k).startswith("_"):
            continue
        if _SECRET_KEY.search(str(k)):
            out[str(k)] = "[redacted]"
            continue
        s = str(v)
        out[str(k)] = s[:300] + ("…" if len(s) > 300 else "")
    return out


def sanitize_destination(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        return text[:300]
    parts = urlsplit(text)
    host = parts.hostname or ""
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", "", ""))[:400]


def destination_of(tool: str, kwargs: dict[str, Any]) -> str | None:
    if tool not in OUTBOUND_TOOLS:
        return None
    for key in _URL_KEYS:
        val = kwargs.get(key)
        if val:
            return sanitize_destination(str(val))
    return f"tool:{tool}"


def digest_of(kwargs: dict[str, Any]) -> str:
    payload = _canonical(_redact(kwargs))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _last_hash_unlocked(path: Path) -> str:
    if not path.is_file():
        return _GENESIS
    last = ""
    with path.open("rb") as fh:
        for line in fh:
            if line.strip():
                last = line
    if not last:
        return _GENESIS
    try:
        rec = json.loads(last.decode("utf-8"))
        return str(rec.get("hash") or _GENESIS)
    except json.JSONDecodeError:
        return _GENESIS


def record_egress(
    *,
    tool: str,
    destination: str,
    digest: str,
    owner_id: str = "",
    source: str = "master",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "owner_id": owner_id or "",
        "tool": tool,
        "destination": destination,
        "digest": digest,
        "source": source,
    }
    if extra:
        payload["extra"] = extra
    with _LOCK:
        prev = _last_hash_unlocked(path)
        hashed = hashlib.sha256((prev + _canonical(payload)).encode("utf-8")).hexdigest()
        rec = {"payload": payload, "prev": prev, "hash": hashed}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_canonical(rec) + "\n")
    return rec


def verify_chain(path: Path | None = None) -> tuple[bool, str]:
    target = path or log_path()
    if not target.is_file():
        return True, "empty"
    prev = _GENESIS
    with target.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return False, f"line {i}: not json"
            if rec.get("prev") != prev:
                return False, f"line {i}: prev mismatch"
            payload = rec.get("payload")
            expect = hashlib.sha256((prev + _canonical(payload)).encode("utf-8")).hexdigest()
            if rec.get("hash") != expect:
                return False, f"line {i}: hash mismatch"
            prev = rec["hash"]
    return True, "ok"


def allowlist() -> set[str]:
    raw = os.environ.get("VEYA_EGRESS_ALLOWLIST", "")
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def destination_allowed(dest: str) -> bool:
    allowed = allowlist()
    if not allowed:
        return True
    needle = dest.lower()
    host = urlsplit(dest).hostname or ""
    if needle in allowed or host.lower() in allowed:
        return True
    for item in allowed:
        if host and (host == item or host.endswith("." + item)):
            return True
    return False
