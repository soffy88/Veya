#!/usr/bin/env python3
"""§12 end-to-end: real MCP client over HTTP -> /mcp -> canonical Veya runtime.

Starts the real ``server.routes.remote_mcp`` router (the same router mounted by
``server.app``) with the real canonical executor, then drives one full remote
session over HTTP JSON-RPC:

    initialize -> bind workspace -> read source -> write controlled test file
    -> run test -> git diff -> confirm execution happened in an isolated worktree

Usage::

    venv/bin/python scripts/qualify_remote_mcp_http.py --workspace /data/soffy/projects/veya
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  ({note})" if note else ""))


def _bootstrap() -> str | None:
    try:
        from veya import platform as veya_platform

        if veya_platform.available("obase"):
            veya_platform.load("obase")
        from server.tool_registry import master_tools  # noqa: F401
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    args = parser.parse_args()

    error = _bootstrap()
    if error:
        print(f"BLOCKED: canonical Veya runtime is not importable: {error}")
        return 2

    from veya.remote.auth import RemoteAuth
    from veya.remote.models import RemotePermissions

    workspace = str(Path(args.workspace).expanduser().resolve())
    store = Path(tempfile.mkdtemp(prefix="veya-remote-http-")) / "tokens.json"
    auth = RemoteAuth(store_path=store)
    _, secret = auth.issue(
        "http-e2e",
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        workspaces=[workspace],
    )
    # The route reads credentials from the environment at first use.
    import os

    os.environ["VEYA_REMOTE_TOKENS_FILE"] = str(store)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.routes.remote_mcp as route

    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {secret}"}
    workspace_marker = ".veya/worktrees/task-remote-"
    probe = "tests/_remote_http_probe.py"

    def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers=headers,
        )
        return response.json()

    def call(session: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = rpc(
            "tools/call",
            {"name": name, "arguments": arguments, "session_id": session},
        )
        result = (body.get("result") or {}).get("structuredContent")
        if isinstance(result, dict):
            return result
        return {"ok": False, "error_code": "PROTOCOL", "message": json.dumps(body)[:200]}

    # 1. connect + bind workspace
    init = rpc("initialize", {"workspace": workspace, "clientInfo": {"name": "e2e"}})
    session = (init.get("result") or {}).get("sessionId", "")
    record("HTTP_INITIALIZE", bool(session), str(session)[:24])
    record("HTTP_WORKSPACE_BINDING", (init.get("result") or {}).get("workspace") == workspace)

    # 2. read source
    read = call(session, "file.read", {"path": "README.md"})
    record("HTTP_FILE_READ", read.get("ok") is True and "hashline" in str(read.get("result")))

    # 3. modify a controlled test file (isolated worktree)
    write = call(
        session,
        "file.write",
        {"path": probe, "content": "def test_http_probe():\n    assert True\n"},
    )
    record("HTTP_FILE_WRITE", write.get("ok") is True)

    # 4. run the test in the isolated worktree
    run = call(
        session,
        "test.run",
        {"command": "python3 -c \"print('http probe ok')\"", "wait": True},
    )
    record("HTTP_TEST_RUN", run.get("ok") is True)

    # 5. git diff of the session worktree
    diff = call(session, "git.diff", {})
    record("HTTP_GIT_DIFF", diff.get("ok") is True and probe in str(diff.get("result")))

    # 6. prove execution happened in an isolated worktree, not the bound workspace
    status = call(session, "git.status", {})
    status_text = json.dumps(status)
    record(
        "HTTP_ISOLATED_WORKTREE",
        workspace_marker in status_text,
        status_text[:240],
    )

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"HTTP E2E FAILED: {', '.join(failed)}")
        return 1
    print(f"HTTP E2E PASSED: {len(RESULTS)} item(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
