#!/usr/bin/env python3
"""Public qualification for the Remote MCP gateway (real MCP SDK + raw HTTP).

    VEYA_REMOTE_TOKEN=$(cat ~/.veya/remote_mcp_token.secret) \
        venv/bin/python scripts/qualify_remote_mcp_public.py \
        --url https://veya.aiinote.com/mcp --workspace /data/soffy/projects/veya

Covers: health, invalid-auth fail-closed, valid auth, initialize, session id,
tools/list, workspace.info, file.read, file.write, test.run, git.diff, worktree
isolation, secret redaction.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  ({note})" if note else ""))


def _text(result: Any) -> str:
    return "\n".join(getattr(item, "text", "") for item in getattr(result, "content", []) or [])


async def _raw(url: str, token: str | None, payload: dict[str, Any], session: str | None = None):
    import httpx

    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["Mcp-Session-Id"] = session
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.post(url, headers=headers, json=payload)


async def run(url: str, token: str, workspace: str) -> int:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    health_url = url.rstrip("/") + "/health"

    # 1. health
    async with httpx.AsyncClient(timeout=30) as client:
        health = await client.get(health_url)
    body = health.json() if health.status_code == 200 else {}
    record("PUBLIC_MCP_HEALTH", health.status_code == 200 and body.get("status") == "ok")

    # 2. invalid auth fails closed (raw + SDK)
    bad = await _raw(
        url, "not-a-valid-token", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    bad_json = bad.json() if bad.status_code < 500 else {}
    bad_code = (bad_json.get("error") or {}).get("data", {}).get("error_code")
    sdk_bad_failed = False
    try:
        async with (
            streamablehttp_client(url, headers={"Authorization": "Bearer wrong"}) as (r, w, _),
            ClientSession(r, w) as s,
        ):
            await s.initialize()
    except Exception:
        sdk_bad_failed = True
    record(
        "PUBLIC_MCP_AUTH_INVALID_FAIL_CLOSED",
        bad_code == "AUTH_DENIED" and sdk_bad_failed,
    )

    # 3/4. valid auth, initialize, session id header
    headers = {"Authorization": f"Bearer {token}"}
    init = await _raw(
        url,
        token,
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"workspace": workspace}},
    )
    init_json = init.json()
    session_id = init.headers.get("mcp-session-id") or (init_json.get("result") or {}).get(
        "sessionId"
    )
    record("PUBLIC_MCP_AUTH_VALID", init.status_code == 200 and "result" in init_json)
    record("PUBLIC_MCP_INITIALIZE", bool((init_json.get("result") or {}).get("protocolVersion")))
    record("PUBLIC_MCP_SESSION_ID", bool(session_id), str(session_id)[:24])

    listing = await _raw(
        url,
        token,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        session=session_id,
    )
    tools = [t["name"] for t in ((listing.json().get("result") or {}).get("tools") or [])]
    record(
        "PUBLIC_MCP_TOOLS_LIST",
        "file.read" in tools and "veya.mission.create" in tools,
        f"{len(tools)} tools",
    )
    # Terminate the raw session so repeated runs never exhaust the session cap.
    if session_id:
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            await client.request(
                "DELETE",
                url,
                headers={"Authorization": f"Bearer {token}", "Mcp-Session-Id": session_id},
            )

    # 5+. full flow via the official SDK client
    async with (
        streamablehttp_client(url, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = await session.call_tool(name, arguments)
            text = _text(result)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"ok": False, "raw": text[:200]}

        info = await call("workspace.info", {"path": workspace})
        record("PUBLIC_MCP_WORKSPACE_INFO", info.get("ok") is True)

        read_res = await call("file.read", {"path": "README.md"})
        record(
            "PUBLIC_MCP_FILE_READ",
            read_res.get("ok") is True and "hashline" in str(read_res.get("result")),
        )

        probe = "tests/_remote_public_probe.py"
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        write = await call("file.write", {"path": probe, "content": f"TOKEN={secret}\n"})
        record("PUBLIC_MCP_FILE_WRITE", write.get("ok") is True)

        # secret redaction: read the file back; the fake credential must be masked
        reread = await call("file.read", {"path": probe})
        reread_text = str(reread.get("result"))
        record(
            "PUBLIC_MCP_SECRET_REDACTION",
            "***REDACTED***" in reread_text and secret not in reread_text,
        )

        test = await call(
            "test.run",
            {"command": "python3 -c \"print('public probe ok')\"", "wait": True},
        )
        record("PUBLIC_MCP_TEST_RUN", test.get("ok") is True)

        diff = await call("git.diff", {})
        record("PUBLIC_MCP_GIT_DIFF", diff.get("ok") is True and probe in str(diff.get("result")))

        status = await call("git.status", {})
        record(
            "PUBLIC_MCP_WORKTREE_ISOLATION",
            ".veya/worktrees/task-remote-" in json.dumps(status),
        )

        # false success: an out-of-workspace read must be ok=false
        escape = await call("file.read", {"path": "../../../etc/passwd"})
        false_success = escape.get("ok") is not False
        record("FALSE_SUCCESS_0", not false_success, f"escape ok={escape.get('ok')}")

        # duplicate side effects: a single write yields exactly one changed file
        status_text = json.dumps(status)
        record(
            "DUPLICATE_SIDE_EFFECTS_0",
            status_text.count(probe) <= 2,
            "one write == one changed file",
        )

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"PUBLIC QUALIFICATION FAILED: {', '.join(failed)}")
        return 1
    print(f"PUBLIC QUALIFICATION PASSED: {len(RESULTS)} item(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://veya.aiinote.com/mcp")
    parser.add_argument("--workspace", default="/data/soffy/projects/veya")
    args = parser.parse_args()
    token = os.environ.get("VEYA_REMOTE_TOKEN", "").strip()
    if not token:
        print("error: set VEYA_REMOTE_TOKEN", file=sys.stderr)
        return 2
    return asyncio.run(run(args.url, token, args.workspace))


if __name__ == "__main__":
    raise SystemExit(main())
