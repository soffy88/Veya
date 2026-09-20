"""P8C gates: the Missions UI is a read-only view over canonical supervision.

Static contract checks over the Svelte sources. They exist because the UI must
never (a) invent a second status machine, (b) render "executed" as success,
(c) dispatch work on page load, or (d) bypass canonical retask on Retry.
"""

from __future__ import annotations

from pathlib import Path

ROUTES = Path("apps/web/src/routes/missions")
LIST = (ROUTES / "+page.svelte").read_text(encoding="utf-8")
NEW = (ROUTES / "new" / "+page.svelte").read_text(encoding="utf-8")
DETAIL = (ROUTES / "[mission_id]" / "+page.svelte").read_text(encoding="utf-8")
DETAIL_LOAD = (ROUTES / "[mission_id]" / "+page.ts").read_text(encoding="utf-8")


# ── pages exist and are wired to the P8B data layer ───────────────────
def test_mission_pages_exist():
    for name, source in (("list", LIST), ("new", NEW), ("detail", DETAIL)):
        assert "$lib/supervision/" in source, name
    assert "missionId" in DETAIL_LOAD and "params.mission_id" in DETAIL_LOAD


def test_web_mission_list_ui():
    assert "createMissionListStore" in LIST
    assert "store.load()" in LIST
    for field in (
        "goal",
        "status",
        "supervision_mode",
        "active_supervisor",
        "iteration",
        "updated_at",
    ):
        assert field in LIST, field
    assert "/missions/new" in LIST and "/missions/${mission.mission_id}" in LIST


def test_web_mission_create_ui_three_mode_selector():
    # the three canonical modes, mapped to their technical values
    for mode in ('"auto"', '"external"', '"internal"'):
        assert mode in NEW, mode
    for label in ("自动", "ChatGPT 监督", "Veya 自主"):
        assert label in NEW, label
    # executor choices
    for executor in ('"hicode"', '"dsh"'):
        assert executor in NEW, executor
    # defaults: auto mode + automatic executor
    assert 'let mode = $state<SupervisionMode>("auto")' in NEW
    assert 'let executor = $state<ExecutorKind | "">("")' in NEW
    # advanced fields folded away
    assert "advanced = !advanced" in NEW
    assert "workspace" in NEW and "acceptance" in NEW and "constraints" in NEW
    # create -> start -> navigate on success only
    assert NEW.index("createMission(") < NEW.index("runMission(")
    assert "!created.ok" in NEW and "never navigate on failure" in NEW
    assert "goto(`/missions/${missionId}`)" in NEW


def test_web_mission_detail_sections():
    for section in (
        "当前状态",
        "ExecutionReport",
        "Review Timeline",
        "Live Events",
        "changes",
        "tests",
        "artifacts",
        "runtime_evidence",
        "failures",
        "blocked_items",
        "git_diff_summary",
        "jev_decisions",
        "proposed_next_action",
        "goalrun_id",
        "execution_id",
    ):
        assert section in DETAIL, section


def test_executed_is_not_rendered_as_success():
    assert "statusLabel(mission?.status)" in DETAIL  # mission status only from backend
    assert "report.tests" in DETAIL  # report status shown via reportHeadline
    assert "reportHeadline(report)" in DETAIL
    # no invented success wording for report/executed states
    for forbidden in ("执行成功", "任务成功", "SUCCESS"):
        assert forbidden not in DETAIL, forbidden
    # artifacts are only claimed as verified when the backend says so
    assert "artifact.verified === true" in DETAIL and "未校验" in DETAIL


def test_waiting_external_label_is_explicit():
    assert 'mission?.status === "WAITING_EXTERNAL_SUPERVISOR"' in DETAIL
    assert "等待 ChatGPT 审查" in DETAIL
    # no button that lets a human bypass the external review
    assert "继续执行" not in DETAIL


def test_auto_route_is_visible():
    assert 'mission?.supervision_mode === "auto"' in DETAIL
    assert "selected supervisor" in DETAIL
    assert "SUPERVISOR_SELECTED" in DETAIL  # route/switch events surface in the timeline
    assert "切换" in DETAIL or "switch" in DETAIL.lower()


def test_jev_decisions_are_structured_not_chat():
    assert "jev_decisions" in DETAIL
    assert "<table" in DETAIL
    for column in ("question", "answer", "confidence", "decision"):
        assert column in DETAIL, column
    # the UI never calls Jev itself
    for forbidden in ("jev.decide", "/jev", "decide("):
        assert forbidden not in DETAIL, forbidden


def test_owner_escalation_cards_are_owner_only():
    for code in (
        "OWNER_CREDENTIAL_REQUIRED",
        "IRREVERSIBLE_EXTERNAL_ACTION",
        "POLICY_CONFIRMATION_REQUIRED",
        "PRODUCTION_DESTRUCTIVE_ACTION",
        "RESOURCE_OWNER_INPUT_REQUIRED",
    ):
        assert code in DETAIL, code
    # ordinary failures never become a human task card
    assert 'mission?.status === "WAITING_OWNER"' in DETAIL


def test_internal_mode_has_no_continue_prompt():
    for forbidden in ("是否继续", "继续？", "confirm("):
        assert forbidden not in DETAIL, forbidden


def test_retry_uses_canonical_continue():
    detail_store = (Path("apps/web/src/lib/supervision/store.svelte.ts")).read_text(
        encoding="utf-8"
    )
    idx = detail_store.index("async function retry(")
    body = detail_store[idx : detail_store.index("async function applyReview(")]
    assert "reviewMission(" in body  # canonical /continue (review/retask) path
    assert "runMission(" not in body  # never a second run
    assert 'decision: ReviewDecision = "RETRY"' in body or '"RETRY"' in body
    assert "store.retry()" in DETAIL


def test_page_load_does_not_run():
    # only GET-based load + subscription in the mount path
    mount = DETAIL[DETAIL.index("onMount(") : DETAIL.index("onDestroy(")]
    assert "store.load()" in mount and "store.subscribe()" in mount
    assert "store.start()" not in mount and "runMission" not in mount
    assert "store.dispose()" in DETAIL  # stream closed on unload


def test_controls_are_the_approved_four():
    for label in ("Start", "Cancel", "Retry"):
        assert label in DETAIL, label
    assert "switchMode(" in DETAIL  # Switch mode (canonical set_supervision_mode)
    for forbidden in ("Pause", "Resume"):
        assert forbidden not in DETAIL, forbidden


def test_double_start_guard_in_ui_and_store():
    assert "disabled={store.runInFlight}" in DETAIL
    assert "if (busy) return;" in NEW  # double submit
    assert "if (runInFlight) return;" in (
        Path("apps/web/src/lib/supervision/store.svelte.ts")
    ).read_text(encoding="utf-8")


def test_mode_switch_preserves_mission_id():
    assert "switchMode" in DETAIL and "same mission" not in DETAIL
    detail_store = (Path("apps/web/src/lib/supervision/store.svelte.ts")).read_text(
        encoding="utf-8"
    )
    start = detail_store.index("async function switchMode(")
    body = detail_store[start : detail_store.index("\n\n  return {", start)]
    assert "setSupervisionMode(missionId, mode)" in body
    assert "createMission" not in body and "goto(" not in body
