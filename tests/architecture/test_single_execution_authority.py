"""There is one execution authority, and the backend facade is not a second one.

`POST /api/v1/backends/run` used to reach ``server.engine_runner.run_engine``
directly: an L2 CLI process spawned with no ExecutorRegistry admission, no
permission check, no GoalRun, no receipt and no worktree binding, from an
endpoint with no authentication and a caller-chosen ``cwd``. Together with
``POST /api/v1/backends/register`` (which accepted an arbitrary ``command``)
that was an unauthenticated arbitrary-executable surface.

These tests pin the closed shape. The receipt gate is a separate, still-unmet
requirement and is marked, not faked — see
``test_backend_run_emits_execution_receipt``.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from server.backends import (
    FACADE_PERMISSIONS,
    FACADE_PRINCIPAL,
    FACADE_ROOT_ENV,
    BackendRegistry,
    _facade_session,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _init_repo(path: Path) -> None:
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


def _an_available_command() -> list[str]:
    """A command that passes BackendSpec.available(), without spawning anything."""

    assert shutil.which(sys.executable), "the running interpreter must be resolvable"
    return [sys.executable]


def test_no_backend_direct_executor_path():
    """No backend module may import or call the L2 launcher directly.

    Checked over the AST rather than the raw text so that a docstring naming the
    old call site is not mistaken for a call to it.
    """

    for rel in ("server/backends.py", "server/routes/backends.py"):
        tree = ast.parse(_source(rel), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                modules = []
            for module in modules:
                root = module.split(".")[0]
                assert not (root == "server" and "engine_runner" in module), (
                    f"{rel}:{node.lineno} imports the engine runner"
                )
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                assert name != "run_engine", f"{rel}:{node.lineno} calls run_engine directly"


@pytest.mark.asyncio
async def test_backend_run_requires_registry_admission(monkeypatch):
    """A cli facade request is answered by ExecutorRegistry, and spawns nothing."""

    import server.engine_runner as engine_runner

    def _must_not_run(*args, **kwargs):
        raise AssertionError("backend facade called run_engine: second execution path")

    monkeypatch.setattr(engine_runner, "run_engine", _must_not_run)

    registry = BackendRegistry()

    # A name that is not a canonical executor id is refused by the registry.
    registry.register("claude", "cli", command=_an_available_command())
    result = await registry.run("claude", "hi", timeout_s=10)
    assert result["ok"] is False
    assert result["error_code"] == "EXECUTOR_NOT_ADMITTED"
    assert result["canonical_executor_id"] == "claude"

    # An admitted executor still fails closed: no service principal is bound
    # here, so the facade must not invent authority. It names the real entry.
    registry.register("opencode", "cli", command=_an_available_command())
    result = await registry.run("opencode", "hi", timeout_s=10)
    assert result["ok"] is False
    assert result["error_code"] == "EXECUTION_FACADE_CLOSED"
    assert result["canonical_entry"] == "worker.dispatch"
    assert result["canonical_executor_id"] == "opencode"

    # A retired executor is refused by the registry, not substituted.
    registry.register("hicode", "cli", command=_an_available_command())
    result = await registry.run("hicode", "hi", timeout_s=10)
    assert result["ok"] is False
    assert result["error_code"] == "EXECUTOR_RETIRED"

    # Every refusal reports the admitted set, so a caller can recover.
    assert "opencode" in result["admitted_executors"]


def test_backend_run_does_not_expose_spawning_registration_over_http():
    """A caller may not choose a spawned executable through the HTTP registry."""

    from fastapi.testclient import TestClient

    client = TestClient(__import__("server.app", fromlist=["app"]).app)

    for kind in ("acp", "cli"):
        r = client.post(
            "/api/v1/backends/register",
            json={"name": f"evil-{kind}", "kind": kind, "command": ["/bin/sh", "-c", "id"]},
        )
        assert r.status_code == 400, f"kind={kind} must not be HTTP-registrable"
        assert "worker.dispatch" in r.json()["detail"]

    # Non-spawning kinds still work, so the registry keeps its read/use surface.
    r = client.post("/api/v1/backends/register", json={"name": "facade-builtin", "kind": "builtin"})
    assert r.status_code == 200 and r.json()["status"] == "registered"

    # An unknown kind is still rejected for its own reason.
    r = client.post("/api/v1/backends/register", json={"name": "bad", "kind": "nope"})
    assert r.status_code == 400


def test_discover_does_not_seed_model_with_engine_name():
    """A discovered cli backend must not hand its own name back as a model id."""

    registry = BackendRegistry()
    for spec in registry.discover():
        if spec.kind == "cli":
            assert spec.model == "", f"{spec.name} seeds model={spec.model!r}"


@pytest.mark.asyncio
async def test_backend_run_emits_execution_receipt(tmp_path, monkeypatch):
    """A facade run that reaches the canonical chain answers with a receipt.

    This gate could not be met while the facade was closed, because nothing ran
    and so no receipt could exist. It asserts the real success path rather than
    "either it worked or it had an error code", which would pass vacuously.

    ``opencode`` is admitted by ExecutorRegistry, so the dispatch proceeds far
    enough to create a durable parent execution — whether the child then
    succeeds depends on a real opencode binary being present, which is not what
    this gate is about.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    monkeypatch.setenv(FACADE_ROOT_ENV, str(root))

    registry = BackendRegistry()
    registry.register("opencode", "cli", command=_an_available_command())

    result = await registry.run("opencode", "say hello", timeout_s=1)

    assert result["ok"] is True, result
    # The receipt is the canonical one, not a facade invention: a parent
    # execution, a GoalRun, a dispatch id and a durable child.
    assert result.get("execution_id"), result
    payload = result["output"]
    assert payload.get("accepted") is True, payload
    assert payload.get("dispatch_id"), payload
    assert payload.get("goal_run_id"), payload
    assert payload.get("parent_execution_id") == result["execution_id"], payload
    assert payload.get("status") == "DISPATCHED", payload
    assert payload.get("child_execution_ids"), payload


@pytest.mark.asyncio
async def test_facade_is_closed_until_an_operator_declares_a_root(tmp_path, monkeypatch):
    monkeypatch.delenv(FACADE_ROOT_ENV, raising=False)
    registry = BackendRegistry()
    registry.register("opencode", "cli", command=_an_available_command())

    result = await registry.run("opencode", "hi", timeout_s=5)

    assert result["ok"] is False
    assert result["error_code"] == "EXECUTION_FACADE_CLOSED"
    assert FACADE_ROOT_ENV in result["error"]
    assert result.get("execution_id") is None


@pytest.mark.asyncio
async def test_facade_refuses_a_workspace_outside_the_declared_root(tmp_path, monkeypatch):
    monkeypatch.setenv(FACADE_ROOT_ENV, str(tmp_path / "allowed"))
    (tmp_path / "allowed").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    registry = BackendRegistry()
    registry.register("opencode", "cli", command=_an_available_command())

    result = await registry.run("opencode", "hi", cwd=str(outside), timeout_s=5)

    assert result["ok"] is False
    assert result["error_code"] == "WORKSPACE_DENIED"
    assert FACADE_ROOT_ENV in result["error"]


@pytest.mark.asyncio
async def test_facade_session_grant_is_exactly_the_measured_minimum():
    """The grant is a capability to dispatch, not an effect grant on the task.

    Measured against the permission engine: read alone is refused for lacking
    write, read+write for lacking shell, and read+write+shell is admitted. The
    test pins that set so a future edit cannot quietly widen it, and asserts the
    three escalations that stay off.
    """
    session = _facade_session(Path("/tmp"))
    assert session.principal == FACADE_PRINCIPAL
    granted = {name for name in FACADE_PERMISSIONS if getattr(session.permissions, name)}
    assert granted == set(FACADE_PERMISSIONS)
    for escalated in ("git", "destructive", "service_control", "network"):
        assert getattr(session.permissions, escalated) is False, escalated


@pytest.mark.asyncio
async def test_retired_and_unknown_executors_still_fail_before_delegation(tmp_path, monkeypatch):
    """Admission happens first, so a bad name never reaches the workspace check."""
    monkeypatch.setenv(FACADE_ROOT_ENV, str(tmp_path))
    registry = BackendRegistry()
    registry.register("hicode", "cli", command=_an_available_command())
    registry.register("claude", "cli", command=_an_available_command())

    assert (await registry.run("hicode", "hi", timeout_s=5))["error_code"] == "EXECUTOR_RETIRED"
    assert (await registry.run("claude", "hi", timeout_s=5))[
        "error_code"
    ] == "EXECUTOR_NOT_ADMITTED"
