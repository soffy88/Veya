"""W17 group B — canonical tools with host parameter (B-group original plan)."""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from server.remote_control import approval as approval_mod
from server.remote_control import hosts as hosts_mod
from server.remote_control import identity as identity_mod
from server.remote_control import ssh_tools, tools
from server.tool_registry import (
    _tool_grep,
    _tool_list_files,
    _tool_read_file_ast,
    _tool_read_hashline,
    _tool_run_in_sandbox,
    _tool_write_file,
    master_tools,
)
from tests.test_w17_ssh_tools import FakeApprovals
from tests.w17_fakes import LocalExecRunner


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "1")
    monkeypatch.setenv("VEYA_SSH_HOSTS", "1")
    monkeypatch.setenv("VEYA_REMOTE_CONTROL_HOSTS_PATH", str(tmp_path / "hosts.json"))
    monkeypatch.setenv("VEYA_REMOTE_CONTROL_POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setenv("VEYA_SSH_HOSTS_CONSUMED_PATH", str(tmp_path / "consumed.json"))
    monkeypatch.setenv("VEYA_SSH_HOSTS_CM_DIR", str(tmp_path / "cm"))
    root = tmp_path / "w"
    root.mkdir()
    hosts_mod.save_host(
        hosts_mod.RemoteHost(
            host_id="lap",
            display_name="Laptop",
            tailnet_ip="100.64.0.7",
            owner_id="u1",
            write_root=str(root),
            default_cwd=str(root),
            metadata={"read_approved": False},
        )
    )
    identity_mod.save_policy(identity_mod.IdentityPolicy(default="deny", allowlist=["host:laptop"]))
    monkeypatch.setattr(
        ssh_tools,
        "IDENTITY_RESOLVER",
        lambda ip: identity_mod.PeerIdentity(
            node_name="laptop" if ip == "100.64.0.7" else "stranger", ip=ip, resolved=True
        ),
    )
    runner = LocalExecRunner()
    monkeypatch.setattr(ssh_tools, "RUNNER", runner)
    fake = FakeApprovals()
    monkeypatch.setattr(approval_mod, "_service", lambda: fake)
    monkeypatch.setattr(tools, "_caller_owner_id", lambda: "u1")
    return {"root": root, "runner": runner, "approvals": fake}


async def _resolve_result(val: Any) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val


def test_canonical_tools_schema_has_host():
    """Verify canonical tools schema in master_tools contains host parameter."""
    schemas = {
        s["function"]["name"]: s["function"]
        for s in master_tools.get_all_schemas()
        if s.get("type") == "function"
    }
    for tool_name in (
        "write_file",
        "read_hashline",
        "edit_hashline",
        "ast_grep_rewrite",
        "read_file_ast",
        "grep",
        "list_files",
        "run_in_sandbox",
    ):
        assert tool_name in schemas, f"tool {tool_name} not registered"
        props = schemas[tool_name].get("parameters", {}).get("properties", {})
        assert "host" in props, f"tool {tool_name} missing host parameter"
        assert "approved" in props, f"tool {tool_name} missing approved parameter"
        assert "approval_id" in props, f"tool {tool_name} missing approval_id parameter"


async def test_canonical_tools_refused_when_flag_off(env, monkeypatch):
    """When VEYA_SSH_HOSTS is OFF, canonical tools with host parameter are refused."""
    monkeypatch.setenv("VEYA_SSH_HOSTS", "0")
    res = await _resolve_result(_tool_write_file(filepath="test.txt", content="hello", host="lap"))
    assert "功能未启用" in str(res)


async def test_canonical_write_and_read_hashline_remote(env):
    """Write file to remote host with approval, then read it via read_hashline."""
    # 1. Mutating tool without approved=True is refused
    res = await _resolve_result(
        _tool_write_file(filepath="sub/hello.txt", content="line1\nline2\n", host="lap")
    )
    assert "需要 approved=true" in str(res)
    assert not (env["root"] / "sub" / "hello.txt").exists()

    # 2. Mutating tool with approved=True creates pending approval
    pending = await _resolve_result(
        _tool_write_file(
            filepath="sub/hello.txt", content="line1\nline2\n", host="lap", approved=True
        )
    )
    pending_dict = json.loads(pending)
    assert pending_dict["status"] == "approval_pending"
    aid = pending_dict["approval_id"]

    # 3. Resolve approval and call again with approval_id
    env["approvals"].resolve(aid, "once")
    done = await _resolve_result(
        _tool_write_file(
            filepath="sub/hello.txt",
            content="line1\nline2\n",
            host="lap",
            approved=True,
            approval_id=aid,
        )
    )
    assert "wrote" in done
    assert (env["root"] / "sub" / "hello.txt").read_text() == "line1\nline2\n"

    # 4. Read requires approval when read_approved is False
    read_pending = await _resolve_result(
        _tool_read_hashline(filepath="sub/hello.txt", host="lap", approved=True)
    )
    read_p_dict = json.loads(read_pending)
    assert read_p_dict["status"] == "approval_pending"
    read_aid = read_p_dict["approval_id"]
    env["approvals"].resolve(read_aid, "once")

    read_done = await _resolve_result(
        _tool_read_hashline(
            filepath="sub/hello.txt", host="lap", approved=True, approval_id=read_aid
        )
    )
    assert "[hashline sub/hello.txt]" in read_done
    assert "line1" in read_done


async def test_canonical_read_approved_host_skips_approval(env, monkeypatch):
    """When host.metadata.read_approved is True, read tools do not need approval."""
    hosts_mod.save_host(
        hosts_mod.RemoteHost(
            host_id="lap",
            display_name="Laptop",
            tailnet_ip="100.64.0.7",
            owner_id="u1",
            write_root=str(env["root"]),
            default_cwd=str(env["root"]),
            metadata={"read_approved": True},
        )
    )
    (env["root"] / "data.py").write_text("def foo():\n    return 42\n")

    res = await _resolve_result(_tool_read_file_ast(filepath="data.py", host="lap"))
    assert "def foo()" in res

    files = await _resolve_result(_tool_list_files(path=".", host="lap"))
    assert "data.py" in files

    grep_res = await _resolve_result(_tool_grep(pattern="return 42", host="lap"))
    assert "data.py" in grep_res


async def test_canonical_run_in_sandbox_remote(env):
    """run_in_sandbox with host executes command on remote host."""
    res = await _tool_run_in_sandbox(command="echo 'remote execution'", host="lap", approved=False)
    assert "需要 approved=true" in res

    pending = await _tool_run_in_sandbox(
        command="echo 'remote execution'", host="lap", approved=True
    )
    pending_dict = json.loads(pending)
    assert pending_dict["status"] == "approval_pending"
    aid = pending_dict["approval_id"]

    env["approvals"].resolve(aid, "once")
    out = await _tool_run_in_sandbox(
        command="echo 'remote execution'", host="lap", approved=True, approval_id=aid
    )
    assert "exit_code=0" in out
    assert "remote execution" in out


async def test_canonical_tools_via_master_tools_execute(env):
    """Calling canonical tools through master_tools.execute with host works seamlessly."""
    # Write file via registry execute
    pending = await master_tools.execute(
        "write_file",
        {"filepath": "exec_test.txt", "content": "via registry", "host": "lap", "approved": True},
    )
    pending_dict = json.loads(pending)
    assert pending_dict["status"] == "approval_pending"
    aid = pending_dict["approval_id"]
    env["approvals"].resolve(aid, "once")

    done = await master_tools.execute(
        "write_file",
        {
            "filepath": "exec_test.txt",
            "content": "via registry",
            "host": "lap",
            "approved": True,
            "approval_id": aid,
        },
    )
    assert "wrote" in done
    assert (env["root"] / "exec_test.txt").read_text() == "via registry"
