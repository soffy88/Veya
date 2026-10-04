"""Asking to wait for a result means not getting a receipt for the submission.

``wait=True`` / ``wait_timeout_s`` opt into blocking for a terminal result. When
the wait returned with the execution still running, the handler fell straight
past its terminal-failure branch and answered ``ok=True`` with
``accepted=True`` and a non-terminal snapshot. The caller had asked to wait for a
result and received confirmation that the work was submitted, with nothing in
the payload distinguishing the two.

These tests drive the real code path with a runner that outlives the wait budget,
so the red case is the behaviour itself and not a mock's return value.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.tool_adapter import RemoteToolAdapter


def _repo(path: Path) -> None:
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


def _session(path: Path) -> RemoteSession:
    now = time.time()
    return RemoteSession(
        session_id="rs-wait",
        principal="test",
        token_id="rt-wait",
        workspaces=(str(path.resolve()),),
        active_workspace=str(path.resolve()),
        permissions=RemotePermissions(
            read=True,
            write=True,
            shell=True,
            git=True,
            network=False,
            destructive=False,
            service_control=False,
        ),
        created_at=now,
        expires_at=now + 3600,
    )


async def _slow_runner(_record: object) -> str:
    """Outlives any sane wait budget, so the record is still non-terminal."""

    await asyncio.sleep(30)
    return "done"


def _patch_runner(adapter: RemoteToolAdapter, monkeypatch) -> None:
    monkeypatch.setattr(adapter, "_make_long_runner", lambda *a, **k: _slow_runner)


@pytest.mark.asyncio
async def test_bounded_wait_that_expires_fails_instead_of_claiming_success(
    tmp_path: Path, monkeypatch
):
    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    _patch_runner(adapter, monkeypatch)
    session = _session(tmp_path)

    res = await adapter._call_impl(
        session,
        "veya.mission.run",
        {
            "project_root": str(tmp_path),
            "mission_id": "m1",
            "wait": True,
            "wait_timeout_s": 0.2,
        },
    )

    assert res.ok is False, "a wait that expired must not report success"
    assert res.error_code == "TIMEOUT"
    assert res.execution_id, "the caller must still be able to keep watching it"
    assert res.result["accepted"] is False
    assert res.result["terminal"] is False
    assert "poll process.status" in (res.message or "")


@pytest.mark.asyncio
async def test_a_wait_that_reaches_a_terminal_result_is_unaffected(tmp_path: Path, monkeypatch):
    """The negative case for the new branch: a finished run must not be failed.

    Pairs with the timeout test above so the new branch cannot pass by rejecting
    every waited call.
    """

    async def _fast(_record: object) -> str:
        return "finished"

    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    monkeypatch.setattr(adapter, "_make_long_runner", lambda *a, **k: _fast)
    session = _session(tmp_path)

    res = await adapter._call_impl(
        session,
        "veya.mission.run",
        {"project_root": str(tmp_path), "mission_id": "m1", "wait": True, "wait_timeout_s": 30},
    )
    assert res.ok is True, res.message
    assert res.error_code is None
    assert res.result["accepted"] is True


@pytest.mark.asyncio
async def test_no_wait_flag_still_returns_the_submission_receipt(tmp_path: Path, monkeypatch):
    """Without wait, submitting and returning an execution_id is the contract."""

    _repo(tmp_path)
    adapter = RemoteToolAdapter(None)
    _patch_runner(adapter, monkeypatch)
    session = _session(tmp_path)

    res = await adapter._call_impl(
        session, "veya.mission.run", {"project_root": str(tmp_path), "mission_id": "m1"}
    )
    assert res.ok is True, res.message
    assert res.result["accepted"] is True
    assert res.execution_id
