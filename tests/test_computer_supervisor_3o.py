"""PR-10 Computer Supervisor contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from veya.platform import load

obase = load("obase")
oprim = load("oprim")
oskill = load("oskill")
omodul = load("omodul")
oservi = load("oservi")


@pytest.fixture(autouse=True)
def _clean_computer_runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VEYA_SANDBOX_PROFILE", "local")
    from oprim.computer import _reset_computer_runtime

    _reset_computer_runtime()
    yield
    _reset_computer_runtime()


def _profile(tmp_path: Path, *, backend: str = "local"):
    return obase.ComputerProfile(
        id=f"{backend}-test",
        backend=backend,
        workspace=str(tmp_path / "worktree"),
        image="python:3.11-slim" if backend == "docker" else None,
    )


@pytest.mark.asyncio
async def test_local_computer_lifecycle_is_idempotent(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    created = oprim.computer_create(profile)
    assert created["ok"] is True
    assert created["status"] == "created"

    started = oprim.computer_start(created["handle"])
    assert started["status"] == "running"

    status = oprim.computer_status(started["handle"])
    assert status["status"] == "running"
    attached = oprim.computer_attach(status["handle"])
    assert attached["status"] == "attached"
    stopped = oprim.computer_stop(attached["handle"])
    assert stopped["status"] == "stopped"
    restarted = oprim.computer_start(stopped["handle"])
    assert restarted["status"] == "running"
    reset = oprim.computer_reset(restarted["handle"])
    assert reset["status"] == "running"


def test_computer_stop_and_reset_retain_caller_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    marker = workspace / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")
    created = oprim.computer_create(_profile(tmp_path))
    stopped = oprim.computer_stop(created["handle"])
    assert stopped["ok"] is True
    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_unknown_backend_is_rejected_without_remote_worker() -> None:
    result = oprim.computer_create(
        {"id": "remote", "backend": "remote", "workspace": "/tmp/remote"}
    )
    assert result["ok"] is False
    assert "remote" in result["error"]


def test_readiness_is_stateless() -> None:
    handle = obase.ComputerHandle(
        computer_id="computer-1",
        profile_id="local",
        backend="local",
        workspace=None,
        sandbox_id="sandbox-1",
        state="running",
    )
    ready = oskill.evaluate_computer_readiness(handle)
    not_ready = oskill.evaluate_computer_readiness(handle, status={"status": "stopped"})
    assert ready["ready"] is True
    assert not_ready["ready"] is False


@pytest.mark.asyncio
async def test_prepare_transaction_runs_create_start_status_readiness(tmp_path: Path) -> None:
    events: list[str] = []
    engine = oservi.ComputerSupervisorEngine(
        computer_create=oprim.computer_create,
        computer_start=oprim.computer_start,
        computer_status=oprim.computer_status,
        computer_attach=oprim.computer_attach,
        computer_stop=oprim.computer_stop,
        computer_reset=oprim.computer_reset,
        readiness_evaluator=oskill.evaluate_computer_readiness,
        prepare_computer_session=omodul.prepare_computer_session,
        trigger={"on_demand": True},
        config={},
        name="test-computer",
    )
    engine.run()
    result = await engine.prepare(
        _profile(tmp_path),
        attach=True,
        output_dir=tmp_path / "outputs",
        on_step=lambda step: events.append(str(step["event"])),
    )
    assert result["status"] == "completed"
    assert result["prepared"] is True
    assert result["computer"]["state"] == "attached"
    assert events == [
        "computer_create",
        "computer_start",
        "computer_status",
        "computer_readiness",
        "computer_attach",
    ]


@pytest.mark.asyncio
async def test_prepare_fails_closed_when_readiness_fails(tmp_path: Path) -> None:
    calls: list[str] = []

    def create(_profile):
        calls.append("create")
        return {"ok": True, "handle": {"computer_id": "c", "state": "created"}}

    def start(handle):
        calls.append("start")
        return {"ok": True, "handle": {**handle, "state": "running"}}

    def status(handle):
        calls.append("status")
        return {"ok": True, "handle": handle, "status": "running"}

    def readiness(_handle, *, status):
        calls.append("readiness")
        return {"ready": False, "reason": "not ready"}

    result = await omodul.prepare_computer_session(
        omodul.PrepareComputerSessionConfig(),
        omodul.PrepareComputerSessionInput(
            profile=_profile(tmp_path),
            computer_create=create,
            computer_start=start,
            computer_status=status,
            readiness_evaluator=readiness,
        ),
        tmp_path / "outputs",
    )
    assert result["status"] == "failed"
    assert result.get("prepared") is not True
    assert calls == ["create", "start", "status", "readiness"]


def test_supervisor_injection_contract_and_no_remote_worker() -> None:
    points = oservi.ComputerSupervisorEngine.injection_points
    for name in (
        "computer_create",
        "computer_start",
        "computer_status",
        "computer_attach",
        "computer_stop",
        "computer_reset",
    ):
        assert points[name].kind == "oprim"
        assert points[name].cardinality == "1"
    assert points["readiness_evaluator"].kind == "oskill"
    assert points["prepare_computer_session"].kind == "omodul"
    assert "computer_supervisor" in oservi.list_skeletons()


def test_veya_adapter_reuses_coding_profile() -> None:
    from server.computer_supervisor_adapter import _computer_profile

    profile = _computer_profile("/tmp/veya-worktree", "docker_python")
    assert profile.backend == "docker"
    assert profile.image == "veya/python-dev:latest"
    assert profile.block_network is True
