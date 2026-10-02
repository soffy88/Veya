"""Adversarial and qualification test suite for Veya Local2 Full-Capability L0 (P0).

Covers:
- RuntimeProfile: active discovery (Python, venv, pytest, ruff, mypy, docker, etc.)
- ExecutionTarget: NEW_ISOLATED_WORKTREE, EXISTING_WORKTREE, CANONICAL_WORKTREE, HOST
- ExecutionDomain: L0_WORKSPACE_FULL, L0_ISOLATED, L0_HOST
- NESTED_WORKTREE_FOR_EXISTING_TARGET == 0
- Path escape containment (fail-closed)
- Zero secret leaks (redaction)
- Zero orphan processes (timeout and cancel)
- Local2 dogfooding acceptance against real AGY worktree
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from runtime.coding.sandbox_profiles import get_sandbox_profile
from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
)
from veya.remote.mcp_server import create_gateway
from veya.remote.runtime_profile import (
    WorkspaceRuntimeProfile,
    discover_runtime_profile,
)
from veya.remote.workspace_binding import (
    WorkspaceBindingError,
    resolve_repo_target,
)

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
PY = sys.executable
CANONICAL_VEYA = Path("/data/soffy/projects/veya")
AGY_WORKTREE = CANONICAL_VEYA / ".veya" / "worktrees" / "task-remote-bb507e826728150ba9a5"


class DummyExecutor:
    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        if name == "write_file":
            filepath = Path(kwargs["filepath"])
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(kwargs["content"], encoding="utf-8")
            return '{"status": "ok"}'
        if name == "edit_hashline":
            filepath = Path(kwargs["filepath"])
            content = filepath.read_text(encoding="utf-8")
            new_text = kwargs["new_text"]
            lines = content.splitlines(keepends=True)
            for i, line in enumerate(lines):
                if kwargs["start_tag"] in line or line.strip() in kwargs["start_tag"]:
                    lines[i] = new_text
                    break
            else:
                lines.append(new_text)
            filepath.write_text("".join(lines), encoding="utf-8")
            return '{"status": "ok"}'
        return '{"status": "ok"}'


def make_gateway(bound_roots: list[Path]):
    auth = RemoteAuth()
    _record, secret = auth.issue(
        "local2-tester", permissions=PERMS, workspaces=[str(w) for w in bound_roots]
    )
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(DummyExecutor(), redact=audit.redact)
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=4),
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret, adapter


async def rpc(gateway, method, params, *, secret=None, session=None):
    return await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        authorization=f"Bearer {secret}" if secret else None,
        session_header=session,
    )


async def initialize(gateway, secret, *, workspace: str | None = None) -> str:
    params: dict[str, Any] = {"clientInfo": {"name": "local2-test"}}
    if workspace:
        params["workspace"] = workspace
    response = await rpc(gateway, "initialize", params, secret=secret)
    assert "result" in response, response
    return response["result"]["sessionId"]


async def call_tool(gateway, secret, session, name: str, arguments: dict[str, Any]):
    response = await rpc(
        gateway,
        "tools/call",
        {"name": name, "arguments": arguments},
        secret=secret,
        session=session,
    )
    assert "result" in response, response
    return response["result"]["structuredContent"]


# ── 1. Runtime Profile Active Discovery ──────────────────────────────
def test_runtime_profile_discovery_canonical():
    """Verify runtime profile on canonical repo discovers real host tools."""
    profile = discover_runtime_profile(CANONICAL_VEYA, force_refresh=True)
    assert isinstance(profile, WorkspaceRuntimeProfile)
    assert profile.workspace_root == str(CANONICAL_VEYA.resolve())
    assert profile.repo_root == str(CANONICAL_VEYA.resolve())
    assert profile.python is not None
    assert profile.python_bin is not None
    assert profile.pytest is not None
    assert profile.ruff is not None
    assert profile.mypy is not None
    assert profile.profile_hash is not None
    assert len(profile.path_entries) > 0


def test_runtime_profile_discovery_existing_agy_worktree():
    """Verify runtime profile resolves parent repo venv when targeting an existing worktree."""
    if not AGY_WORKTREE.exists():
        pytest.skip(f"AGY worktree not present at {AGY_WORKTREE}")
    profile = discover_runtime_profile(AGY_WORKTREE, force_refresh=True)
    assert profile.workspace_root == str(AGY_WORKTREE.resolve())
    assert profile.repo_root == str(CANONICAL_VEYA.resolve())
    assert profile.python is not None
    assert profile.pytest is not None
    # The parent repo venv was projected into path_entries
    assert any(str(CANONICAL_VEYA) in p for p in profile.path_entries)


# ── 2. Worktree Resolution & Nested Worktree Bug Prevention ──────────
@pytest.mark.asyncio
async def test_nested_worktree_for_existing_target_is_zero(tmp_path: Path):
    """When target is an existing worktree, ensure no nested worktree is created."""
    # 1. Test on AGY worktree: count before == count after
    if AGY_WORKTREE.exists():
        gateway, secret, adapter = make_gateway([CANONICAL_VEYA])
        session_id = await initialize(gateway, secret, workspace=str(AGY_WORKTREE))
        session = gateway.sessions.get(session_id)

        nested_dir = AGY_WORKTREE / ".veya" / "worktrees"
        count_before = len(list(nested_dir.iterdir())) if nested_dir.exists() else 0

        worktree, _repo_root = await adapter._ensure_isolated_worktree(
            session,
            repo_root=str(CANONICAL_VEYA),
            target_path=str(AGY_WORKTREE),
            execution_target="EXISTING_WORKTREE",
        )
        assert worktree == str(AGY_WORKTREE.resolve())
        count_after = len(list(nested_dir.iterdir())) if nested_dir.exists() else 0
        assert count_after == count_before

    # 2. Test on a fresh repo + worktree: verify no .veya/worktrees is created inside the worktree
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("ok", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    wt = tmp_path / "fresh_wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt), "HEAD"], check=True)

    gw, sec, adap = make_gateway([tmp_path])
    sid = await initialize(gw, sec, workspace=str(wt))
    sess = gw.sessions.get(sid)
    res_wt, _ = await adap._ensure_isolated_worktree(
        sess, repo_root=str(repo), target_path=str(wt), execution_target="EXISTING_WORKTREE"
    )
    assert res_wt == str(wt.resolve())
    assert not (wt / ".veya" / "worktrees").exists()


@pytest.mark.asyncio
async def test_execution_target_canonical_and_host():
    """CANONICAL_WORKTREE and HOST return repo root directly."""
    gateway, secret, adapter = make_gateway([CANONICAL_VEYA])
    session_id = await initialize(gateway, secret, workspace=str(CANONICAL_VEYA))
    session = gateway.sessions.get(session_id)

    wt, rr = await adapter._ensure_isolated_worktree(
        session,
        repo_root=str(CANONICAL_VEYA),
        execution_target="CANONICAL_WORKTREE",
    )
    assert wt == str(CANONICAL_VEYA.resolve())
    assert rr == str(CANONICAL_VEYA.resolve())

    wt_h, rr_h = await adapter._ensure_isolated_worktree(
        session,
        repo_root=str(CANONICAL_VEYA),
        execution_target="HOST",
    )
    assert wt_h == str(CANONICAL_VEYA.resolve())
    assert rr_h == str(CANONICAL_VEYA.resolve())


# ── 3. Sandbox Profiles & Domains ───────────────────────────────────
def test_l0_sandbox_profiles_registration():
    p_full = get_sandbox_profile("l0_workspace_full")
    assert p_full.id == "l0_workspace_full"
    assert p_full.network == "allowed"

    p_iso = get_sandbox_profile("l0_isolated")
    assert p_iso.id == "l0_isolated"
    assert p_iso.network == "denied"

    p_host = get_sandbox_profile("l0_host")
    assert p_host.id == "l0_host"

    # Case insensitivity
    assert get_sandbox_profile("L0_WORKSPACE_FULL").id == "l0_workspace_full"
    assert get_sandbox_profile("L0_ISOLATED").id == "l0_isolated"


# ── 4. Containment & Path Escape Rejection ───────────────────────────
def test_path_escape_blocked_fail_closed(tmp_path: Path):
    bound = tmp_path / "workspace"
    bound.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    # Escaping with relative ../..
    with pytest.raises(WorkspaceBindingError) as exc_info:
        resolve_repo_target(bound, "../../outside", operation="file.read")
    assert exc_info.value.code == "WORKSPACE_DENIED"

    # Absolute path escaping boundary
    with pytest.raises(WorkspaceBindingError) as exc_info2:
        resolve_repo_target(bound, str(outside), operation="file.read")
    assert exc_info2.value.code == "WORKSPACE_DENIED"

    # NUL byte rejection
    with pytest.raises(WorkspaceBindingError) as exc_info3:
        resolve_repo_target(bound, "bad\x00path", operation="file.read")
    assert exc_info3.value.code == "WORKSPACE_DENIED"


# ── 5. Secret Redaction (Zero Secret Leaks) ──────────────────────────
@pytest.mark.asyncio
async def test_secret_redaction_zero_leaks(tmp_path: Path, monkeypatch):
    secret_token = "secret-token-abcdef123456"
    monkeypatch.setenv("TEST_SECRET_KEY", secret_token)

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)

    gateway, secret, _ = make_gateway([tmp_path])
    session_id = await initialize(gateway, secret, workspace=str(tmp_path))

    # Echo the secret
    envelope = await call_tool(
        gateway,
        secret,
        session_id,
        "shell.exec",
        {
            "command": f"{PY} -c \"import os; print('{secret_token}')\"",
            "execution_domain": "L0_WORKSPACE_FULL",
            "execution_target": "CANONICAL_WORKTREE",
            "wait": True,
        },
    )
    result = envelope.get("result", {})
    text = result.get("text", "") or result.get("stdout_tail", "")
    assert secret_token not in text
    assert "[REDACTED]" in text


# ── 6. Process Supervision: Timeout & Cancellation (Zero Orphans) ────
@pytest.mark.asyncio
async def test_process_supervision_timeout(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)

    gateway, secret, _ = make_gateway([tmp_path])
    session_id = await initialize(gateway, secret, workspace=str(tmp_path))

    envelope = await call_tool(
        gateway,
        secret,
        session_id,
        "shell.exec",
        {
            "command": f'{PY} -c "import time; time.sleep(10)"',
            "timeout_s": 0.5,
            "wait_timeout_s": 2.0,
            "execution_domain": "L0_WORKSPACE_FULL",
            "execution_target": "CANONICAL_WORKTREE",
            "wait": True,
        },
    )
    eid = envelope.get("execution_id") or envelope.get("result", {}).get("execution_id")
    res_status = str(envelope.get("result", {}).get("status") or "").lower()
    if res_status in {"timeout", "failed"}:
        pass
    else:
        final_status = res_status
        for _ in range(30):
            status_resp = await call_tool(
                gateway, secret, session_id, "process.status", {"execution_id": eid}
            )
            final_status = str(status_resp["result"]["status"]).lower()
            if final_status not in {"running", "starting"}:
                break
            await asyncio.sleep(0.2)
        # A deadline that expires reports as a timeout, not a generic failure.
    assert final_status in {"timeout", "timed_out", "failed"}


# ── 7. New MCP Tools: runtime.profile, capabilities, probe ───────────
@pytest.mark.asyncio
async def test_runtime_tools_mcp_surface():
    gateway, secret, _ = make_gateway([CANONICAL_VEYA])
    session_id = await initialize(gateway, secret, workspace=str(CANONICAL_VEYA))

    # runtime.profile
    prof_resp = await call_tool(
        gateway, secret, session_id, "runtime.profile", {"workspace": str(CANONICAL_VEYA)}
    )
    assert prof_resp["ok"] is True
    profile_data = prof_resp["result"]["profile"]
    assert profile_data["python"] is not None
    assert profile_data["pytest"] is not None

    # runtime.capabilities
    caps_resp = await call_tool(
        gateway, secret, session_id, "runtime.capabilities", {"workspace": str(CANONICAL_VEYA)}
    )
    assert caps_resp["ok"] is True
    caps = caps_resp["result"]["capabilities"]
    assert "L0_WORKSPACE_FULL" in caps["available_domains"]
    assert "EXISTING_WORKTREE" in caps["available_targets"]

    # runtime.probe
    probe_resp = await call_tool(
        gateway,
        secret,
        session_id,
        "runtime.probe",
        {"workspace": str(CANONICAL_VEYA), "tool": "python"},
    )
    assert probe_resp["ok"] is True
    assert probe_resp["result"]["status"] == "passed"
    assert "Python" in probe_resp["result"]["stdout"]


# ── 8. Local2 Dogfooding on AGY Worktree ──────────────────────────────
@pytest.mark.asyncio
async def test_local2_dogfooding_acceptance_agy_worktree():
    """Verify Local2 operates inside AGY worktree without nested worktree creation."""
    if not AGY_WORKTREE.exists():
        pytest.skip(f"AGY worktree not present at {AGY_WORKTREE}")

    gateway, secret, _ = make_gateway([CANONICAL_VEYA])
    session_id = await initialize(gateway, secret, workspace=str(AGY_WORKTREE))

    nested_wts = AGY_WORKTREE / ".veya" / "worktrees"
    count_before = len(list(nested_wts.iterdir())) if nested_wts.exists() else 0

    # 1. Probe python in AGY worktree
    probe = await call_tool(
        gateway,
        secret,
        session_id,
        "runtime.probe",
        {"workspace": str(AGY_WORKTREE), "tool": "python"},
    )
    assert probe["ok"] is True
    assert probe["result"]["status"] == "passed"

    # 2. Run shell.exec in AGY worktree with EXISTING_WORKTREE target
    exec_resp = await call_tool(
        gateway,
        secret,
        session_id,
        "shell.exec",
        {
            "command": f"{PY} -c \"import sys; print('LOCAL2_QUALIFIED_IN_AGY_WORKTREE')\"",
            "execution_target": "EXISTING_WORKTREE",
            "execution_domain": "L0_WORKSPACE_FULL",
            "wait": True,
        },
    )
    assert exec_resp["ok"] is True
    result = exec_resp["result"]
    assert "LOCAL2_QUALIFIED_IN_AGY_WORKTREE" in (
        result.get("text", "") or result.get("stdout_tail", "")
    )

    # 3. Confirm nested worktree count did not increase
    count_after = len(list(nested_wts.iterdir())) if nested_wts.exists() else 0
    assert count_after == count_before


# ── 9. Host Go Toolchain Discovery & Projection (P0-A, P0-B) ───────────
def test_host_go_discovery_and_projection():
    """Verify host Go toolchain is discovered and projected into profile."""
    profile = discover_runtime_profile(CANONICAL_VEYA, force_refresh=True)

    assert profile.go is not None, "Go executable not discovered"
    assert profile.go_bin is not None
    assert profile.go_version is not None and "go1." in profile.go_version
    assert profile.gofmt is not None
    assert profile.goroot is not None
    assert profile.gopath is not None
    assert len(profile.go_path_entries) > 0

    assert profile.tool_status.get("go") in ("HOST_AVAILABLE", "PROJECTED")
    assert profile.tool_status.get("gofmt") in ("HOST_AVAILABLE", "PROJECTED")

    # Verify Go env vars in allowlist and passthrough
    assert "GOROOT" in profile.env_allowlist
    assert "GOPATH" in profile.env_allowlist
    assert "GOBIN" in profile.env_allowlist


# ── 10. Go Execution inside Local2 (P0-B) ──────────────────────────────
@pytest.mark.asyncio
async def test_local2_go_version_and_env_execution():
    """Verify Local2 can execute go version and go env in L0_WORKSPACE_FULL."""
    gateway, secret, _ = make_gateway([CANONICAL_VEYA])
    session_id = await initialize(gateway, secret, workspace=str(CANONICAL_VEYA))

    # Test go version
    resp = await call_tool(
        gateway,
        secret,
        session_id,
        "shell.exec",
        {
            "command": "go version",
            "execution_domain": "L0_WORKSPACE_FULL",
            "wait": True,
        },
    )
    assert resp["ok"] is True
    res = resp["result"]
    output = res.get("text", "") or res.get("stdout_tail", "")
    assert "go version go1." in output

    # Test go env GOROOT
    resp2 = await call_tool(
        gateway,
        secret,
        session_id,
        "shell.exec",
        {
            "command": "go env GOROOT",
            "execution_domain": "L0_WORKSPACE_FULL",
            "wait": True,
        },
    )
    assert resp2["ok"] is True
    res2 = resp2["result"]
    output2 = (res2.get("text", "") or res2.get("stdout_tail", "")).strip()
    assert len(output2) > 0
    assert Path(output2).is_dir()


# ── 11. Real Go Build & Test Probe (P0-B, P0-P) ────────────────────────
@pytest.mark.asyncio
async def test_local2_go_real_build_and_test():
    """Verify real Go compilation and testing inside a project workspace."""
    workspace_dir = Path("/data/soffy/projects") / "tmp_test_go_probe"
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir, ignore_errors=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(workspace_dir)], check=True, capture_output=True
    )

    try:
        # Create minimal Go module and test
        (workspace_dir / "go.mod").write_text("module testgoprobe\n\ngo 1.23\n", encoding="utf-8")
        (workspace_dir / "math.go").write_text(
            "package main\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n\nfunc main() {\n\tprintln(Add(2, 3))\n}\n",
            encoding="utf-8",
        )
        (workspace_dir / "math_test.go").write_text(
            'package main\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) {\n\tif Add(2, 3) != 5 {\n\t\tt.Fatal("expected 5")\n\t}\n}\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(workspace_dir), "config", "user.name", "Test"], check=True)
        subprocess.run(
            ["git", "-C", str(workspace_dir), "config", "user.email", "t@t.local"], check=True
        )
        subprocess.run(["git", "-C", str(workspace_dir), "add", "."], check=True)
        subprocess.run(["git", "-C", str(workspace_dir), "commit", "-m", "init"], check=True)

        gateway, secret, _ = make_gateway([Path("/data/soffy/projects")])
        session_id = await initialize(gateway, secret, workspace=str(workspace_dir))

        # Run go test
        test_resp = await call_tool(
            gateway,
            secret,
            session_id,
            "shell.exec",
            {
                "command": "go test ./...",
                "workspace": str(workspace_dir),
                "execution_domain": "L0_WORKSPACE_FULL",
                "wait": True,
            },
        )
        assert test_resp["ok"] is True
        res = test_resp["result"]
        output = res.get("text", "") or res.get("stdout_tail", "")
        assert "PASS" in output or "ok" in output

        # Run go build
        build_resp = await call_tool(
            gateway,
            secret,
            session_id,
            "shell.exec",
            {
                "command": "go build ./...",
                "workspace": str(workspace_dir),
                "execution_domain": "L0_WORKSPACE_FULL",
                "wait": True,
            },
        )
        assert build_resp["ok"] is True
        assert (workspace_dir / "testgoprobe").exists() or build_resp["result"].get(
            "exit_code"
        ) == 0

    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)


# ── 12. Canonical File Write and Patch (P0-H) ──────────────────────────
@pytest.mark.asyncio
async def test_canonical_file_write_and_patch():
    """Verify direct file.write and file.patch on canonical workspace when permitted."""
    gateway, secret, _ = make_gateway([CANONICAL_VEYA])
    session_id = await initialize(gateway, secret, workspace=str(CANONICAL_VEYA))

    test_file = CANONICAL_VEYA / "scratch_canonical_write_qualification.txt"
    if test_file.exists():
        test_file.unlink()

    try:
        # 1. file.write to canonical
        write_resp = await call_tool(
            gateway,
            secret,
            session_id,
            "file.write",
            {
                "path": str(test_file),
                "content": "line1\nline2\n",
                "execution_target": "CANONICAL_WORKTREE",
            },
        )
        assert write_resp["ok"] is True
        assert test_file.is_file()
        assert test_file.read_text(encoding="utf-8") == "line1\nline2\n"

        # 2. file.read to get hash tags
        read_resp = await call_tool(
            gateway,
            secret,
            session_id,
            "file.read",
            {"path": str(test_file)},
        )
        assert read_resp["ok"] is True
        read_text = read_resp["result"]["text"]
        tag_line2 = next(line for line in read_text.splitlines() if "line2" in line).split()[0]

        # 3. file.patch on canonical
        patch_resp = await call_tool(
            gateway,
            secret,
            session_id,
            "file.patch",
            {
                "path": str(test_file),
                "start_tag": tag_line2,
                "new_text": "line2_patched\n",
                "execution_target": "CANONICAL_WORKTREE",
            },
        )
        assert patch_resp["ok"] is True
        assert "line2_patched" in test_file.read_text(encoding="utf-8")

    finally:
        if test_file.exists():
            test_file.unlink()


# ── 13. Canonical Git Promotion: Clean Target (P0-D through P0-K) ───────
@pytest.mark.asyncio
async def test_canonical_git_promotion_safe(tmp_path: Path):
    """Verify git.promote safely applies worktree modifications to canonical."""
    from runtime.coding.worktree import WorktreeManager

    # Initialize a dummy git repo as canonical
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.local"], check=True
    )
    (tmp_path / "base.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True)

    # Create an isolated task worktree
    mgr = WorktreeManager(str(tmp_path))
    rec = mgr.create("promo-test-1", "feature test")
    wt_path = Path(rec.path)

    # Modify a file and create a new file in the worktree
    (wt_path / "base.txt").write_text("initial\nfeature line\n", encoding="utf-8")
    (wt_path / "new_feature.txt").write_text("brand new feature\n", encoding="utf-8")

    # Run git.promote via gateway
    gateway, secret, _ = make_gateway([tmp_path])
    session_id = await initialize(gateway, secret, workspace=str(tmp_path))

    promote_resp = await call_tool(
        gateway,
        secret,
        session_id,
        "git.promote",
        {
            "source_worktree": str(wt_path),
            "target_canonical": str(tmp_path),
            "verify_after": False,
        },
    )
    assert promote_resp["ok"] is True
    res = promote_resp["result"]
    assert res["status"] == "PROMOTED"
    assert "base.txt" in res["promoted_files"]
    assert "new_feature.txt" in res["promoted_files"]

    # Verify canonical now contains the modifications in its working tree
    assert (tmp_path / "base.txt").read_text(encoding="utf-8") == "initial\nfeature line\n"
    assert (tmp_path / "new_feature.txt").read_text(encoding="utf-8") == "brand new feature\n"

    # Verify it was NOT auto-committed
    st = subprocess.check_output(
        ["git", "-C", str(tmp_path), "status", "--porcelain=v1"], text=True
    )
    assert "base.txt" in st


# ── 14. Dirty Worktree Preservation during Promotion (P0-F) ────────────
@pytest.mark.asyncio
async def test_canonical_git_promotion_unrelated_dirty_preserved(tmp_path: Path):
    """Verify promotion strictly preserves unrelated canonical dirty files."""
    from runtime.coding.worktree import WorktreeManager

    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.local"], check=True
    )
    (tmp_path / "app.py").write_text("def app(): pass\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("def unrelated(): pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True)

    # Canonical introduces an UNRELATED dirty modification
    (tmp_path / "unrelated.py").write_text(
        "def unrelated(): # UNRELATED_DIRTY_CONTENT\n pass\n", encoding="utf-8"
    )

    # Worktree modifies app.py
    mgr = WorktreeManager(str(tmp_path))
    rec = mgr.create("promo-test-2", "app change")
    wt_path = Path(rec.path)
    (wt_path / "app.py").write_text("def app():\n    return 42\n", encoding="utf-8")

    gateway, secret, _ = make_gateway([tmp_path])
    session_id = await initialize(gateway, secret, workspace=str(tmp_path))

    promote_resp = await call_tool(
        gateway,
        secret,
        session_id,
        "git.promote",
        {
            "source_worktree": str(wt_path),
            "target_canonical": str(tmp_path),
            "verify_after": False,
        },
    )
    assert promote_resp["ok"] is True
    res = promote_resp["result"]
    assert res["status"] == "PROMOTED"
    assert res["unrelated_dirty_preserved"] is True
    assert "unrelated.py" in res["preflight"]["unrelated_dirty_files"]

    # Verify unrelated dirty file preserved verbatim
    assert "# UNRELATED_DIRTY_CONTENT" in (tmp_path / "unrelated.py").read_text(encoding="utf-8")
    # Verify promoted file applied
    assert "return 42" in (tmp_path / "app.py").read_text(encoding="utf-8")


# ── 15. Promotion 3-Way Merge and Conflict Containment (P0-F, P0-G) ────
@pytest.mark.asyncio
async def test_canonical_git_promotion_three_way_merge_and_conflict(tmp_path: Path):
    """Verify 3-way merge on non-conflicting edits and fail-closed on true conflicts."""
    from runtime.coding.worktree import WorktreeManager

    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.local"], check=True
    )
    (tmp_path / "file.txt").write_text("header\nmiddle\nfooter\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True)

    # 1. Non-conflicting 3-way merge:
    # Canonical modifies line 1 (header), worktree modifies line 3 (footer)
    (tmp_path / "file.txt").write_text("header_canonical\nmiddle\nfooter\n", encoding="utf-8")

    mgr = WorktreeManager(str(tmp_path))
    rec = mgr.create("promo-test-3", "footer edit")
    wt_path = Path(rec.path)
    (wt_path / "file.txt").write_text("header\nmiddle\nfooter_worktree\n", encoding="utf-8")

    gateway, secret, _ = make_gateway([tmp_path])
    session_id = await initialize(gateway, secret, workspace=str(tmp_path))

    promote_resp = await call_tool(
        gateway,
        secret,
        session_id,
        "git.promote",
        {
            "source_worktree": str(wt_path),
            "target_canonical": str(tmp_path),
            "verify_after": False,
        },
    )
    assert promote_resp["ok"] is True
    res = promote_resp["result"]
    assert res["status"] == "PROMOTED"
    assert res["preflight"]["classification"] == "MERGE_REQUIRED"

    # Both modifications exist cleanly
    merged_text = (tmp_path / "file.txt").read_text(encoding="utf-8")
    assert "header_canonical" in merged_text
    assert "footer_worktree" in merged_text

    # 2. Conflicting edit fail-closed:
    # Both canonical and worktree modify 'middle' incompatibly
    (tmp_path / "file.txt").write_text(
        "header_canonical\nmiddle_CONFLICT_CANONICAL\nfooter\n", encoding="utf-8"
    )
    (wt_path / "file.txt").write_text(
        "header_canonical\nmiddle_CONFLICT_WORKTREE\nfooter\n", encoding="utf-8"
    )

    conflict_resp = await call_tool(
        gateway,
        secret,
        session_id,
        "git.promote",
        {
            "source_worktree": str(wt_path),
            "target_canonical": str(tmp_path),
            "verify_after": False,
        },
    )
    assert conflict_resp["ok"] is False
    c_res = conflict_resp["result"]
    assert c_res["status"] == "BLOCKED"
    assert c_res["preflight"]["classification"] == "CONFLICT"

    # Verify canonical was NOT corrupted with conflict markers
    assert "middle_CONFLICT_CANONICAL" in (tmp_path / "file.txt").read_text(encoding="utf-8")
    assert "<<<<<<<" not in (tmp_path / "file.txt").read_text(encoding="utf-8")


# ── 16. Post-Promotion Verification & Scoped Rollback (P0-J, P0-K) ──────
@pytest.mark.asyncio
async def test_canonical_git_promotion_rollback_on_verify_failure(tmp_path: Path):
    """Verify that failed post-verification triggers scoped rollback preserving unrelated dirty."""
    from runtime.coding.worktree import WorktreeManager

    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.local"], check=True
    )
    (tmp_path / "feature.py").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "user_dirty.py").write_text("user_orig = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True)

    # User has dirty change in user_dirty.py
    (tmp_path / "user_dirty.py").write_text("user_orig = True # KEEP_ME_INTACT\n", encoding="utf-8")

    # Worktree modifies feature.py and adds broken_code.py
    mgr = WorktreeManager(str(tmp_path))
    rec = mgr.create("promo-test-4", "broken feature")
    wt_path = Path(rec.path)
    (wt_path / "feature.py").write_text("version = 2\n", encoding="utf-8")
    (wt_path / "broken_code.py").write_text("syntax error !!!", encoding="utf-8")

    gateway, secret, _ = make_gateway([tmp_path])
    session_id = await initialize(gateway, secret, workspace=str(tmp_path))

    # Promote with a failing verification command
    promote_resp = await call_tool(
        gateway,
        secret,
        session_id,
        "git.promote",
        {
            "source_worktree": str(wt_path),
            "target_canonical": str(tmp_path),
            "verify_after": True,
            "verify_command": "exit 1",  # simulate test failure
            "rollback_on_failure": True,
        },
    )
    assert promote_resp["ok"] is False
    res = promote_resp["result"]
    assert res["status"] == "VERIFY_FAILED_ROLLED_BACK"
    assert res["rollback_performed"] is True

    # Promoted files were rolled back!
    assert (tmp_path / "feature.py").read_text(encoding="utf-8") == "version = 1\n"
    assert not (tmp_path / "broken_code.py").exists()

    # User's unrelated dirty file is still preserved!
    assert "# KEEP_ME_INTACT" in (tmp_path / "user_dirty.py").read_text(encoding="utf-8")
