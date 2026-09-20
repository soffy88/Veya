"""P8B contract tests: the Web supervision data layer mirrors the backend.

The frontend must not maintain a second set of business enums, so this test
compares the TypeScript declarations in
``apps/web/src/lib/supervision/types.ts`` against the *live* canonical
``to_dict()`` output and enum members. Any backend field/enum change that is not
mirrored here fails the test.
"""

from __future__ import annotations

import re
from pathlib import Path

from veya.supervision.models import (
    ExecutionReport,
    Mission,
    MissionStatus,
    ReviewDecision,
    SupervisionMode,
    SupervisorReview,
)

LIB = Path("apps/web/src/lib/supervision")
TYPES = (LIB / "types.ts").read_text(encoding="utf-8")
STORE = (LIB / "store.svelte.ts").read_text(encoding="utf-8")
EVENTS = (LIB / "events.ts").read_text(encoding="utf-8")
API = (LIB / "api.ts").read_text(encoding="utf-8")


def _interface_fields(name: str) -> set[str]:
    match = re.search(rf"export interface {name} \{{(.*?)\n\}}", TYPES, re.S)
    assert match, f"interface {name} not found in types.ts"
    body = match.group(1)
    fields = set()
    for line in body.splitlines():
        line = line.strip()
        # field names may be quoted or bare, optional or not
        m = re.match(r"^(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z_][A-Za-z0-9_]*))\??\s*:", line)
        if m:
            fields.add(next(g for g in m.groups() if g))
    return fields


def _union_values(name: str) -> set[str]:
    match = re.search(rf"export type {name} =(.*?);", TYPES, re.S)
    assert match, f"union {name} not found in types.ts"
    return set(re.findall(r"\"([A-Za-z_]+)\"", match.group(1)))


def test_mission_fields_match_backend():
    canonical = set(
        Mission(mission_id="m", goal="g").to_dict().keys()
    )
    assert _interface_fields("Mission") == canonical


def test_execution_report_fields_match_backend():
    canonical = set(
        ExecutionReport(
            mission_id="m", iteration=0, objective="o", status="executed"
        ).to_dict().keys()
    )
    assert _interface_fields("ExecutionReport") == canonical


def test_supervisor_review_fields_match_backend():
    canonical = set(
        SupervisorReview(
            mission_id="m", iteration=0, supervisor="internal", decision=ReviewDecision.accept
        ).to_dict().keys()
    )
    assert _interface_fields("SupervisorReview") == canonical


def test_status_and_decision_unions_are_canonical():
    assert _union_values("MissionStatus") == {str(s) for s in MissionStatus}
    assert _union_values("ReviewDecision") == {str(d) for d in ReviewDecision}
    assert _union_values("SupervisionMode") == {str(m) for m in SupervisionMode}


def test_no_second_state_machine_in_format_module():
    """format.ts may label canonical values but must not invent new ones."""
    fmt = (LIB / "format.ts").read_text(encoding="utf-8")
    labels = set(re.findall(r"^\s{2}([A-Za-z_]+):", fmt, re.M))
    report_statuses = {"executed", "INTERRUPTED"}
    assert labels <= {str(s) for s in MissionStatus} | report_statuses, sorted(labels)
    # executed must never be presented as success
    assert "已执行（不代表成功）" in fmt


def test_load_path_never_starts_a_run():
    """WEB_NO_DUPLICATE_RUN: only the explicit start() action dispatches work."""
    assert STORE.count("runMission(") == 1
    start_idx = STORE.index("async function start(")
    load_idx = STORE.index("async function load(")
    assert STORE.index("runMission(") > start_idx > load_idx
    # the read-only load path only uses GET endpoints
    load_body = STORE[load_idx : STORE.index("function subscribe(")]
    for forbidden in ("runMission", "reviewMission", "cancelMission", "setSupervisionMode"):
        assert forbidden not in load_body, forbidden


def test_start_is_idempotent_against_double_click():
    assert "if (runInFlight) return;" in STORE


def test_api_only_talks_to_the_canonical_supervision_surface():
    assert "api/v1/supervision" in API
    for forbidden in ("/mcp", "hicode", "dsh", "OPENAI", "DEEPSEEK", "remote_mcp"):
        assert forbidden not in API, forbidden


def test_event_reconnect_dedupes():
    assert "if (index < seen) return;" in EVENTS
    assert "startIndex" in EVENTS and "preferPolling" in EVENTS
    assert "authorization" in EVENTS  # token via fetch header (EventSource cannot)
