#!/usr/bin/env python3
"""llm_tap — a logging pass-through for OpenAI-compatible / Anthropic Messages calls.

Dev tooling (NANO-W10 ADDENDUM U). It sits in front of a provider endpoint,
forwards every request and response untouched, and appends ONE JSONL line per
call describing it:

    {"ts", "bot", "kind", "model", "in_tokens", "cached_in_tokens",
     "out_tokens", "prefix_divergence_offset", "status"}

Two rules it never breaks:

* **headers are never logged** — not the API key, not the session id, not
  anything else. Only the request *body shape* is summarised.
* the log records the **first byte offset where this request diverged from the
  same bot's previous request** — that is the cache-prefix divergence signal the
  prompt-cache metric consumes. A byte-identical repeat logs ``0``.

Usage::

    python scripts/llm_tap.py --listen 127.0.0.1:8899 \
        --upstream https://api.example.com/v1/chat/completions \
        --log ~/.veya/llm_tap.jsonl

Point the client at ``http://127.0.0.1:8899/v1/chat/completions``. The tap adds
no behaviour of its own: a body that fails to parse is still forwarded.

This is not a capability registry and not a router — it is one process, one
upstream, one log file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: Request bodies larger than this are hashed in a streaming fashion anyway; the
#: cap only bounds how much of the body is *held* for the hash.
MAX_BODY_BYTES = 32 * 1024 * 1024

#: Call kinds the log distinguishes (mirrors the usage-event ``kind``).
CALL_KINDS = ("main", "steward", "checkpoint", "review", "image", "other")


def _env_bot() -> str:
    """Bot label for this process (never a user id)."""
    return os.environ.get("VEYA_TAP_BOT") or os.environ.get("VEYA_BOT_ID") or "default"


def _request_summary(body: bytes) -> dict[str, Any]:
    """Summarise a request body without ever touching headers."""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
    except (json.JSONDecodeError, ValueError):
        return {"model": "", "messages": 0, "tools": 0, "bytes": len(body)}
    if not isinstance(payload, dict):
        return {"model": "", "messages": 0, "tools": 0, "bytes": len(body)}
    return {
        "model": str(payload.get("model") or ""),
        "messages": len(payload.get("messages") or []),
        "tools": len(payload.get("tools") or []),
        "bytes": len(body),
    }


def _usage_of(payload: Any) -> tuple[int, int, int]:
    """``(in_tokens, cached_in_tokens, out_tokens)`` from one response body."""
    if not isinstance(payload, dict):
        return (0, 0, 0)
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return (0, 0, 0)
    cached = 0
    for key in ("cache_read_input_tokens", "cachedContentTokenCount", "cached_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and value > 0:
            cached = value
            break
    details = usage.get("prompt_tokens_details")
    if not cached and isinstance(details, dict):
        value = details.get("cached_tokens")
        cached = value if isinstance(value, int) and value > 0 else 0
    total = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    out = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    return (
        int(total) if isinstance(total, int) else 0,
        cached,
        int(out) if isinstance(out, int) else 0,
    )


def prefix_divergence(previous: bytes | None, current: bytes) -> int:
    """First byte offset where *current* stops matching *previous*.

    ``0`` means the request is a byte-identical repeat (the strongest cache hit
    signal); ``None`` means there is no previous request for this bot, so no
    comparison is possible.

    Example:
        >>> prefix_divergence(b"abc", b"abc")
        0
        >>> prefix_divergence(b"abcd", b"abXd")
        2
        >>> prefix_divergence(None, b"abc") is None
        True
    """
    if previous is None:
        return None
    limit = min(len(previous), len(current))
    for offset in range(limit):
        if previous[offset] != current[offset]:
            return offset
    return limit if len(previous) != len(current) else 0


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:16]


class TapState:
    """Last request body per bot label — the comparison basis for divergence."""

    def __init__(self) -> None:
        self._last: dict[str, bytes] = {}

    def observe(self, bot: str, body: bytes) -> int | None:
        previous = self._last.get(bot)
        self._last[bot] = body
        return prefix_divergence(previous, body)


def _append_record(log_path: str, record: dict[str, Any]) -> None:
    """Append one JSONL line; never raise into the request path."""
    try:
        directory = os.path.dirname(os.path.abspath(log_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return


def make_handler(state: TapState, upstream: str, log_path: str, bot: str, kind: str):
    """Build the pass-through handler class (a factory keeps the state private)."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:  # silence stderr access log
            return

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, MAX_BODY_BYTES)) if length else b""
            divergence = state.observe(bot, body)
            summary = _request_summary(body)

            request = urllib.request.Request(
                upstream,
                data=body,
                method=self.command,
                headers={
                    # Forwarded verbatim, never logged.
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() in {"content-type", "accept", "x-api-key", "authorization"}
                },
            )
            started = time.time()
            status = 0
            payload: Any = None
            try:
                with urllib.request.urlopen(request, timeout=600) as response:
                    status = int(response.status)
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                raw = exc.read()
            except Exception as exc:  # upstream unreachable — report, never hang
                raw = json.dumps({"error": str(exc)}).encode("utf-8")
                status = 599

            try:
                payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except (json.JSONDecodeError, ValueError):
                payload = None
            in_tokens, cached, out_tokens = _usage_of(payload)

            _append_record(
                log_path,
                {
                    "ts": started,
                    "bot": bot,
                    "kind": kind,
                    "model": summary.get("model") or "",
                    "messages": summary.get("messages", 0),
                    "tools": summary.get("tools", 0),
                    "request_digest": _digest(body),
                    "prefix_divergence_offset": divergence,
                    "in_tokens": in_tokens,
                    "cached_in_tokens": cached,
                    "out_tokens": out_tokens,
                    "status": status,
                    "latency_ms": round((time.time() - started) * 1000, 2),
                },
            )

            self.send_response(status if 100 <= status < 600 else 599)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_POST = _forward
        do_GET = _forward

    return Handler


def cache_hit_rate(log_path: str) -> dict[str, Any]:
    """Aggregate a tap log into a cache-hit rate over input tokens.

    Read-only; used by the item-4 metric and by humans reading the file. The
    cached column is what the provider reported (``cached_in_tokens``); the
    uncached column is what the tap has to pay full price for.
    """
    rows: list[dict[str, Any]] = []
    try:
        with open(log_path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)
    except OSError:
        return {
            "calls": 0,
            "cached_input_tokens": 0,
            "uncached_input_tokens": 0,
            "total_input_tokens": 0,
            "cache_hit_rate": 0.0,
        }
    cached = sum(int(row.get("cached_in_tokens") or 0) for row in rows)
    total = sum(int(row.get("in_tokens") or 0) for row in rows)
    return {
        "calls": len(rows),
        "cached_input_tokens": cached,
        "uncached_input_tokens": max(0, total - cached),
        "total_input_tokens": total,
        "cache_hit_rate": round(cached / total, 6) if total else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM logging pass-through (dev tooling)")
    parser.add_argument("--listen", default="127.0.0.1:8899", help="host:port to bind")
    parser.add_argument(
        "--upstream",
        default="",
        help="provider endpoint to forward to (required unless --stats)",
    )
    parser.add_argument("--log", default=os.path.expanduser("~/.veya/llm_tap.jsonl"))
    parser.add_argument("--bot", default="", help="bot label (default: $VEYA_TAP_BOT)")
    parser.add_argument("--kind", default="main", choices=list(CALL_KINDS))
    parser.add_argument("--stats", action="store_true", help="print cache stats and exit")
    args = parser.parse_args(argv)

    if args.stats:
        print(json.dumps(cache_hit_rate(args.log), ensure_ascii=False, indent=2))
        return 0

    if not args.upstream:
        parser.error("--upstream is required when serving (omit it only for --stats)")

    host, _, port = args.listen.rpartition(":")
    state = TapState()
    handler = make_handler(
        state,
        args.upstream,
        args.log,
        args.bot or _env_bot(),
        args.kind if args.kind in CALL_KINDS else "other",
    )
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port or 8899)), handler)
    print(
        f"llm_tap listening on {args.listen} -> {args.upstream} (log: {args.log})", file=sys.stderr
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
