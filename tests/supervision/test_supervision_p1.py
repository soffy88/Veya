"""P1: Mission durable store + SupervisionRouter (selection, switching, lineage)."""

from __future__ import annotations

from pathlib import Path

from veya.supervision.models import (
    Mission,
    MissionStatus,
    ReviewDecision,
    SupervisionMode,
    SupervisorReview,
)
from veya.supervision.router import RouterRequest, SupervisionRouter
from veya.supervision.store import MissionStore


def _mission(mode: SupervisionMode = SupervisionMode.auto) -> Mission:
    return Mission(mission_id="m-1", goal="do the thing", supervision_mode=mode)


# ── store ───────────────────────────────────────────────────────────────
def test_mission_document_roundtrip(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)
    store.save(_mission())
    restored = store.load("m-1")
    assert restored is not None
    assert restored.goal == "do the thing"
    assert [m.mission_id for m in store.list()] == ["m-1"]


def test_status_and_mode_updates_do_not_rebuild_mission(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)
    store.save(_mission())
    store.set_status("m-1", MissionStatus.executing)
    store.set_supervision_mode("m-1", "internal")
    restored = store.load("m-1")
    assert restored is not None
    assert restored.mission_id == "m-1"
    assert restored.status is MissionStatus.executing
    assert restored.supervision_mode is SupervisionMode.internal


def test_goalrun_link_and_event_log(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)
    store.save(_mission())
    store.link_goalrun("m-1", "goalrun-9", checkpoint_id="ck-3")
    store.append_event("m-1", "MISSION_CREATED", {"mode": "auto"})
    restored = store.load("m-1")
    assert restored is not None
    assert restored.authority["goalrun_id"] == "goalrun-9"
    assert restored.authority["checkpoint_id"] == "ck-3"
    assert [e["topic"] for e in store.events("m-1")] == ["MISSION_CREATED"]


def test_review_log_roundtrip(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)
    store.save(_mission())
    store.append_review(
        SupervisorReview(
            mission_id="m-1",
            iteration=1,
            supervisor="internal",
            decision=ReviewDecision.revise,
        )
    )
    latest = store.latest_review("m-1")
    assert latest is not None and latest.decision is ReviewDecision.revise


# ── router: initial selection ───────────────────────────────────────────
def test_explicit_modes_are_honoured() -> None:
    router = SupervisionRouter()
    ext = router.select(RouterRequest(mission=_mission(SupervisionMode.external)))
    assert ext.selected_mode == "external" and ext.reason_code == "explicit_external"
    internal = router.select(RouterRequest(mission=_mission(SupervisionMode.internal)))
    assert internal.selected_mode == "internal" and internal.confidence == 1.0


def test_external_unavailable_waits_by_default() -> None:
    router = SupervisionRouter()
    decision = router.select(
        RouterRequest(mission=_mission(SupervisionMode.external), external_available=False)
    )
    assert decision.selected_mode == "external"
    assert decision.reason_code == "external_unavailable_wait"


def test_external_unavailable_falls_back_only_when_policy_allows() -> None:
    mission = _mission(SupervisionMode.external)
    mission.policies.supervisor_policy["external_fallback_to_internal"] = True
    router = SupervisionRouter()
    decision = router.select(RouterRequest(mission=mission, external_available=False))
    assert decision.selected_mode == "internal"
    assert decision.reason_code == "external_unavailable_fallback"


def test_auto_bias_and_risk() -> None:
    router = SupervisionRouter()
    assert (
        router.select(
            RouterRequest(mission=_mission(), characteristics=["architecture_redesign"])
        ).selected_mode
        == "external"
    )
    assert (
        router.select(
            RouterRequest(mission=_mission(), characteristics=["known_bug"])
        ).selected_mode
        == "internal"
    )
    # high risk biases an otherwise-internal task to external review
    high = router.select(
        RouterRequest(mission=_mission(), characteristics=["known_bug"], risk="critical")
    )
    assert high.selected_mode == "external" and high.reason_code == "risk_external"


def test_auto_defaults_to_internal() -> None:
    router = SupervisionRouter()
    assert router.select(RouterRequest(mission=_mission())).selected_mode == "internal"


# ── router: runtime switching + lineage ─────────────────────────────────
def test_switch_persists_lineage_and_event(tmp_path: Path) -> None:
    store = MissionStore(tmp_path)
    mission = store.save(_mission())
    router = SupervisionRouter(store)
    decision = router.switch(
        mission,
        current="internal",
        trigger="architecture_ambiguity",
        reason="canonical conflict",
        iteration=3,
    )
    assert decision is not None and decision.selected_mode == "external"
    reloaded = store.load("m-1")
    assert reloaded is not None
    lineage = router.lineage(reloaded)
    assert lineage[0]["from"] == "internal" and lineage[0]["to"] == "external"
    assert any(e["topic"] == "SUPERVISOR_SWITCHED" for e in store.events("m-1"))


def test_switch_direction_is_bounded() -> None:
    router = SupervisionRouter()
    assert router.switch(_mission(), current="internal", trigger="nonsense") is None
    assert (
        router.switch(_mission(), current="external", trigger="design_frozen") is None
    )  # no store
