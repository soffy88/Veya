"""W17 group B — ssh tools, approval gate, guard, registration (offline)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server.remote_control import approval as approval_mod
from server.remote_control import hosts as hosts_mod
from server.remote_control import identity as identity_mod
from server.remote_control import ssh_tools, tools
from server.tool_guard import ToolGuard
from tests.w17_fakes import LocalExecRunner


class FakeApprovals:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    async def request(self, *, tool, args, user_id, session_id="", actor="", reason="", **_):
        aid = f"a{len(self.records) + 1}"
        self.records[aid] = {
            "approval_id": aid,
            "tool": tool,
            "tool_args": args,
            "status": "pending",
            "decision": "",
            "user_id": user_id,
        }
        return {"approval_id": aid}

    def get(self, aid):
        return self.records.get(aid)

    def resolve(self, aid, decision):
        self.records[aid].update(status="resolved", decision=decision)


@pytest.fixture
def env(tmp_path, monkeypatch):
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
        )
    )
    hosts_mod.save_host(
        hosts_mod.RemoteHost(
            host_id="other",
            display_name="Other",
            tailnet_ip="100.64.0.8",
            owner_id="u2",
            write_root=str(root),
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


async def _approved_call(env, func, **kwargs):
    first = json.loads(await func(**kwargs, approved=True))
    assert first["status"] == "approval_pending"
    env["approvals"].resolve(first["approval_id"], "once")
    return await func(**kwargs, approved=True, approval_id=first["approval_id"])


async def test_host_write_refused_without_approved(env) -> None:
    out = await ssh_tools.ssh_write_file(host_id="lap", path="a.txt", content="x")
    assert out.startswith("已拒绝") and not (env["root"] / "a.txt").exists()
    assert env["runner"].calls == []  # no ssh was issued
    pending = await ssh_tools.ssh_write_file(
        host_id="lap", path="a.txt", content="x", approved=True
    )
    assert json.loads(pending)["status"] == "approval_pending"
    assert not (env["root"] / "a.txt").exists() and env["runner"].calls == []
    aid = json.loads(pending)["approval_id"]
    still = await ssh_tools.ssh_write_file(
        host_id="lap", path="a.txt", content="x", approved=True, approval_id=aid
    )
    assert "pending" in still and not (env["root"] / "a.txt").exists()
    env["approvals"].resolve(aid, "deny")
    denied = await ssh_tools.ssh_write_file(
        host_id="lap", path="a.txt", content="x", approved=True, approval_id=aid
    )
    assert "refused" in denied and not (env["root"] / "a.txt").exists()


async def test_tool_host_param_routes_to_ssh(env) -> None:
    out = await _approved_call(
        env, ssh_tools.ssh_write_file, host_id="lap", path="a.txt", content="hi\n"
    )
    assert "wrote" in out and (env["root"] / "a.txt").read_text() == "hi\n"
    assert env["runner"].calls and env["runner"].calls[-1][0] == "ssh"
    ran = json.loads(
        await _approved_call(env, ssh_tools.ssh_run, host_id="lap", command="cat a.txt")
    )
    assert ran["exit_code"] == 0 and ran["stdout"] == "hi\n"
    read = await _approved_call(env, ssh_tools.ssh_read_file, host_id="lap", path="a.txt")
    assert read == "hi\n"


async def test_approval_bound_to_args_and_single_use(env) -> None:
    first = json.loads(
        await ssh_tools.ssh_write_file(host_id="lap", path="a.txt", content="ok", approved=True)
    )
    env["approvals"].resolve(first["approval_id"], "once")
    aid = first["approval_id"]
    swapped = await ssh_tools.ssh_write_file(
        host_id="lap", path="b.txt", content="evil", approved=True, approval_id=aid
    )
    assert "不是为这次调用" in swapped and not (env["root"] / "b.txt").exists()
    other_tool = await ssh_tools.ssh_run(
        host_id="lap", command="touch c", approved=True, approval_id=aid
    )
    assert "不是为这次调用" in other_tool
    assert "wrote" in await ssh_tools.ssh_write_file(
        host_id="lap", path="a.txt", content="ok", approved=True, approval_id=aid
    )
    again = await ssh_tools.ssh_write_file(
        host_id="lap", path="a.txt", content="ok", approved=True, approval_id=aid
    )
    assert "已被使用" in again


async def test_read_approved_host_skips_approval(env) -> None:
    (env["root"] / "r.txt").write_text("readme")
    host = hosts_mod.get_host("lap")
    host.metadata = {"read_approved": True}
    hosts_mod.save_host(host)
    assert await ssh_tools.ssh_read_file(host_id="lap", path="r.txt") == "readme"
    assert "r.txt" in await ssh_tools.ssh_list_dir(host_id="lap")
    # writes still need approval regardless
    assert (await ssh_tools.ssh_write_file(host_id="lap", path="x", content="x")).startswith(
        "已拒绝"
    )


async def test_unregistered_or_foreign_host_refused(env) -> None:
    for host_id in ("ghost", "other"):
        out = await ssh_tools.ssh_run(host_id=host_id, command="true", approved=True)
        assert out.startswith("已拒绝") and "找不到主机" in out
    assert await ssh_tools.ssh_host_probe(host_id="other") == "已拒绝：找不到主机 'other'。"
    assert env["runner"].calls == [] and env["approvals"].records == {}


async def test_identity_policy_refusal(env, monkeypatch) -> None:
    identity_mod.save_policy(identity_mod.IdentityPolicy(default="deny", denylist=["host:laptop"]))
    out = await ssh_tools.ssh_host_probe(host_id="lap")
    assert "身份策略拒绝" in out and env["runner"].calls == []


async def test_catastrophic_command_refused_even_when_approved(env) -> None:
    out = await _approved_call(env, ssh_tools.ssh_run, host_id="lap", command="rm -rf /")
    assert "refused by policy" in out


async def test_probe_and_host_list(env) -> None:
    probe = json.loads(await ssh_tools.ssh_host_probe(host_id="lap"))
    assert probe["reachable"] is True and probe["os_family_echo"] == "linux"
    listed = json.loads(await ssh_tools.ssh_host_list())
    assert [h["host_id"] for h in listed] == ["lap"]
    assert "ssh_key_ref" not in listed[0]


# ── registration ──


class _Reg:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def has(self, name):
        return name in self.items

    def register(self, name, description, parameters, func, **kw):
        self.items[name] = {"parameters": parameters, **kw}


def test_ssh_tools_registered_only_when_on(monkeypatch) -> None:
    from server.tool_registry import SideEffect

    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "1")
    monkeypatch.delenv("VEYA_SSH_HOSTS", raising=False)
    reg = _Reg()
    tools.register_tools(reg)
    assert not any(name.startswith("ssh_") for name in reg.items)
    assert "remote_host_list" in reg.items  # W13 surface unchanged
    monkeypatch.setenv("VEYA_SSH_HOSTS", "1")
    guard = ToolGuard()
    monkeypatch.setattr("server.tool_guard.global_tool_guard", guard)
    reg2 = _Reg()
    tools.register_tools(reg2)
    ssh = {n: v for n, v in reg2.items.items() if n.startswith("ssh_")}
    assert len(ssh) == 13
    for name in ssh_tools.MUTATING_TOOLS:
        assert ssh[name]["side_effect"] == SideEffect.EXTERNAL_MUTATION
        assert ssh[name]["effect_capability"] == "manual_only"
        assert "approved" in ssh[name]["parameters"]["required"]
    for name in (
        "ssh_host_probe",
        "ssh_bg_status",
        "ssh_read_file",
        "ssh_list_dir",
        "ssh_grep",
        "ssh_host_list",
    ):
        assert ssh[name]["side_effect"] == SideEffect.PURE_READ
    assert guard.has_policy(ssh_tools.GUARD_POLICY)
    monkeypatch.delenv("VEYA_REMOTE_CONTROL", raising=False)
    reg3 = _Reg()
    assert tools.register_tools(reg3) == 0 and reg3.items == {}


# ── both execute branches ──


def _registry_with_ssh_tools():
    from server.tool_registry import MasterToolRegistry

    reg = MasterToolRegistry()
    for name, description, parameters, func, effect in ssh_tools._TOOLS:
        from server.tool_registry import SideEffect

        reg.register(
            name,
            description,
            parameters,
            func,
            side_effect=SideEffect.PURE_READ
            if effect == "pure_read"
            else SideEffect.EXTERNAL_MUTATION,
            effect_capability="none" if effect == "pure_read" else "manual_only",
        )
    return reg


async def test_guard_promotes_host_calls_tool_guard_branch(env, monkeypatch) -> None:
    import server.tool_registry as tr
    from server.tool_registry import ToolExecutionError

    guard = ToolGuard()
    ssh_tools.install_w17_host_policy(guard)
    monkeypatch.setattr(tr, "_tool_guard", guard)
    monkeypatch.setattr("server.tool_governance_adapter.current_task_governance", lambda: None)
    reg = _registry_with_ssh_tools()
    with pytest.raises(ToolExecutionError):
        await reg.execute(
            "ssh_write_file", {"host_id": "lap", "path": "g.txt", "content": "x", "approved": False}
        )
    assert not (env["root"] / "g.txt").exists()
    # read-only tools are not blocked by the guard
    out = await reg.execute("ssh_bg_status", {"host_id": "lap", "name": "none"})
    assert '"found": false' in out


async def test_guard_promotes_host_calls_governance_branch(env, monkeypatch) -> None:
    """Under task governance the guard is skipped; the tool body still refuses."""

    class FakeGovernance:
        calls = 0

        async def execute_native(self, *, name, arguments, executor, **_):
            FakeGovernance.calls += 1
            return await executor(**arguments)

    gov = FakeGovernance()
    monkeypatch.setattr("server.tool_governance_adapter.current_task_governance", lambda: gov)
    reg = _registry_with_ssh_tools()
    out = await reg.execute(
        "ssh_write_file", {"host_id": "lap", "path": "g.txt", "content": "x", "approved": False}
    )
    assert FakeGovernance.calls == 1
    assert str(out).startswith("已拒绝") and not (env["root"] / "g.txt").exists()
    pending = await reg.execute(
        "ssh_write_file", {"host_id": "lap", "path": "g.txt", "content": "x", "approved": True}
    )
    assert "approval_pending" in str(pending) and not (env["root"] / "g.txt").exists()


def test_guard_policy_shape() -> None:
    assert ssh_tools.host_policy("ssh_run", {"approved": False}, "t")
    assert ssh_tools.host_policy("ssh_run", {"approved": True}, "t") is None
    assert ssh_tools.host_policy("ssh_read_file", {}, "t") is None
    assert ssh_tools.host_policy("write_file", {}, "t") is None  # canonical tools untouched
    _ = Path
