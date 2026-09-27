from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from veya.remote.action_gateway import (
    ActionCategory,
    ActionGateway,
    classify_action,
)
from veya.remote.approval import ApprovalStore
from veya.remote.models import (
    RemotePermissions,
    RemoteSession,
    RiskClass,
)
from veya.remote.tool_adapter import RemoteToolAdapter
from veya.remote.workspace_policy import WorkspacePolicy, managed_home_roots


def _repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _session(path: Path, *, network: bool = True) -> RemoteSession:
    now = time.time()
    return RemoteSession(
        session_id="rs-l0",
        principal="chatgpt-web",
        token_id="rt-l0",
        workspaces=(str(path.resolve()),),
        active_workspace=str(path.resolve()),
        permissions=RemotePermissions(
            read=True,
            write=True,
            shell=True,
            git=True,
            network=network,
            destructive=False,
            service_control=True,
        ),
        created_at=now,
        expires_at=now + 3600,
    )


class FakeProcess:
    def __init__(
        self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"", pid: int = 12345
    ) -> None:
        self.returncode = returncode
        self.pid = pid
        # Provide asyncio.StreamReader-compatible stdout/stderr so
        # direct_exec._pump(proc.stdout, ...) can read() from them.
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def communicate(self) -> tuple[bytes, bytes]:
        out = await self.stdout.read(-1)
        err = await self.stderr.read(-1)
        return out, err

    async def wait(self) -> int:
        return self.returncode


# 1. Workspace file operations are AUTO_OPEN (no approved flag required)
async def test_auto_open_workspace_file_ops_without_approved_flag(tmp_path: Path) -> None:
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)

    # Read
    res_read = await adapter._call_impl(
        session, "file.read", {"path": "file.txt", "workspace": str(tmp_path)}
    )
    assert res_read.ok is True
    assert "hello" in str(res_read.result)

    # Write without approved=true
    res_write = await adapter._call_impl(
        session,
        "file.write",
        {"path": "new_file.txt", "content": "world\n", "workspace": str(tmp_path)},
    )
    assert res_write.ok is True
    # Reading back through file.read observes the write in session worktree
    res_read2 = await adapter._call_impl(
        session, "file.read", {"path": "new_file.txt", "workspace": str(tmp_path)}
    )
    assert res_read2.ok is True
    assert "world" in str(res_read2.result)

    # Workspace info without approved
    res_info = await adapter._call_impl(
        session, "workspace.info", {"path": ".", "workspace": str(tmp_path)}
    )
    assert res_info.ok is True

    # Path escape is blocked.
    #
    # Canonical precedence (see PermissionEngine): a valid request is classified
    # by permission BEFORE workspace/execution discovery, so an out-of-scope
    # target is refused by the permission layer itself. The refusal is therefore
    # POLICY_BLOCKED (engine reason DENY_SCOPE_ESCAPE) rather than a workspace
    # error produced by later repository discovery. Both are fail-closed; the
    # canonical one is reported here so the contract stays unambiguous.
    res_escape = await adapter._call_impl(
        session, "file.read", {"path": "../../etc/passwd", "workspace": str(tmp_path)}
    )
    assert res_escape.ok is False
    assert str(res_escape.error_code) == "POLICY_BLOCKED"

    # The permission verdict, not just the transport code, is the contract.
    from veya.remote.permission_engine import (
        Decision,
        OperationContext,
        PermissionEngine,
        ReasonCode,
    )

    decision = PermissionEngine().evaluate(
        OperationContext(
            tool="file.read",
            operation="file.read",
            workspace_root=tmp_path,
            cwd=tmp_path,
            target_paths=((tmp_path / "../../etc/passwd").resolve(),),
            filesystem_effect="read",
        )
    )
    assert decision.decision is Decision.DENY
    assert decision.reason is ReasonCode.DENY_SCOPE_ESCAPE


# 2. Managed home roots are accessible while sensitive paths remain blocked
def test_managed_home_roots_accessible(tmp_path: Path) -> None:
    policy = WorkspacePolicy(root=tmp_path, permissions=RemotePermissions())
    home_roots = managed_home_roots()
    assert len(home_roots) == 4
    # Protected credential path is rejected
    with pytest.raises(Exception) as exc_info:
        policy.resolve(str(Path.home() / ".ssh" / "id_rsa"))
    assert getattr(exc_info.value, "code", "") == "WORKSPACE_DENIED"


# 3. Git normal operations are AUTO_OPEN
@pytest.mark.parametrize(
    "cmd",
    [
        "git status",
        "git diff",
        "git log -n 10",
        "git add file.txt",
        "git restore --staged file.txt",
        "git commit -m 'update'",
        "git branch feature",
        "git switch feature",
        "git checkout main",
        "git fetch",
        "git pull --ff-only",
        "git push origin main",
        "git tag v1.0.0",
    ],
)
def test_auto_open_git_normal_ops(tmp_path: Path, cmd: str) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == ActionCategory.AUTO_OPEN
    assert cl.capability_id == "git.normal"
    assert cl.requires_approval is False


# 4. Git destructive operations are HUMAN_GATED
@pytest.mark.parametrize(
    "cmd",
    [
        "git reset --hard HEAD~1",
        "git clean -fdx",
        "git push --force origin main",
        "git push -f origin main",
        "git push --force-with-lease origin main",
        "git branch -D old-branch",
        "git tag -d v1.0.0",
    ],
)
def test_human_gated_git_destructive(tmp_path: Path, cmd: str) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == ActionCategory.HUMAN_GATED
    assert cl.capability_id == "privileged.git_destructive"
    assert cl.risk_class == RiskClass.P2_ROOT_MUTATION
    assert cl.requires_approval is True


# 5. Normal argv shell execution is AUTO_OPEN without approved flag
@pytest.mark.parametrize(
    "cmd",
    [
        "python3 script.py",
        "node index.js",
        "npm test",
        "pnpm build",
        "uv run foo",
        "pytest -q",
        "ruff check .",
        "mypy .",
        "rg foo",
        "find . -name '*.py'",
        "cat file.txt",
        "mkdir -p src",
        "curl https://example.com",
    ],
)
def test_auto_open_normal_argv_shell(tmp_path: Path, cmd: str) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == ActionCategory.AUTO_OPEN
    assert cl.capability_id == "shell.argv"
    assert cl.requires_approval is False


# 6. Shell wrappers (bash -lc, sh -c) are HUMAN_GATED (P1_PRIVILEGED_HOST)
@pytest.mark.parametrize(
    "cmd",
    [
        'bash -lc "ls -la && id"',
        'sh -c "echo hello | grep h"',
        '/usr/bin/bash -lc "whoami"',
        'bash -c "cat /etc/hosts"',
    ],
)
def test_human_gated_shell_wrappers(tmp_path: Path, cmd: str) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == ActionCategory.HUMAN_GATED
    assert cl.capability_id == "privileged.shell_wrapper"
    assert cl.risk_class == RiskClass.P1_PRIVILEGED_HOST
    assert cl.requires_approval is True


# 7. Python / Node inline execution is AUTO_OPEN (not shell wrappers)
@pytest.mark.parametrize(
    "cmd",
    [
        'python -c "import sys; print(sys.version)"',
        'python3 -c "print(1 + 1)"',
        'node -e "console.log(process.pid)"',
    ],
)
def test_auto_open_inline_runtime(tmp_path: Path, cmd: str) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == ActionCategory.AUTO_OPEN
    assert cl.capability_id == "shell.inline_runtime"
    assert cl.requires_approval is False


# 8. User systemd is AUTO_OPEN on managed units
@pytest.mark.parametrize(
    "cmd",
    [
        "systemctl --user daemon-reload",
        "systemctl --user start veya-remote-mcp.service",
        "systemctl --user stop veya-openai-tunnel.service",
        "systemctl --user restart veya-remote-mcp.service",
        "systemctl --user is-active veya-remote-mcp.service",
        "systemctl --user status veya-openai-tunnel.service",
        "systemctl --user status veya-gateway.service",
        "systemctl --user status hevi-api.service",
    ],
)
def test_auto_open_user_systemd(tmp_path: Path, cmd: str) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == ActionCategory.AUTO_OPEN
    assert cl.capability_id == "service_control.user"
    assert cl.requires_approval is False


# 9. Sudo & Root shell are HUMAN_GATED
@pytest.mark.parametrize(
    ("cmd", "cap", "risk"),
    [
        ("sudo apt install vim", "privileged.sudo", RiskClass.P2_ROOT_MUTATION),
        ("sudo systemctl restart nginx", "privileged.sudo", RiskClass.P2_ROOT_MUTATION),
        ("sudo -i", "privileged.root_shell", RiskClass.P3_CRITICAL_HOST),
        ("sudo -s", "privileged.root_shell", RiskClass.P3_CRITICAL_HOST),
        ("sudo bash", "privileged.root_shell", RiskClass.P3_CRITICAL_HOST),
        ("su -", "privileged.root_shell", RiskClass.P3_CRITICAL_HOST),
    ],
)
def test_human_gated_sudo_and_root_shell(
    tmp_path: Path, cmd: str, cap: str, risk: RiskClass
) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == ActionCategory.HUMAN_GATED
    assert cl.capability_id == cap
    assert cl.risk_class == risk


# 10. System service read-only vs mutation
@pytest.mark.parametrize(
    "cmd",
    [
        "systemctl status ssh.service",
        "systemctl --system is-active docker.service",
        "systemctl show NetworkManager.service",
    ],
)
def test_auto_open_system_service_read(tmp_path: Path, cmd: str) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == ActionCategory.AUTO_OPEN
    assert cl.capability_id == "system_service.read"


@pytest.mark.parametrize(
    ("cmd", "cap", "risk"),
    [
        (
            "systemctl restart ssh.service",
            "privileged.critical_service",
            RiskClass.P3_CRITICAL_HOST,
        ),
        (
            "systemctl stop docker.service",
            "privileged.critical_service",
            RiskClass.P3_CRITICAL_HOST,
        ),
        (
            "systemctl restart my-worker.service",
            "privileged.system_service_write",
            RiskClass.P2_ROOT_MUTATION,
        ),
        (
            "systemctl --system daemon-reload",
            "privileged.system_service_write",
            RiskClass.P2_ROOT_MUTATION,
        ),
    ],
)
def test_human_gated_system_service_write(
    tmp_path: Path, cmd: str, cap: str, risk: RiskClass
) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == ActionCategory.HUMAN_GATED
    assert cl.capability_id == cap
    assert cl.risk_class == risk


# 11. Journalctl user & system
@pytest.mark.parametrize(
    ("cmd", "cap"),
    [
        ("journalctl --user -u veya-remote-mcp.service --no-pager", "service_logs.user"),
        ("journalctl -u docker.service --no-pager", "system_logs.read"),
        ("journalctl -k --no-pager", "system_logs.read"),
    ],
)
def test_auto_open_journalctl(tmp_path: Path, cmd: str, cap: str) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == ActionCategory.AUTO_OPEN
    assert cl.capability_id == cap


# 12. Docker read vs project vs privileged
@pytest.mark.parametrize(
    ("cmd", "cat", "cap"),
    [
        ("docker ps", ActionCategory.AUTO_OPEN, "docker.read"),
        ("docker logs container_a", ActionCategory.AUTO_OPEN, "docker.read"),
        ("docker compose ps", ActionCategory.AUTO_OPEN, "docker.read"),
        ("docker compose up -d", ActionCategory.AUTO_OPEN, "docker.project"),
        ("docker compose build", ActionCategory.AUTO_OPEN, "docker.project"),
        ("docker compose down", ActionCategory.AUTO_OPEN, "docker.project"),
        (
            "docker run --privileged ubuntu",
            ActionCategory.HUMAN_GATED,
            "privileged.docker_privileged",
        ),
        (
            "docker run -v /:/host ubuntu",
            ActionCategory.HUMAN_GATED,
            "privileged.docker_privileged",
        ),
        ("docker system prune", ActionCategory.HUMAN_GATED, "privileged.docker_privileged"),
    ],
)
def test_docker_classification(tmp_path: Path, cmd: str, cat: ActionCategory, cap: str) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == cat
    assert cl.capability_id == cap


# 13. Host package manager read vs mutation
@pytest.mark.parametrize(
    ("cmd", "cat", "cap"),
    [
        ("apt list --installed", ActionCategory.AUTO_OPEN, "host.package_read"),
        ("dpkg -l", ActionCategory.AUTO_OPEN, "host.package_read"),
        ("apt-cache search python", ActionCategory.AUTO_OPEN, "host.package_read"),
        ("apt install python3-pip", ActionCategory.HUMAN_GATED, "privileged.host_package_manager"),
        ("apt-get remove vim", ActionCategory.HUMAN_GATED, "privileged.host_package_manager"),
        ("dpkg -i foo.deb", ActionCategory.HUMAN_GATED, "privileged.host_package_manager"),
        ("snap install certbot", ActionCategory.HUMAN_GATED, "privileged.host_package_manager"),
    ],
)
def test_package_manager_classification(
    tmp_path: Path, cmd: str, cat: ActionCategory, cap: str
) -> None:
    session = _session(tmp_path)
    cl = classify_action("shell.exec", {"command": cmd}, session, str(tmp_path))
    assert cl.category == cat
    assert cl.capability_id == cap


# 14. ActionGateway rejects boolean approved=true for privileged operations
async def test_action_gateway_rejects_boolean_approved_flag(tmp_path: Path) -> None:
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)

    # Calling sudo with approved=True but no approval_id MUST BE REJECTED
    res = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": "sudo apt install htop",
            "approved": True,  # Legacy boolean approved
            "workspace": str(tmp_path),
        },
    )
    assert res.ok is False
    assert str(res.error_code) == "INVALID_APPROVAL"
    assert "server-issued approval_id required" in str(res.message)


# 15. ActionGateway requires approval_id and consumes it once
async def test_action_gateway_one_shot_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)
    store = ApprovalStore()
    adapter.action_gateway = ActionGateway(approval_store=store)

    cmd = "sudo apt install curl"
    record = store.create_approval(
        principal=session.principal,
        capability_id="privileged.sudo",
        normalized_operation=cmd,
        cwd=str(tmp_path),
        workspace=session.active_workspace,
        risk_class=RiskClass.P2_ROOT_MUTATION,
    )

    # Mock execution so sudo doesn't actually run on host
    async def fake_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        return FakeProcess(0, b"installed\n", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    # First call: valid approval_id -> succeeds
    res1 = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": cmd,
            "approval_id": record.approval_id,
            "workspace": str(tmp_path),
        },
    )
    assert res1.ok is True

    # Second call with SAME approval_id -> REJECTED (single use!)
    res2 = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": cmd,
            "approval_id": record.approval_id,
            "workspace": str(tmp_path),
        },
    )
    assert res2.ok is False
    assert str(res2.error_code) == "INVALID_APPROVAL"
    assert "already been consumed" in str(res2.message)


# 16. ActionGateway detects parameter tampering (APPROVAL_MISMATCH)
async def test_action_gateway_detects_parameter_tampering(tmp_path: Path) -> None:
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)
    store = ApprovalStore()
    adapter.action_gateway = ActionGateway(approval_store=store)

    # Approved for 'sudo apt install pkgA'
    record = store.create_approval(
        principal=session.principal,
        capability_id="privileged.sudo",
        normalized_operation="sudo apt install pkgA",
        cwd=str(tmp_path),
        workspace=session.active_workspace,
        risk_class=RiskClass.P2_ROOT_MUTATION,
    )

    # Executing 'sudo apt install pkgB' with that approval_id -> BLOCKED
    res = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": "sudo apt install pkgB",
            "approval_id": record.approval_id,
            "workspace": str(tmp_path),
        },
    )
    assert res.ok is False
    assert str(res.error_code) == "APPROVAL_MISMATCH"


# 17. Expired approval is rejected
async def test_action_gateway_expired_approval(tmp_path: Path) -> None:
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)
    store = ApprovalStore()
    adapter.action_gateway = ActionGateway(approval_store=store)

    record = store.create_approval(
        principal=session.principal,
        capability_id="privileged.sudo",
        normalized_operation="sudo apt update",
        cwd=str(tmp_path),
        workspace=session.active_workspace,
        risk_class=RiskClass.P2_ROOT_MUTATION,
        ttl_s=0.01,
    )
    time.sleep(0.05)

    res = await adapter._call_impl(
        session,
        "shell.exec",
        {
            "command": "sudo apt update",
            "approval_id": record.approval_id,
            "workspace": str(tmp_path),
        },
    )
    assert res.ok is False
    assert str(res.error_code) == "APPROVAL_EXPIRED"
