"""P8A contract tests: the supervision HTTP surface is a thin canonical adapter."""

from __future__ import annotations

from server.routes.supervision import router
from server.supervision_tools import _TOOLS

APPROVED_SURFACE = {
    ("POST", "/api/v1/supervision/missions"),
    ("GET", "/api/v1/supervision/missions"),
    ("GET", "/api/v1/supervision/missions/{mission_id}"),
    ("POST", "/api/v1/supervision/missions/{mission_id}/run"),
    ("POST", "/api/v1/supervision/missions/{mission_id}/cancel"),
    ("POST", "/api/v1/supervision/missions/{mission_id}/continue"),
    ("POST", "/api/v1/supervision/missions/{mission_id}/mode"),
    ("GET", "/api/v1/supervision/missions/{mission_id}/reports/latest"),
    ("GET", "/api/v1/supervision/missions/{mission_id}/reports/{iteration}"),
    ("GET", "/api/v1/supervision/missions/{mission_id}/reviews"),
    ("GET", "/api/v1/supervision/missions/{mission_id}/escalations"),
    ("GET", "/api/v1/supervision/missions/{mission_id}/events"),
}


def test_http_surface_matches_approved_set():
    actual = {(method, route.path) for route in router.routes for method in route.methods}
    assert actual == APPROVED_SURFACE


def test_no_pause_resume_endpoint_by_design():
    paths = {route.path for route in router.routes}
    assert not any(p.endswith(("/pause", "/resume")) for p in paths)


def test_canonical_tools_cover_http_surface():
    names = {entry[0] for entry in _TOOLS}
    for required in (
        "veya_mission_create",
        "veya_mission_inspect",
        "veya_mission_run",
        "veya_mission_cancel",
        "veya_mission_continue",
        "veya_mission_set_mode",
        "veya_report_latest",
        "veya_report_get",
        "veya_reviews",
        "veya_escalation_list",
        "veya_events",
    ):
        assert required in names, f"{required} missing from the canonical tool registry"


def test_set_mode_is_thin_store_wrapper(tmp_path):
    from server.supervision_tools import veya_mission_create, veya_mission_set_mode
    from veya.supervision import MissionStore

    created = veya_mission_create(
        project_root=str(tmp_path), goal="g", supervision_mode="auto", workspace=str(tmp_path)
    )
    mission_id = created["mission"]["mission_id"]
    switched = veya_mission_set_mode(
        project_root=str(tmp_path), mission_id=mission_id, mode="external"
    )
    assert switched["mission"]["supervision_mode"] == "external"
    # same mission, lineage untouched
    assert switched["mission"]["mission_id"] == mission_id
    reloaded = MissionStore(tmp_path).load(mission_id)
    assert str(reloaded.supervision_mode) == "external"
    events = MissionStore(tmp_path).events(mission_id)
    assert any(e.get("topic") == "SUPERVISION_MODE_CHANGED" for e in events)


def test_reviews_and_events_are_read_only_views(tmp_path):
    from server.supervision_tools import veya_events, veya_mission_create, veya_reviews

    created = veya_mission_create(
        project_root=str(tmp_path), goal="g", supervision_mode="auto", workspace=str(tmp_path)
    )
    mission_id = created["mission"]["mission_id"]
    assert veya_reviews(project_root=str(tmp_path), mission_id=mission_id) == {"reviews": []}
    events = veya_events(project_root=str(tmp_path), mission_id=mission_id)["events"]
    assert isinstance(events, list) and events
