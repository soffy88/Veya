"""P9C supervision regression: canonical web-source contracts + backend projection.

The Web implementation is TypeScript/Svelte. These Python tests validate its
source contract rather than inventing a second Python implementation of the
frontend store. Backend integration uses the real Veya supervision runtime.
"""

from __future__ import annotations

import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STORE_PATH = ROOT / "apps/web/src/lib/supervision/store.svelte.ts"
EVENTS_PATH = ROOT / "apps/web/src/lib/supervision/events.ts"
STORE_SRC = STORE_PATH.read_text(encoding="utf-8")
EVENTS_SRC = EVENTS_PATH.read_text(encoding="utf-8")


def _state_topics() -> set[str]:
    match = re.search(
        r"STATE_AFFECTING_TOPICS[^=]*=\s*new Set\(\[(.*?)\]\);",
        STORE_SRC,
        re.S,
    )
    assert match, "STATE_AFFECTING_TOPICS declaration missing"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _function_slice(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


def test_state_affecting_event_triggers_refresh_contract() -> None:
    topics = _state_topics()
    assert {
        "MISSION_CREATED",
        "EXECUTION_COMPLETED",
        "REVIEW_COMPLETED",
        "EXECUTOR_COMPLETED",
    } <= topics
    subscribe = _function_slice(STORE_SRC, "subscribe", "dispose")
    assert "if (isStateAffecting(event)) refreshAfterDebounce();" in subscribe


def test_non_state_event_does_not_refresh_contract() -> None:
    topics = _state_topics()
    assert {"SOME_UNKNOWN_EVENT", "RANDOM_NOTIFICATION", "UI_CLICK", "TOKEN_STREAM"}.isdisjoint(
        topics
    )
    assert 'return STATE_AFFECTING_TOPICS.has(event.topic ?? "");' in STORE_SRC


def test_burst_refresh_is_debounced_and_coalesced() -> None:
    refresh = _function_slice(STORE_SRC, "refreshAfterDebounce", "refreshCanonical")
    assert "REFRESH_DEBOUNCE_MS = 150" in STORE_SRC
    assert "clearTimeout(refreshTimer)" in refresh
    assert "refreshTimer = setTimeout(" in refresh
    assert "if (refreshInFlight)" in refresh
    assert "refreshPending = true" in refresh


def test_in_flight_refresh_queues_exactly_one_trailing_refresh() -> None:
    refresh = _function_slice(STORE_SRC, "refreshCanonical", "isStateAffecting")
    assert "if (refreshInFlight) return;" in refresh
    assert "refreshInFlight = true" in refresh
    assert "refreshInFlight = false" in refresh
    assert "if (refreshPending)" in refresh
    assert "refreshPending = false" in refresh
    assert "refreshAfterDebounce();" in refresh


def test_dispose_stops_future_refresh_timer() -> None:
    dispose = _function_slice(STORE_SRC, "dispose", "start")
    assert "disposeStream?.();" in dispose
    assert "clearTimeout(refreshTimer)" in dispose
    assert "refreshInFlight = false" in dispose
    assert "refreshPending = false" in dispose


def test_load_and_refresh_paths_are_read_only() -> None:
    load = _function_slice(STORE_SRC, "load", "refreshAfterDebounce")
    refresh = _function_slice(STORE_SRC, "refreshCanonical", "isStateAffecting")
    for forbidden in ("runMission(", "reviewMission(", "cancelMission(", "setSupervisionMode("):
        assert forbidden not in load
        assert forbidden not in refresh
    assert "await load();" in refresh
    assert STORE_SRC.count("runMission(") == 1
    start = _function_slice(STORE_SRC, "start", "cancel")
    assert "if (runInFlight) return;" in start
    assert "runMission(" in start


@pytest.mark.asyncio
async def test_backend_projection_uses_real_execution_handle(tmp_path: Path) -> None:
    from veya.supervision import MissionStore, SupervisionRouter
    from veya.supervision.loop import MissionLoop
    from veya.supervision.models import Mission, MissionPolicies, SupervisionMode

    node = types.SimpleNamespace(
        title="t",
        assignee="dsh",
        status="completed",
        acceptance=["ok"],
        verify_summary="passed",
        artifacts=[],
        evidence=[],
        retries=0,
        block_reason=None,
        unfinished_work=[],
    )

    async def runner(_mission):
        return types.SimpleNamespace(
            goal_id="gr-1",
            status="executed",
            tasks={"t": node},
            final_summary="ok",
            unfinished_work=[],
        )

    store = MissionStore(tmp_path)
    mission = Mission(
        mission_id="mission-p9c",
        goal="g",
        supervision_mode=SupervisionMode.external,
        workspace=str(tmp_path),
        policies=MissionPolicies(execution_policy={"assignee_hint": "dsh"}),
    )
    store.save(mission)
    loop = MissionLoop(store=store, router=SupervisionRouter(store), runner=runner)

    await loop.step(mission.mission_id)

    persisted = store.load(mission.mission_id)
    assert persisted is not None
    assert persisted.authority["execution_id"] == f"{mission.mission_id}:0"
    assert persisted.authority["iteration"] == 0
    handle = store.execution_for(mission.mission_id, 0)
    assert handle is not None
    assert handle["execution_id"] == persisted.authority["execution_id"]


def test_sse_reconnect_deduplicates_by_seen_index() -> None:
    assert "let seen = Math.max(0, Math.floor(startIndex));" in EVENTS_SRC
    assert "if (index < seen) return;" in EVENTS_SRC
    assert "seen = index + 1;" in EVENTS_SRC
    assert "for (let i = seen; i < events.length; i += 1)" in EVENTS_SRC


def test_full_supervision_regression() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/supervision",
            "tests/runtime/test_supervision_http.py",
            "tests/runtime/test_supervision_web_contracts.py",
            "tests/runtime/test_supervision_web_authz.py",
            "tests/runtime/test_supervision_web_ui.py",
            "-q",
            "--tb=short",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
