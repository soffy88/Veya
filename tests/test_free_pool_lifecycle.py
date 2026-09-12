"""State-machine tests for the discovered free-model lifecycle."""

from __future__ import annotations

import json

import pytest

from veya.obase.free_pool import FreePoolLifecycle, FreePoolSnapshot


def _entry(model: str, *, source: str = "provider") -> dict[str, str]:
    return {"provider": source, "model": model, "endpoint": "https://example.test/v1", "source": source}


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
async def test_transient_probe_failures_use_cooldown_before_removal(tmp_path):
    seed = _entry("flaky-free")
    lifecycle = FreePoolLifecycle(
        [seed], state_path=tmp_path / "pool.json", failure_threshold=3
    )

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
