"""A damaged append-only journal reports the damage.

``ObservationJournal._load`` swallowed every decode failure, so a truncated or
corrupted line made the journal read as a journal that never recorded those
observations. ``autonomous.observations`` then answered ``ok=true`` with whatever
survived — the caller had no way to know the answer was incomplete.

The wait journal had the same shape; it was fixed alongside. This pins the
observation journal and the flush behaviour that makes a record that reached the
file a whole line.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from veya.autonomous.journal import ObservationJournal
from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.tool_adapter import RemoteToolAdapter

MISSION = "m-obs"


def _session(path: Path) -> RemoteSession:
    now = time.time()
    return RemoteSession(
        session_id="rs-obs",
        principal="test",
        token_id="rt-obs",
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


def _mission_dir(root: Path) -> Path:
    d = root / ".veya" / "autonomous" / MISSION
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_corrupt_records_are_counted_not_swallowed(tmp_path: Path):
    path = tmp_path / "journal.jsonl"
    journal = ObservationJournal(path)
    journal.append(mission_id=MISSION, summary="first")
    journal.append(mission_id=MISSION, summary="second")

    with open(path, "a", encoding="utf-8") as f:
        f.write("{not json\n")
        # Valid JSON, wrong shape: from_dict raises on a non-mapping.
        f.write("[1, 2, 3]\n")

    reloaded = ObservationJournal(path)
    assert reloaded.unreadable_records == 2, "both bad lines must be counted"
    assert reloaded.last_load_error, "the decode error must be kept for the caller"
    # The readable records still load, so the caller can see what survived.
    assert [o.summary for o in reloaded.query(MISSION)] == ["first", "second"]


def test_a_sparse_but_well_formed_record_is_not_treated_as_corruption(tmp_path: Path):
    """The counter must mean damage, not sparsity.

    ``from_dict`` fills defaults, so a record carrying only an id is a real
    record. Counting it would train the caller to ignore the signal.
    """

    path = tmp_path / "journal.jsonl"
    path.write_text('{"mission_id": "m", "observation_id": "x"}\n', encoding="utf-8")

    reloaded = ObservationJournal(path)
    assert reloaded.unreadable_records == 0
    assert len(reloaded.query("m")) == 1


def test_a_record_is_on_disk_before_append_returns(tmp_path: Path):
    """A record that reached the file must be a whole line, not a buffered one."""

    path = tmp_path / "journal.jsonl"
    journal = ObservationJournal(path)
    journal.append(mission_id=MISSION, summary="durable")

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["summary"] == "durable"


def test_appends_accumulate_rather_than_replace(tmp_path: Path):
    path = tmp_path / "journal.jsonl"
    journal = ObservationJournal(path)
    for index in range(5):
        journal.append(mission_id=MISSION, summary=f"obs-{index}")

    assert (
        len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]) == 5
    )
    assert len(ObservationJournal(path).query(MISSION)) == 5


@pytest.mark.asyncio
async def test_observations_reports_a_damaged_journal_instead_of_an_empty_success(
    tmp_path: Path,
):
    auto_dir = _mission_dir(tmp_path)
    journal = ObservationJournal(auto_dir / "journal.jsonl")
    journal.append(mission_id=MISSION, summary="survivor")
    with open(auto_dir / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write("{ truncated\n")

    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)

    res = await adapter._call_impl(session, "autonomous.observations", {"mission": MISSION})
    assert res.ok is False
    assert res.error_code == "EXECUTION_FAILED"
    assert "could not be decoded" in (res.message or "")
    # What did decode is still returned.
    assert len(res.result["observations"]) == 1


@pytest.mark.asyncio
async def test_clean_journal_is_an_explicit_empty_success(tmp_path: Path):
    _mission_dir(tmp_path)

    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)

    res = await adapter._call_impl(session, "autonomous.observations", {"mission": MISSION})
    assert res.ok is True
    assert res.result["observations"] == []
