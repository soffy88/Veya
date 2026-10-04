"""G2 — the backend facade shell entitlement is a gate, not a default.

Two independent gates, both required:

* Gate A, ``VEYA_BACKEND_FACADE_ROOT`` — where the facade may execute.
* Gate B, ``VEYA_BACKEND_FACADE_ALLOW_SHELL`` — that it may execute at all.

Neither implies the other. A correct principal, a workspace inside the root and a
READ task contract are not consent: an unauthenticated endpoint that picks up
shell because someone set a path variable is exactly the implicit entitlement
this gate exists to prevent.

Case 4 runs the real dispatch. ``worker.dispatch`` is a write-class tool that
spawns a process, and a mock of the shell capability would not prove the gate
lets anything through — it would only prove the mock was called. The dispatch
below therefore reaches ExecutorRegistry, pre-admission, GoalRun pre-create and
a durable parent execution. Whether the child then succeeds depends on a real
opencode binary; what the gate is about is whether it is admitted at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from server.backends import (
    FACADE_PERMISSIONS,
    FACADE_PRINCIPAL,
    FACADE_ROOT_ENV,
    FACADE_SHELL_ENV,
    BackendRegistry,
    _facade_session,
    _shell_confirmed,
)

OPEN = "opencode"  # admitted by ExecutorRegistry
DENIED = "claude"  # not a canonical executor id


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "t"], check=True, capture_output=True
    )
    (path / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-qm", "init"], check=True, capture_output=True
    )
    return path


def _registry() -> BackendRegistry:
    import shutil
    import sys

    assert shutil.which(sys.executable)
    registry = BackendRegistry()
    registry.register(OPEN, "cli", command=[sys.executable])
    return registry


@pytest.fixture
def gates(monkeypatch, tmp_path):
    """Both gates off by default; each test opens only what it needs."""

    monkeypatch.delenv(FACADE_ROOT_ENV, raising=False)
    monkeypatch.delenv(FACADE_SHELL_ENV, raising=False)
    root = _repo(tmp_path / "repo")

    def open_root() -> Path:
        monkeypatch.setenv(FACADE_ROOT_ENV, str(root))
        return root

    def open_shell(value: str = "1") -> None:
        monkeypatch.setenv(FACADE_SHELL_ENV, value)

    return open_root, open_shell, root


# ── Case 1: root absent, shell absent → DENY ────────────────────────────────
@pytest.mark.asyncio
async def test_case1_no_root_no_shell_denies(gates):
    result = await _registry().run(OPEN, "hi", timeout_s=5)
    assert result["ok"] is False
    assert result["error_code"] == "EXECUTION_FACADE_CLOSED"
    assert result.get("execution_id") is None


# ── Case 2: root present, shell absent → DENY ───────────────────────────────
@pytest.mark.asyncio
async def test_case2_root_without_shell_gate_denies(gates):
    open_root, _open_shell, _root = gates
    open_root()
    result = await _registry().run(OPEN, "hi", timeout_s=5)
    assert result["ok"] is False
    assert result["error_code"] == "SHELL_NOT_AUTHORIZED"
    assert FACADE_SHELL_ENV in result["error"]
    assert result.get("execution_id") is None


# ── Case 3: shell present, root absent → DENY ──────────────────────────────
@pytest.mark.asyncio
async def test_case3_shell_gate_without_root_denies(gates):
    _open_root, open_shell, _root = gates
    open_shell()
    result = await _registry().run(OPEN, "hi", timeout_s=5)
    assert result["ok"] is False
    assert result["error_code"] == "EXECUTION_FACADE_CLOSED"
    assert result.get("execution_id") is None


# ── Case 4: both gates open, READ → real dispatch, admitted ────────────────
@pytest.mark.asyncio
async def test_case4_both_gates_allow_a_real_dispatch(gates):
    open_root, open_shell, root = gates
    open_root()
    open_shell()

    result = await _registry().run(OPEN, "say hello", cwd=str(root), timeout_s=1)

    assert result["ok"] is True, result
    payload = result["output"]
    assert payload.get("accepted") is True, payload
    assert payload.get("dispatch_id"), payload
    assert payload.get("goal_run_id"), payload
    assert payload.get("status") == "DISPATCHED", payload
    assert payload.get("child_execution_ids"), payload
    assert result["execution_id"] == payload["parent_execution_id"]


# ── Case 5: WRITE contract → DENY ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_case5_write_contract_is_refused(gates, monkeypatch):
    """The facade pins READ. A caller cannot escalate the contract."""
    open_root, open_shell, root = gates
    open_root()
    open_shell()

    seen: list[dict] = []
    import veya.remote.tool_adapter as adapter_module

    original = adapter_module.RemoteToolAdapter.call

    async def _spy(self, session, name, arguments):
        seen.append(arguments or {})
        return await original(self, session, name, arguments)

    monkeypatch.setattr(adapter_module.RemoteToolAdapter, "call", _spy)

    await _registry().run(OPEN, "hi", cwd=str(root), timeout_s=1)

    assert seen, "the dispatch never reached the adapter"
    contract = seen[0]["tasks"][0]["task_contract"]
    assert contract == {"task_kind": "READ"}, contract


# ── Case 6: target outside root → DENY ────────────────────────────────────
@pytest.mark.asyncio
async def test_case6_target_outside_root_denies(gates, tmp_path):
    open_root, open_shell, _root = gates
    open_root()
    open_shell()
    outside = tmp_path / "outside"
    outside.mkdir()

    result = await _registry().run(OPEN, "hi", cwd=str(outside), timeout_s=5)
    assert result["ok"] is False
    assert result["error_code"] == "WORKSPACE_DENIED"


# ── Case 7: malformed target → DENY ───────────────────────────────────────
@pytest.mark.asyncio
async def test_case7_malformed_target_denies(gates):
    open_root, open_shell, root = gates
    open_root()
    open_shell()

    result = await _registry().run(OPEN, "hi", cwd=str(root / "nope" / "deep"), timeout_s=5)
    assert result["ok"] is False
    assert result["error_code"] == "WORKSPACE_DENIED"
    assert "does not exist" in result["error"]


# ── Case 8: admission reject → DENY ───────────────────────────────────────
@pytest.mark.asyncio
async def test_case8_admission_reject_denies(gates):
    """An executor outside the registry never reaches either gate."""
    open_root, open_shell, root = gates
    open_root()
    open_shell()

    result = await _registry().run(DENIED, "hi", cwd=str(root), timeout_s=5)
    assert result["ok"] is False
    assert result["error_code"] == "EXECUTOR_NOT_ADMITTED"


# ── Gate mechanics ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("yes", False),
        ("true-ish", False),
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("1 ", True),
    ],
)
def test_only_an_explicit_affirmative_opens_gate_b(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(FACADE_SHELL_ENV, raising=False)
    else:
        monkeypatch.setenv(FACADE_SHELL_ENV, raw)
    assert _shell_confirmed() is expected


def test_shell_entitlement_follows_gate_b(gates):
    open_root, open_shell, root = gates

    open_root()
    assert _facade_session(root).permissions.shell is False
    open_shell()
    assert _facade_session(root).permissions.shell is True


def test_gate_b_does_not_widen_the_canonical_grant(gates):
    """§5.3: the maximum stays canonical however the gates are set."""
    open_root, open_shell, root = gates
    open_root()
    open_shell()

    session = _facade_session(root)
    assert session.principal == FACADE_PRINCIPAL
    granted = {
        n
        for n in ("read", "write", "shell", "git", "destructive", "network")
        if getattr(session.permissions, n)
    }
    assert granted <= set(FACADE_PERMISSIONS), granted
    for escalated in ("git", "destructive", "network"):
        assert getattr(session.permissions, escalated) is False, escalated
