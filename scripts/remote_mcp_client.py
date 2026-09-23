#!/usr/bin/env python3
"""Real external MCP client (official mcp SDK) against the Remote MCP gateway.

    VEYA_REMOTE_TOKEN=<secret> venv/bin/python scripts/remote_mcp_client.py \
        --url http://127.0.0.1:8790/mcp --workspace /data/soffy/projects/veya

Runs the standard remote flow over Streamable HTTP: initialize -> tools/list ->
workspace.info -> file.read -> file.write -> test.run -> git.diff.
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
    parts = []
    for item in getattr(result, "content", []) or []:
        parts.append(getattr(item, "text", ""))
    return "\n".join(parts)


async def run(url: str, token: str, workspace: str) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    async with (
        streamablehttp_client(url, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        record("CLIENT_INITIALIZE", init.serverInfo.name != "", init.protocolVersion)

        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        record("CLIENT_TOOLS_LIST", "file.read" in names, f"{len(names)} tools")

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = await session.call_tool(name, arguments)
            text = _text(result)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"ok": False, "raw": text[:200]}

        info = await call("workspace.info", {"path": workspace})
        record("CLIENT_WORKSPACE_INFO", info.get("ok") is True)

        read = await call("file.read", {"path": "README.md"})
        record("CLIENT_FILE_READ", read.get("ok") is True and "hashline" in str(read.get("result")))

        probe = "tests/_remote_client_probe.py"
        write = await call(
            "file.write",
            {"path": probe, "content": "def test_client_probe():\n    assert True\n"},
        )
        record("CLIENT_FILE_WRITE", write.get("ok") is True)

        test = await call(
            "test.run",
            {"command": "python3 -c \"print('mcp client probe ok')\"", "wait": True},
        )
        record("CLIENT_TEST_RUN", test.get("ok") is True)

        diff = await call("git.diff", {})
        record("CLIENT_GIT_DIFF", diff.get("ok") is True and probe in str(diff.get("result")))

        status = await call("git.status", {})
        record(
            "CLIENT_ISOLATED_WORKTREE",
            ".veya/worktrees/task-remote-" in json.dumps(status),
        )

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"CLIENT E2E FAILED: {', '.join(failed)}")
        return 1
    print(f"CLIENT E2E PASSED: {len(RESULTS)} item(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default=os.environ.get("VEYA_REMOTE_MCP_URL", "http://127.0.0.1:8790/mcp")
    )
    parser.add_argument("--workspace", default="/data/soffy/projects/veya")
    args = parser.parse_args()

    token = os.environ.get("VEYA_REMOTE_TOKEN", "").strip()
    if not token:
        print("error: set VEYA_REMOTE_TOKEN", file=sys.stderr)
        return 2
    return asyncio.run(run(args.url, token, args.workspace))


if __name__ == "__main__":
    raise SystemExit(main())
