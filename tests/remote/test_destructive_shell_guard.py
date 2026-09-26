"""L0 capability classification hotfix regression: destructive shell guard.

Guards the boundary between generic ``shell.argv`` AUTO_OPEN and the
human-gated destructive shell surface. Reuses
``veya/remote/workspace_policy.py::classify_destructive`` (no second
classifier).

Invariants:
* DESTRUCTIVE_SHELL_AUTO_OPEN=0
* GENERIC_SHELL_SAFE_AUTO_OPEN=PASS
* DESTRUCTIVE_APPROVAL_BYPASS=0
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from veya.remote.action_gateway import ActionCategory, classify_action
from veya.remote.models import RemotePermissions, RemoteSession


def _session(path: Path, *, destructive: bool = False) -> RemoteSession:
    now = time.time()
    return RemoteSession(
        session_id="rs-destructive-guard",
        principal="chatgpt-web",
        token_id="rt-destructive-guard",
        workspaces=(str(path.resolve()),),
        active_workspace=str(path.resolve()),
        permissions=RemotePermissions(
            read=True,
            write=True,
            shell=True,
            git=True,
            destructive=destructive,
        ),
        created_at=now,
        expires_at=now + 3600,
    )


# DESTRUCTIVE_SHELL_AUTO_OPEN=0
@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /",
        "rm -rf build",
        "dd if=/dev/zero of=/dev/sda",
        "kill -9 1",
        "shred -u secret.txt",
        "truncate -s 0 data.db",
    ],
)
def test_destructive_shell_is_human_gated(tmp_path: Path, cmd: str) -> None:
    cl = classify_action("shell.exec", {"command": cmd}, _session(tmp_path), str(tmp_path))
    assert cl.category == ActionCategory.HUMAN_GATED
    assert cl.requires_approval is True
    assert cl.capability_id == "privileged.destructive_shell"


# GENERIC_SHELL_SAFE_AUTO_OPEN=PASS
@pytest.mark.parametrize(
    "cmd",
    [
        "python3 script.py",
        "pytest -q",
        "git status",
        "rg foo",
        "cat file.txt",
        "mkdir -p src",
        "curl https://example.com",
    ],
)
def test_generic_shell_safe_auto_open(tmp_path: Path, cmd: str) -> None:
    cl = classify_action("shell.exec", {"command": cmd}, _session(tmp_path), str(tmp_path))
    assert cl.category == ActionCategory.AUTO_OPEN
    assert cl.requires_approval is False
    if cmd != "git status":
        assert cl.capability_id == "shell.argv"


# DESTRUCTIVE_APPROVAL_BYPASS=0 / test_destructive_command_guard
async def test_gateway_blocks_destructive_shell_without_capability(tmp_path: Path) -> None:
    from veya.remote import (
        RemoteAudit,
        RemoteAuth,
        RemoteSessionManager,
        RemoteToolAdapter,
    )
    from veya.remote.mcp_server import create_gateway

    auth = RemoteAuth()
    _record, secret = auth.issue(
        "tester",
        permissions=RemotePermissions(read=True, write=True, shell=True, git=True),
        workspaces=[str(tmp_path)],
    )
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(None, redact=audit.redact)
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=4),
        audit=audit,
        adapter=adapter,
    )
    init = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = init["result"]["sessionId"]

    async def call(arguments: dict[str, Any]) -> dict[str, Any]:
        response = await gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "shell.exec", "arguments": arguments},
            },
            authorization=f"Bearer {secret}",
            session_header=session_id,
        )
        return response["result"]["structuredContent"]

    plain = await call({"command": "rm -rf build"})
    assert plain["ok"] is False
    assert plain["error_code"] == "POLICY_BLOCKED"

    boolean_bypass = await call({"command": "rm -rf build", "approved": True})
    assert boolean_bypass["ok"] is False
    assert boolean_bypass["error_code"] == "POLICY_BLOCKED"


async def test_destructive_shell_requires_approval_even_with_capability(tmp_path: Path) -> None:
    from veya.remote import (
        RemoteAudit,
        RemoteAuth,
        RemoteSessionManager,
        RemoteToolAdapter,
    )
    from veya.remote.mcp_server import create_gateway

    auth = RemoteAuth()
    _record, secret = auth.issue(
        "tester",
        permissions=RemotePermissions(
            read=True, write=True, shell=True, git=True, destructive=True
        ),
        workspaces=[str(tmp_path)],
    )
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(None, redact=audit.redact)
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=4),
        audit=audit,
        adapter=adapter,
    )
    init = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    session_id = init["result"]["sessionId"]
    response = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "shell.exec",
                "arguments": {"command": "rm -rf build", "approved": True},
            },
        },
        authorization=f"Bearer {secret}",
        session_header=session_id,
    )
    structured = response["result"]["structuredContent"]
    assert structured["ok"] is False
    assert structured["error_code"] == "INVALID_APPROVAL"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(test_gateway_blocks_destructive_shell_without_capability(Path("/tmp"))))
