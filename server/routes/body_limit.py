"""Shared request-body size limit for external entry points.

Autoloop graft (1.1.1 remainder): upstream caps request bodies at 8MB.
veya entry points used to read bodies fully with no limit, so one oversized
webhook could exhaust memory before any signature or routing check.

Order of checks at each entry: size limit first, then signature, then
parse/dispatch. Oversized bodies never reach signature verification or
business logic, and no event is persisted for them.

The helper preserves the raw body on the request (``request._body``) so a
later ``await request.body()`` — the Slack signature path re-reads it —
still returns the same bytes.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

#: Maximum request body accepted at a guarded entry point (8 MiB).
MAX_BODY_BYTES = 8 * 1024 * 1024


async def read_limited_body(request: Request, *, limit: int = MAX_BODY_BYTES) -> bytes:
    """Read the request body, refusing anything over ``limit`` with 413.

    A declared ``content-length`` over the limit is rejected before any
    byte is read; otherwise the stream is read in chunks and refused as
    soon as the limit is crossed.
    """

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"request body too large (limit {limit} bytes)",
                )
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"request body too large (limit {limit} bytes)",
            )
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body
    return body
