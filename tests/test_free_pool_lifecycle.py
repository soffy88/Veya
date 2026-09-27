"""State-machine tests for the discovered free-model lifecycle."""

from __future__ import annotations

import json

import pytest

from veya.obase.free_pool import FreePoolLifecycle, FreePoolSnapshot


def _entry(model: str, *, source: str = "provider") -> dict[str, str]:
    return {
        "provider": source,
        "model": model,
        "endpoint": "https://example.test/v1",
        "source": source,
    }


@pytest.mark.asyncio
async def test_catalog_adds_free_model_and_removes_disappeared_model(tmp_path):
    old = _entry("old-free")
    new = _entry("new-free")
    lifecycle = FreePoolLifecycle([old], state_path=tmp_path / "pool.json")

    async def healthy(entry):
        return True, ""

    first = await lifecycle.reconcile(
        FreePoolSnapshot(
            entries=(old, new),
            available={"provider": frozenset({"old-free", "new-free"})},
            healthy_sources=frozenset({"provider"}),
            errors={},
        ),
        healthy,
    )
    assert [item["model"] for item in first] == ["old-free", "new-free"]

    second = await lifecycle.reconcile(
        FreePoolSnapshot(
            entries=(new,),
            available={"provider": frozenset({"new-free"})},
            healthy_sources=frozenset({"provider"}),
            errors={},
        ),
        healthy,
    )
    assert [item["model"] for item in second] == ["new-free"]
    saved = json.loads((tmp_path / "pool.json").read_text(encoding="utf-8"))
    assert saved["models"]["provider/old-free"]["status"] == "removed"


@pytest.mark.asyncio
async def test_persist_false_keeps_state_in_memory_and_writes_nothing(tmp_path):
    """The gateway runs the lifecycle unpersisted.

    model-state.json is the durable pool authority, so a state file on disk can
    only drift behind the running gateway. With persist=False the state machine
    must still reconcile, but must never create or touch the file — and it must
    not seed itself from a pre-existing stale record.
    """
    state_path = tmp_path / "pool.json"
    stale = _entry("stale-free")
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "models": {
                    "provider/stale-free": {
                        "entry": stale,
                        "active": True,
                        "order": 0,
                        "consecutive_failures": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    lifecycle = FreePoolLifecycle([stale], state_path=state_path, persist=False)
    # A stale on-disk record must not resurrect itself into the active pool.
    assert lifecycle.active_pool == []
    assert lifecycle.status()["persisted"] is False

    async def healthy(entry):
        return True, ""

    active = await lifecycle.reconcile(
        FreePoolSnapshot(
            entries=(stale,),
            available={"provider": frozenset({"stale-free"})},
            healthy_sources=frozenset({"provider"}),
            errors={},
        ),
        healthy,
    )
    assert [item["model"] for item in active] == ["stale-free"]
    # reconcile() succeeded in memory, yet the file is byte-identical.
    assert json.loads(state_path.read_text(encoding="utf-8"))["models"].keys() == {
        "provider/stale-free"
    }
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["models"]["provider/stale-free"]["entry"]
        == stale
    )

    # A missing file must not be created either.
    fresh_path = tmp_path / "never-written.json"
    other = FreePoolLifecycle([], state_path=fresh_path, persist=False)
    await other.reconcile(
        FreePoolSnapshot((), {}, frozenset(), {}),
        healthy,
    )
    assert not fresh_path.exists()


def test_persist_defaults_to_true(tmp_path):
    """Guards the default so the frozen gateway opt-in stays explicit."""
    lifecycle = FreePoolLifecycle([], state_path=tmp_path / "pool.json")
    assert lifecycle.status()["persisted"] is True


@pytest.mark.asyncio
async def test_transient_probe_failures_use_cooldown_before_removal(tmp_path):
    seed = _entry("flaky-free")
    lifecycle = FreePoolLifecycle([seed], state_path=tmp_path / "pool.json", failure_threshold=3)

    async def healthy(entry):
        return True, ""

    snapshot = FreePoolSnapshot((), {}, frozenset(), {})
    assert await lifecycle.reconcile(snapshot, healthy)

    async def failed(entry):
        return False, "HTTP 429"

    assert await lifecycle.reconcile(snapshot, failed)
    assert await lifecycle.reconcile(snapshot, failed)
    assert await lifecycle.reconcile(snapshot, failed) == []
    assert lifecycle.status()["models"][0]["status"] == "unhealthy"
