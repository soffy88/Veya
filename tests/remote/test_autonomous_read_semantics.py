"""autonomous.* reads fail closed instead of reporting an empty success.

Three separate lies used to come out of these handlers:

* ``autonomous.waits`` called ``WaitConditionManager.query``, which did not
  exist, so every read died with ``AttributeError`` — surfaced to the caller as
  ``EXECUTION_FAILED / execution failure (AttributeError)``.
* A missing mission, a missing ``state.json`` and an empty journal were all
  answered ``ok=true``: ``status: NOT_FOUND``, ``state: UNKNOWN``, ``[]``.
* A corrupt ``state.json`` escaped as an opaque handler exception, and a corrupt
  wait record was dropped silently, so a damaged journal read as "never waited".

A nameless read is refused rather than answered from the shared parent
directory, which is where it used to land.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from veya.autonomous.models import WaitType
from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.tool_adapter import RemoteToolAdapter

MISSION = "m-1"


def _session(path: Path) -> RemoteSession:
    now = time.time()
    return RemoteSession(
        session_id="rs-auto",
        principal="test",
        token_id="rt-auto",
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


def _mission_dir(root: Path, mission: str = MISSION) -> Path:
    d = root / ".veya" / "autonomous" / mission
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_state(auto_dir: Path, **fields) -> None:
    payload = {"state": "OBSERVING", "objective": "ship it", "accepted_progress": ["a"]}
    payload.update(fields)
    (auto_dir / "state.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_waits_requires_a_mission_and_reports_active_and_past(tmp_path: Path):
    from veya.autonomous.wait import WaitConditionManager

    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)
    auto_dir = _mission_dir(tmp_path)

    wm = WaitConditionManager(auto_dir / "waits.jsonl")
    active = wm.register_wait(MISSION, WaitType.EVENT, "deploy:done", timeout_s=600)
    wm.mark_satisfied(active.condition_id)

    res = await adapter._call_impl(session, "autonomous.waits", {"mission": MISSION})
    assert res.ok is True, res.message
    waits = res.result["waits"]
    assert len(waits) == 1
    # A resolved wait must still be visible: an active-only view makes a mission
    # whose waits all fired indistinguishable from one that never waited.
    assert waits[0]["status"] == "SATISFIED"

    # No mission: refused, not answered from the shared parent directory.
    res = await adapter._call_impl(session, "autonomous.waits", {})
    assert res.ok is False
    assert res.error_code == "INVALID_ARGUMENT"
    assert res.message and "mission is required" in res.message


@pytest.mark.asyncio
async def test_waits_surfaces_a_corrupt_journal_instead_of_an_empty_list(tmp_path: Path):
    from veya.autonomous.wait import WaitConditionManager

    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)
    auto_dir = _mission_dir(tmp_path)

    wm = WaitConditionManager(auto_dir / "waits.jsonl")
    wm.register_wait(MISSION, WaitType.EVENT, "ok")
    with open(auto_dir / "waits.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json at all\n")

    res = await adapter._call_impl(session, "autonomous.waits", {"mission": MISSION})
    assert res.ok is False
    assert res.error_code == "EXECUTION_FAILED"
    # The decodable records are still returned, so the caller can see what survived.
    assert len(res.result["waits"]) == 1
    assert "1 wait record" in (res.message or "")


@pytest.mark.asyncio
async def test_empty_journal_is_an_explicit_empty_success(tmp_path: Path):
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)
    _mission_dir(tmp_path)

    res = await adapter._call_impl(session, "autonomous.waits", {"mission": MISSION})
    assert res.ok is True
    assert res.result["waits"] == []
    assert res.result["mission_id"] == MISSION


@pytest.mark.asyncio
async def test_missing_mission_is_not_found_not_an_empty_success(tmp_path: Path):
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)

    for tool in (
        "autonomous.status",
        "autonomous.progress",
        "autonomous.observations",
        "autonomous.decisions",
        "autonomous.waits",
        "autonomous.escalations",
    ):
        res = await adapter._call_impl(session, tool, {"mission": "never-started"})
        assert res.ok is False, f"{tool} reported success for a mission that never started"
        assert res.error_code == "NOT_FOUND", f"{tool}: {res.error_code}"


@pytest.mark.asyncio
async def test_missing_state_is_not_found_and_corrupt_state_is_execution_failed(tmp_path: Path):
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)
    auto_dir = _mission_dir(tmp_path)

    # Mission directory exists but nothing has been written yet.
    res = await adapter._call_impl(session, "autonomous.status", {"mission": MISSION})
    assert res.ok is False
    assert res.error_code == "NOT_FOUND"

    (auto_dir / "state.json").write_text("{ truncated", encoding="utf-8")
    res = await adapter._call_impl(session, "autonomous.status", {"mission": MISSION})
    assert res.ok is False
    assert res.error_code == "EXECUTION_FAILED"
    assert "unreadable" in (res.message or "")

    # A record without "state" is a partial write, not an early mission: the old
    # code answered ok=true with state="UNKNOWN".
    (auto_dir / "state.json").write_text(json.dumps({"objective": "x"}), encoding="utf-8")
    res = await adapter._call_impl(session, "autonomous.progress", {"mission": MISSION})
    assert res.ok is False
    assert res.error_code == "EXECUTION_FAILED"
    assert "incomplete" in (res.message or "")


@pytest.mark.asyncio
async def test_progress_reports_the_recorded_state_not_unknown(tmp_path: Path):
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)
    _write_state(_mission_dir(tmp_path), state="EXECUTING", accepted_progress=["x", "y"])

    res = await adapter._call_impl(session, "autonomous.progress", {"mission": MISSION})
    assert res.ok is True
    assert res.result["state"] == "EXECUTING"
    assert res.result["accepted_progress"] == ["x", "y"]
    assert res.result["mission_id"] == MISSION
