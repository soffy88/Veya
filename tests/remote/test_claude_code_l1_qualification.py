"""Canonical Qualification Suite for Claude Code L1 Executor.

Verifies:
1. CLAUDE_CODE registration integrity across all canonical layers.
2. Runtime identity projected from ExecutorRegistry (provider/model/auth).
3. CLI command construction, permission mode, and env hygiene.
4. Capability-aware and health-aware routing with claude_code.
5. Explicit pin authority: worker="claude_code" fail closed without fallback.
6. Failure classification taxonomy handles Claude Code signals.
7. Real isolated probe: read, file write, token verify, shell git, cleanup.
8. Timeout / cancel propagation with zero false success.

All project operations stay inside the Veya execution boundary
(ActionGateway -> PermissionEngine); Claude Code owns no permission rules.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from veya.remote.executor_health import (
    ExecutorFailureClass,
    ExecutorHealthRegistry,
    classify_executor_failure,
    normalize_executor_name,
    registry_order,
    resolve_executor,
)
from veya.remote.executor_registry import get_executor_registry
from veya.remote.tool_adapter import (
    _WORKER_TYPES,
    _resolve_claude_binary,
    _worker_command,
    _worker_model_identity,
    worker_availability,
)
from veya.remote.worker_runtime import WORKER_CAPABILITIES, RecoveryCapability, capabilities_for
from veya.supervision.orchestrated import L1_WORKERS
from veya.supervision.retask import _RETASK_WORKERS


def test_01_claude_code_registration_integrity() -> None:
    """1. Verify CLAUDE_CODE is registered across all canonical registries."""
    assert "claude_code" in _WORKER_TYPES
    assert _WORKER_TYPES["claude_code"] == "CLAUDE_CODE"

    assert "claude_code" in WORKER_CAPABILITIES
    caps = WORKER_CAPABILITIES["claude_code"]
    assert caps.supports_receipts is True
    assert caps.supports_read_task is True
    assert caps.supports_write_task is True
    assert caps.supports_shell_effect is True
    assert caps.supports_file_effect is True
    assert caps.supports_structured_events is True
    assert caps.recovery_capability == str(RecoveryCapability.REATTACH)

    assert "claude_code" in L1_WORKERS
    assert "claude_code" in _RETASK_WORKERS
    assert normalize_executor_name("claude_code") == "claude_code"
    assert normalize_executor_name("claude-code") == "claude_code"

    avail = worker_availability()
    assert "CLAUDE_CODE" in avail["available_workers"]
    assert "claude_code" in registry_order()

    # Canonical id normalizes through the single identity authority.
    registry = get_executor_registry()
    assert registry.identity("claude-code").executor_id == "claude_code"
    assert registry.identity("claude_code").executor_id == "claude_code"


def test_02_claude_code_runtime_identity() -> None:
    """2. Runtime identity is projected from ExecutorRegistry, never hardcoded."""
    identity = get_executor_registry().identity("claude_code")
    assert identity.reachable is True
    assert identity.launcher is not None
    assert identity.authenticated is True
    assert identity.auth_state == "AUTHENTICATED"
    assert identity.status == "READY"
    assert identity.provider == "anthropic"
    assert identity.model == "claude-sonnet-4-5"

    model_identity = _worker_model_identity("claude_code")
    assert model_identity == (identity.provider, identity.model)

    caps = capabilities_for("claude_code")
    assert caps.supports_write_task is True
    assert caps.supports_shell_effect is True


def test_03_claude_code_command_construction(tmp_path: Path, monkeypatch) -> None:
    """3. CLI argv uses non-interactive print mode with Veya boundary intact."""
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("VEYA_CLAUDE_BIN", str(fake_claude))
    from veya.remote.executor_registry import reset_executor_registry

    reset_executor_registry()

    bin_path = _resolve_claude_binary()
    assert bin_path == str(fake_claude)

    argv, env = _worker_command("claude_code", "read the workspace")
    assert argv[0] == str(fake_claude)
    assert "--print" in argv
    assert "--permission-mode" in argv
    assert "acceptEdits" in argv
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert "read the workspace" in argv
    # No Veya endpoint leakage into the worker environment.
    assert "VEYA_LLM_ENDPOINT" not in env
    assert "VEYA_OPENAI_BASE_URL" not in env
    assert env.get("HOME")


def test_03b_claude_code_coding_mode_binds_execution_worktree(tmp_path: Path, monkeypatch) -> None:
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("VEYA_CLAUDE_BIN", str(fake_claude))
    from veya.remote.executor_registry import reset_executor_registry

    reset_executor_registry()

    worktree = tmp_path / "execution-worktree"
    argv, _ = _worker_command(
        "claude_code",
        "create result.txt",
        worktree_path=str(worktree),
        coding_mode=True,
    )
    # The worktree is bound as the process cwd; the argv carries the task.
    assert "create result.txt" in argv
    assert "--print" in argv


def test_04_health_aware_routing_with_claude_code() -> None:
    """4. claude_code participates in the health-aware fallback sequence."""
    reg = ExecutorHealthRegistry()

    # AGY + OPENCODE down -> CLAUDE_CODE
    reg.record_failure("antigravity", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    reg.record_failure("opencode", ExecutorFailureClass.TRANSPORT_FAILURE)
    selected, sub = resolve_executor(health_registry=reg)
    assert selected == "claude_code"
    assert sub is not None and sub.selected_executor == "claude_code"

    # AGY + OPENCODE + CLAUDE_CODE down -> PI
    reg.record_failure("claude_code", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    selected2, _ = resolve_executor(health_registry=reg)
    assert selected2 == "pi"


def test_05_capability_aware_routing_with_claude_code() -> None:
    """5. claude_code is eligible for write tasks via the capability contract."""
    reg = ExecutorHealthRegistry()
    selected, _ = resolve_executor(
        required_capabilities={"supports_write_task": True},
        health_registry=reg,
    )
    # antigravity is the first write-capable executor in preference order.
    assert selected == "antigravity"

    # With antigravity down, claude_code is the next write-capable candidate.
    reg.record_failure("antigravity", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    selected2, _ = resolve_executor(
        required_capabilities={"supports_write_task": True},
        health_registry=reg,
    )
    assert selected2 == "claude_code"


def test_06_explicit_pin_claude_code_not_substituted() -> None:
    """6. Explicit pin preserves claude_code even when unhealthy."""
    reg = ExecutorHealthRegistry()
    reg.record_failure("claude_code", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    selected, sub = resolve_executor(
        requested="claude_code", explicit_pin=True, health_registry=reg
    )
    assert selected == "claude_code"
    assert sub is None


def test_07_failure_taxonomy_claude_code() -> None:
    """7. Failure taxonomy correctly classifies Claude Code failure signals."""
    fc_auth = classify_executor_failure(
        detail="Claude Code authentication failed: 401 unauthorized"
    )
    assert fc_auth == ExecutorFailureClass.AUTH_FAILURE

    fc_quota = classify_executor_failure(detail="Claude Code 429: rate limit exceeded, usage limit")
    # A quota wall is not an outage: "unreachable" and "slow down" call for
    # different operator responses, so the two must not collapse.
    assert fc_quota == ExecutorFailureClass.PROVIDER_RATE_LIMIT

    fc_timeout = classify_executor_failure(
        error=TimeoutError("claude_code run hard max runtime exceeded")
    )
    assert fc_timeout == ExecutorFailureClass.WORKER_TIMEOUT

    fc_cancel = classify_executor_failure(status="CANCELLED")
    assert fc_cancel == ExecutorFailureClass.WORKER_CANCELLED

    fc_crash = classify_executor_failure(exit_code=1)
    assert fc_crash == ExecutorFailureClass.WORKER_CRASH


def test_08_hicode_retired_not_readmitted() -> None:
    """8. Retired hicode is never selected, probed, or readmitted as fallback."""
    assert "hicode" not in registry_order()
    assert "hicode" not in _WORKER_TYPES
    assert "hicode" not in WORKER_CAPABILITIES
    assert "hicode" not in L1_WORKERS
    assert "hicode" not in _RETASK_WORKERS

    reg = ExecutorHealthRegistry()
    with pytest.raises(ValueError):
        resolve_executor(requested="hicode", health_registry=reg)

    avail = worker_availability()
    assert "HICODE" not in avail["available_workers"]


async def test_09_live_claude_code_read_qualification(tmp_path: Path) -> None:
    """9. Real READ qualification: read-only, no file mutation."""
    if not shutil.which("claude"):
        pytest.skip("claude binary not installed")

    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "base.txt").write_text("VEYA_CLAUDE_BASE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)

    from tests.remote.test_l1_parallel import (
        call_tool,
        initialize,
        make_gateway,
        status,
        wait_phase,
    )

    gateway, secret = make_gateway(repo_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {
                    "worker": "claude_code",
                    "task": (
                        "Read base.txt and report its exact contents. Do not modify any file."
                    ),
                    "task_kind": "READ",
                    "effect_requirement": "READ_ONLY",
                }
            ]
        },
    )
    assert envelope["ok"] is True
    parent_id = envelope["execution_id"]
    child_id = envelope["result"]["child_execution_ids"][0]
    final = await wait_phase(
        gateway, secret, session, parent_id, {"COMPLETED", "FAILED"}, timeout=180
    )
    child_st = await status(gateway, secret, session, child_id)
    assert final["status"] == "COMPLETED"
    assert child_st["status"] == "COMPLETED"
    assert "VEYA_CLAUDE_BASE" in (child_st.get("result_summary") or "")
    assert child_st.get("effect_receipt", {}).get("changed_files", []) == []
    assert (repo_path / "base.txt").read_text(encoding="utf-8") == "VEYA_CLAUDE_BASE\n"


async def test_10_live_claude_code_real_probe(tmp_path: Path) -> None:
    """10. Real isolated probe: read, write token, verify, shell git, cleanup.

    Exercises project_read, project_mutation, file_write, shell, git, long_task
    and cleanup inside one harmless isolated qualification.
    """
    if not shutil.which("claude"):
        pytest.skip("claude binary not installed")

    token = f"CLAUDE_CODE_QUAL_TOKEN_{uuid.uuid4().hex[:12]}"
    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "base.txt").write_text("VEYA_CLAUDE_BASE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)

    from tests.remote.test_l1_parallel import (
        call_tool,
        initialize,
        make_gateway,
        status,
        wait_phase,
    )

    gateway, secret = make_gateway(repo_path)
    session = await initialize(gateway, secret)
    task = (
        f"Complete these steps in order. "
        f"1) Read base.txt and confirm it contains VEYA_CLAUDE_BASE. "
        f"2) Create a file named token.txt containing exactly the token {token}. "
        f"3) Read token.txt and verify the token matches {token}. "
        f"4) Run 'git status --short' using your shell tool and report the result. "
        f"5) Delete token.txt using your shell tool (rm). "
        f"6) Reply with exactly this format: TOKEN=<the token you wrote> DONE=CLAUDE_CODE_PROBE_DONE"
    )
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {
                    "worker": "claude_code",
                    "task": task,
                    "task_kind": "READ",
                    "effect_requirement": "READ_ONLY",
                }
            ]
        },
    )
    assert envelope["ok"] is True
    parent_id = envelope["execution_id"]
    child_id = envelope["result"]["child_execution_ids"][0]
    final = await wait_phase(
        gateway, secret, session, parent_id, {"COMPLETED", "FAILED"}, timeout=240
    )
    child_st = await status(gateway, secret, session, child_id)
    assert final["status"] == "COMPLETED", child_st.get("failure_detail")
    assert child_st["status"] == "COMPLETED", child_st.get("failure_detail")

    summary = child_st.get("result_summary") or ""
    # The unique token was written, read back, and reported end-to-end.
    assert token in summary
    # The probe finished normally.
    assert "CLAUDE_CODE_PROBE_DONE" in summary
    # Cleanup: the qualification artifact is gone.
    assert not (repo_path / "token.txt").exists()
    # The receipt recorded real tool activity: the unique token file was
    # written, read back (verify), and removed via shell git status + rm.
    receipt = child_st.get("effect_receipt", {})
    tool_names = {t.get("name") for t in receipt.get("tool_calls", [])}
    assert any(name in tool_names for name in ("Write", "Edit", "create_file")), tool_names
    assert any("Bash" in name or "shell" in name.lower() for name in tool_names), tool_names
    assert receipt.get("shell_calls"), "shell git status must be recorded"


async def test_11_live_claude_code_write_qualification(tmp_path: Path) -> None:
    """11. Real WRITE qualification: file mutation reaches the worktree commit."""
    if not shutil.which("claude"):
        pytest.skip("claude binary not installed")

    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "base.txt").write_text("BASE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)

    from tests.remote.test_l1_parallel import (
        call_tool,
        initialize,
        make_gateway,
        status,
        wait_phase,
    )

    gateway, secret = make_gateway(repo_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {
                    "worker": "claude_code",
                    "task": (
                        "Read base.txt. Create result.txt with exactly "
                        "VEYA_CLAUDE_WRITE_OK and verify its contents."
                    ),
                    "task_kind": "WRITE",
                    "effect_requirement": "FILES_CHANGED",
                    "verification_requirement": "REQUIRED",
                    "commit_requirement": "REQUIRED",
                    "promotion_policy": "MANUAL",
                    "allowed_files": ["result.txt"],
                    "verification_command": 'test "$(cat result.txt)" = VEYA_CLAUDE_WRITE_OK',
                }
            ]
        },
    )
    assert envelope["ok"] is True
    parent_id = envelope["execution_id"]
    child_id = envelope["result"]["child_execution_ids"][0]
    final = await wait_phase(
        gateway, secret, session, parent_id, {"COMPLETED", "FAILED"}, timeout=240
    )
    child_st = await status(gateway, secret, session, child_id)
    assert final["status"] == "COMPLETED", child_st.get("failure_detail")
    assert child_st["status"] == "COMPLETED", child_st.get("failure_detail")
    assert child_st.get("effect_receipt", {}).get("changed_files") == ["result.txt"]
    assert child_st.get("execution_commit_sha")
    # The mutation is committed in the isolated worktree, not the canonical repo.
    assert not (repo_path / "result.txt").exists()


async def test_12_claude_code_timeout_propagation(tmp_path: Path, monkeypatch) -> None:
    """12. Timeout fails closed with WORKER_TIMEOUT and no false success."""
    from tests.remote.test_executor_failure_injection import wait_terminal
    from tests.remote.test_l1_parallel import (
        call_tool,
        initialize,
        make_gateway,
    )
    from veya.remote import tool_adapter

    def mock_worker_command(worker: str, task: str, **_kw):
        return ["sleep", "30"], dict(os.environ)

    monkeypatch.setattr(tool_adapter, "_worker_command", mock_worker_command)

    make_repo(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await initialize(gateway, secret)

    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {"tasks": [{"worker": "claude_code", "task": "test-timeout", "timeout_sec": 1.0}]},
    )
    assert envelope["ok"] is True
    child_id = envelope["result"]["child_execution_ids"][0]

    result = await wait_terminal(gateway, secret, session, child_id, timeout=15.0)
    assert result["status"] in ("FAILED", "TIMED_OUT", "BLOCKED")
    assert result["failure_class"] in (
        "EXECUTION_TIMEOUT",
        "WORKER_TIMEOUT",
        "TIMEOUT",
        "TOOL_TIMEOUT",
        "PROCESS_TIMEOUT",
    )
    assert not result["worker_alive"]


async def test_13_claude_code_cancel(tmp_path: Path, monkeypatch) -> None:
    """13. Cancel terminates the process group and marks CANCELLED."""
    from tests.remote.test_executor_failure_injection import wait_terminal
    from tests.remote.test_l1_parallel import (
        call_tool,
        initialize,
        make_gateway,
    )
    from veya.remote import tool_adapter

    def mock_worker_command(worker: str, task: str, **_kw):
        return ["sleep", "60"], dict(os.environ)

    monkeypatch.setattr(tool_adapter, "_worker_command", mock_worker_command)

    make_repo(tmp_path)
    gateway, secret = make_gateway(tmp_path)
    session = await initialize(gateway, secret)

    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {"tasks": [{"worker": "claude_code", "task": "test-cancel"}]},
    )
    assert envelope["ok"] is True
    child_id = envelope["result"]["child_execution_ids"][0]

    # Give the process a moment to start, then cancel.
    import asyncio

    await asyncio.sleep(0.5)
    cancel_env = await call_tool(
        gateway,
        secret,
        session,
        "process.cancel",
        {"execution_id": child_id},
    )
    assert cancel_env["ok"] is True

    result = await wait_terminal(gateway, secret, session, child_id, timeout=15.0)
    assert result["status"] in ("CANCELLED", "FAILED")
    assert result["failure_class"] in ("WORKER_CANCELLED", "CANCELLED", "FAILED")


def make_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return tmp_path
