"""Regression contract for the deployed Veya Local workspace binding."""

from __future__ import annotations

import pytest

from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
)
from veya.remote.mcp_server import create_gateway
from veya.remote.session import RemoteSessionError
from veya.remote.workspace_policy import WorkspacePolicy, WorkspacePolicyError

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
WORKSPACE_TOOLS = {
    "workspace.list",
    "workspace.info",
    "file.read",
    "file.search",
    "file.write",
    "file.patch",
    "shell.exec",
    "test.run",
    "build.run",
    "git.status",
    "git.diff",
    "git.log",
    "worker.dispatch",
    "hicode.execute",
}


def test_mcp_schema_exposes_explicit_workspace_for_workspace_tools() -> None:
    tools = {item["name"]: item for item in RemoteToolAdapter().list_tools()}
    assert tools.keys() >= WORKSPACE_TOOLS
    for name in WORKSPACE_TOOLS:
        properties = tools[name]["inputSchema"]["properties"]
        assert properties["workspace"]["type"] == "string"


def test_jsonrpc_tools_list_exposes_explicit_workspace() -> None:
    """The outward JSON-RPC response must carry the canonical field."""

    import asyncio

    auth = RemoteAuth()
    _, secret = auth.issue("tester", permissions=PERMS, workspaces=["/tmp"])
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(default_workspace="/tmp"),
        audit=RemoteAudit(),
        adapter=RemoteToolAdapter(),
    )

    async def flow() -> dict:
        initialized = await gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "outward-schema-test"}},
            },
            authorization=f"Bearer {secret}",
        )
        assert initialized is not None
        session_id = initialized["result"]["sessionId"]
        listed = await gateway.handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            authorization=f"Bearer {secret}",
            session_header=session_id,
        )
        assert listed is not None
        return listed

    listed = asyncio.run(flow())
    tools = {item["name"]: item for item in listed["result"]["tools"]}
    assert tools.keys() >= WORKSPACE_TOOLS
    for name in WORKSPACE_TOOLS:
        assert tools[name]["inputSchema"]["properties"]["workspace"]["type"] == "string"


def test_configured_default_workspace_does_not_follow_stale_token_order(tmp_path) -> None:
    old_repo = tmp_path / "old-oprim"
    default_root = tmp_path / "projects"
    old_repo.mkdir()
    default_root.mkdir()
    manager = RemoteSessionManager(default_workspace=default_root)
    token = RemoteAuth().issue(
        "tester",
        permissions=PERMS,
        workspaces=[str(old_repo), str(default_root)],
    )[0]

    session = manager.create(token)

    assert session.active_workspace == str(default_root.resolve())
    assert session.active_workspace != str(old_repo.resolve())


def test_gateway_reads_configured_default_workspace(monkeypatch, tmp_path) -> None:
    old_repo = tmp_path / "old-oprim"
    default_root = tmp_path / "projects"
    old_repo.mkdir()
    default_root.mkdir()
    monkeypatch.setenv("VEYA_WORKSPACE_ROOT", str(default_root))
    auth = RemoteAuth()
    _, secret = auth.issue(
        "tester",
        permissions=PERMS,
        workspaces=[str(old_repo), str(default_root)],
    )
    gateway = create_gateway(
        auth=auth,
        audit=RemoteAudit(),
        adapter=RemoteToolAdapter(),
    )

    import asyncio

    response = asyncio.run(
        gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "contract-test"}},
            },
            authorization=f"Bearer {secret}",
        )
    )
    assert response is not None
    result = response.get("result")
    assert isinstance(result, dict)
    assert result["workspace"] == str(default_root.resolve())


def test_session_resume_preserves_explicit_workspace(tmp_path) -> None:
    root = tmp_path / "projects"
    child = root / "veya"
    root.mkdir()
    child.mkdir()
    token = RemoteAuth().issue("tester", permissions=PERMS, workspaces=[str(root)])[0]
    manager = RemoteSessionManager()
    session = manager.create(token)

    manager.bind_workspace(session, str(child))
    resumed = manager.reconnect(session.session_id)

    assert resumed.active_workspace == str(child.resolve())
    assert resumed.explicit_workspace == str(child.resolve())


def test_configured_root_blocks_authorized_but_outside_workspace(tmp_path) -> None:
    root = tmp_path / "projects"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    token = RemoteAuth().issue("tester", permissions=PERMS, workspaces=[str(root), str(outside)])[0]
    manager = RemoteSessionManager(default_workspace=root)
    with pytest.raises(RemoteSessionError) as exc:
        manager.create(token, workspace=str(outside))
    assert exc.value.code == "WORKSPACE_DENIED"
    session = manager.create(token)

    with pytest.raises(RemoteSessionError) as exc:
        manager.bind_workspace(session, str(outside))
    assert exc.value.code == "WORKSPACE_DENIED"


def test_mcp_initialize_resume_header_preserves_workspace(tmp_path) -> None:
    auth = RemoteAuth()
    _, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(tmp_path)])
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(),
        audit=RemoteAudit(),
        adapter=RemoteToolAdapter(),
    )

    import asyncio

    async def flow() -> tuple[dict, dict]:
        first = await gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "resume-test"}},
            },
            authorization=f"Bearer {secret}",
        )
        assert first is not None
        session_id = first["result"]["sessionId"]
        resumed = await gateway.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"clientInfo": {"name": "resume-test"}},
            },
            authorization=f"Bearer {secret}",
            session_header=session_id,
        )
        assert resumed is not None
        return first, resumed

    first, resumed = asyncio.run(flow())
    assert resumed["result"]["sessionId"] == first["result"]["sessionId"]
    assert resumed["result"]["workspace"] == first["result"]["workspace"]


def test_explicit_workspace_policy_cannot_escape_to_parent_or_system(tmp_path) -> None:
    root = tmp_path / "projects"
    child = root / "veya"
    sibling = root / "other"
    child.mkdir(parents=True)
    sibling.mkdir()
    policy = WorkspacePolicy(child, PERMS)

    for path in ("..", "../other", "/etc"):
        with pytest.raises(WorkspacePolicyError) as exc:
            policy.resolve(path)
        assert exc.value.code == "WORKSPACE_DENIED"

    escape = child / "link-to-etc"
    escape.symlink_to("/etc", target_is_directory=True)
    try:
        with pytest.raises(WorkspacePolicyError) as exc:
            policy.resolve(escape)
        assert exc.value.code == "WORKSPACE_DENIED"
    finally:
        escape.unlink()
