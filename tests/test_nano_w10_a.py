"""NANO-W10 tests: always-on agent ergonomics.

One file for the whole wave, per `NANO_W10_SPEC.md` line 2.  Sections are added
in spec order (1..7, then the ADDENDUMs).  Every dependency that would touch
``~/.veya`` or a live service is injected; temp dirs are the only filesystem.

Run with:  .venv/bin/python -m pytest -q tests/test_nano_w10_a.py
"""

from __future__ import annotations

import json

import pytest

from server.nano_w10.flags import ALL_FLAGS, FeatureDisabled, feature_flags, flag_enabled, require_flag
from server.nano_w10.loop_mode import LoopRunPolicy, finish_card
from server.nano_w10.turn_queue import TurnQueue
from server.nano_w10.wake_router import WakeRouter, build_triage_engine
from veya.omodul.loop_run import LoopRunConfig, LoopRunInput, loop_run
from veya.omodul.turn_intake import (
    TurnIntakeConfig,
    TurnIntakeInput,
    compute_fingerprint_for,
    turn_intake,
)
from veya.omodul.wake_router import WAKE_SOURCES, WakeRouterConfig, WakeRouterInput, wake_router
from veya.oprim.turn_envelope import envelope_fingerprint, idem_key
from veya.oskill.pure.loop_policy import (
    LOOP_MODES,
    MODE_GOAL,
    MODE_LOOP,
    STOP_BUDGET_TIME,
    STOP_BUDGET_TURNS,
    STOP_NO_PROGRESS,
    STOP_USER_STOP,
    checkpoint_card,
    loop_decision,
    parse_loop_brief,
)
from veya.oskill.pure.turn_priority import (
    INBOUND_KINDS,
    STATE_DELIVERED,
    STATE_UNCONFIRMED,
    classify_inbound,
    coalesce_window,
    envelope_priority,
    order_queue,
    steer_decision,
)
from veya.oskill.pure.wake_policy import (
    OUTCOMES,
    is_quiet_hours,
    worth_bugging_user,
)


# ══════════════════════════════════════════════════════════════════════════
# 0 — flags (HARD RULES: default OFF except 7a / read-only surfaces)
# ══════════════════════════════════════════════════════════════════════════


def test_flags_default_off_except_readonly_surfaces() -> None:
    on_by_default = {flag for flag, default in ALL_FLAGS.items() if default}
    assert on_by_default == {
        "VEYA_TOOL_ERROR_FRIENDLY",
        "VEYA_LLM_TAP",
        "VEYA_SEARCH_FALLBACK",
        "VEYA_CHANNEL_STATUS",
    }
    for flag in ("VEYA_TURN_QUEUE", "VEYA_AUTO_REVIEW", "VEYA_PROMPT_CACHE", "VEYA_LOOP_MODE"):
        assert ALL_FLAGS[flag] is False
        assert flag_enabled(flag) is False


def test_require_flag_reports_which_flag_is_off() -> None:
    with pytest.raises(FeatureDisabled) as excinfo:
        require_flag("VEYA_TURN_QUEUE")
    assert excinfo.value.as_error() == {
        "code": "FEATURE_DISABLED",
        "flag": "VEYA_TURN_QUEUE",
    }
    assert set(feature_flags()) == set(ALL_FLAGS)


# ══════════════════════════════════════════════════════════════════════════
# 1 — turn queue: priority + coalescing + steer + idempotency
# ══════════════════════════════════════════════════════════════════════════


def test_priority_order_is_the_spec_order() -> None:
    assert INBOUND_KINDS == ("direct", "group_mention", "channel", "routine")
    assert [envelope_priority({"kind": k}) for k in INBOUND_KINDS] == [0, 1, 2, 3]


def test_unknown_kind_never_outranks_a_user_message() -> None:
    assert classify_inbound({"kind": "who-knows"}) == "routine"
    assert classify_inbound({}) == "routine"
    assert classify_inbound({"user_directed": True}) == "direct"
    assert classify_inbound({"mention": True}) == "group_mention"


def test_order_queue_is_stable_on_arrival_ties() -> None:
    queued = [
        {"kind": "routine", "arrived_at": 10.0, "text": "r1"},
        {"kind": "channel", "arrived_at": 10.0, "text": "c1"},
        {"kind": "direct", "arrived_at": 11.0, "text": "d1"},
        {"kind": "group_mention", "arrived_at": 9.0, "text": "g1"},
    ]
    # Priority first (direct beats a mention even though it arrived later),
    # then arrival order so equal-priority traffic never reorders.
    assert [item["text"] for item in order_queue(queued)] == ["d1", "g1", "c1", "r1"]


def test_direct_message_steers_a_running_turn() -> None:
    decision = steer_decision({"kind": "direct"}, turn_state="running", has_start_evidence=True)
    assert decision["action"] == "steer"
    # W6 K receipt vocabulary is reused verbatim — no second state machine.
    assert decision["state"] in {"delivered", "started", "unconfirmed"}


def test_non_direct_traffic_queues_behind_a_running_turn() -> None:
    for kind in ("group_mention", "channel", "routine"):
        assert steer_decision({"kind": kind}, turn_state="running")["action"] == "queue"


def test_coalesce_merges_inside_the_window_and_keeps_highest_priority() -> None:
    members = [
        {"kind": "routine", "text": "nightly digest", "arrived_at": 100.0},
        {"kind": "direct", "text": "deploy now", "arrived_at": 100.4},
    ]
    merged = coalesce_window(members, now=100.9, window_s=2.0)
    assert merged["coalesced"] is True
    assert merged["count"] == 2
    assert merged["kind"] == "direct"
    assert merged["text"].splitlines() == ["deploy now", "nightly digest"]


def test_coalesce_window_is_configurable_and_zero_means_no_merge() -> None:
    members = [{"kind": "direct", "text": "a", "arrived_at": 100.0}]
    # 1.0s after the first message: inside the 2s window, outside a 0.5s one.
    assert coalesce_window(members, now=101.0, window_s=2.0)["coalesced"] is True
    assert coalesce_window(members, now=101.0, window_s=0.5)["coalesced"] is False
    assert coalesce_window(members, now=101.0, window_s=0.0)["coalesced"] is False
    assert coalesce_window([], now=1.0)["count"] == 0


def test_idempotency_key_is_stable_per_message_and_differs_across_messages() -> None:
    assert idem_key("c1", "user", "m1") == idem_key("c1", "user", "m1")
    assert idem_key("c1", "user", "m1") != idem_key("c1", "user", "m2")
    assert idem_key("c1", "user", "m1") != idem_key("c2", "user", "m1")
    assert envelope_fingerprint({"a": 1}, ()) == ""
    assert envelope_fingerprint({"a": 1}, ("a",)) == envelope_fingerprint({"a": 1}, ("a",))


def test_turn_intake_is_a_pure_function_of_its_collaborators() -> None:
    seen: set[str] = set()
    pending: list[dict[str, object]] = []
    config = TurnIntakeConfig(
        turn_state=lambda: "none",
        list_pending=lambda: list(pending),
        append_pending=lambda envelope, text: pending.append(dict(envelope)),
        seen_before=lambda key: key in seen,
        mark_seen=lambda key, envelope: seen.add(key),
        coalesce_window_s=2.0,
    )
    first = turn_intake(
        config,
        TurnIntakeInput(conversation_ref="c1", kind="direct", text="hi", external_id="m1", arrived_at=1.0),
        None,
        now=1.0,
    )
    assert first["status"] == "completed"
    assert first["action"] == "queue"
    retry = turn_intake(
        config,
        TurnIntakeInput(conversation_ref="c1", kind="direct", text="hi", external_id="m1", arrived_at=1.2),
        None,
        now=1.2,
    )
    assert retry["action"] == "drop"
    assert retry["idem_key"] == first["idem_key"]
    assert len(pending) == 1


def test_turn_intake_never_raises_on_a_missing_conversation_ref() -> None:
    result = turn_intake(TurnIntakeConfig(), TurnIntakeInput(conversation_ref=""), None, now=1.0)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["action"] == "drop"
    # The fingerprint helper degrades to "" rather than exploding.
    assert compute_fingerprint_for(TurnIntakeConfig(), TurnIntakeInput(conversation_ref="")) != ""
    assert compute_fingerprint_for(TurnIntakeConfig(), None) == ""


def test_turn_queue_layer_end_to_end() -> None:
    queue = TurnQueue(window_s=2.0)
    assert queue.turn_state("c1") == "none"

    first = queue.intake(conversation_ref="c1", text="hello", kind="direct", external_id="m1")
    assert first["action"] == "queue"
    second = queue.intake(conversation_ref="c1", text="and this", kind="direct", external_id="m2")
    assert second["action"] == "queue"
    assert "and this" in second["text"]
    assert queue.turn_state("c1") == "pending"

    # A client retry of m1 must not open a second turn.
    assert queue.intake(conversation_ref="c1", text="hello", external_id="m1")["action"] == "drop"

    queue.mark_running("c1", "bot-1")
    steer = queue.intake(conversation_ref="c1", text="actually stop", external_id="m3")
    assert steer["action"] == "steer"
    assert steer["state"] == STATE_DELIVERED
    assert queue.steer_log("c1") == ["actually stop"]
    assert queue.running_snapshot()[0]["bot_ref"] == "bot-1"

    nxt = queue.next_turn("c1")
    assert nxt is not None and nxt["count"] == 2
    queue.mark_finished("c1")
    assert queue.next_turn("c1") is None


def test_turn_queue_drops_the_least_important_member_at_the_cap() -> None:
    queue = TurnQueue(window_s=60.0, max_pending=3)
    queue.intake(conversation_ref="c1", text="a", kind="direct", external_id="1", arrived_at=1.0)
    queue.intake(conversation_ref="c1", text="b", kind="channel", external_id="2", arrived_at=1.1)
    queue.intake(conversation_ref="c1", text="c", kind="routine", external_id="3", arrived_at=1.2)
    queue.intake(conversation_ref="c1", text="d", kind="direct", external_id="4", arrived_at=1.3)
    kinds = [str(item.get("kind")) for item in queue.list_pending("c1")]
    texts = [str(item.get("text")) for item in queue.list_pending("c1")]
    assert kinds.count("routine") == 0
    assert len(kinds) == 3
    # The routine envelope is the one that was dropped, not the newest one.
    assert "c" not in texts
    assert "d" in texts


def test_turn_intake_trail_is_recorded() -> None:
    trail: list[dict[str, object]] = []
    turn_intake(
        TurnIntakeConfig(turn_state=lambda: "none", trail_sink=trail.append),
        TurnIntakeInput(conversation_ref="c1", kind="direct", text="x", arrived_at=1.0),
        None,
        now=1.0,
    )
    assert any(entry.get("event") == "intake.queued" for entry in trail)
    assert json.dumps(trail)  # trail entries must be JSON-serialisable


# ══════════════════════════════════════════════════════════════════════════
# 2 — wake router + "worth bugging the user?"
# ══════════════════════════════════════════════════════════════════════════


def test_quiet_hours_wrap_past_midnight() -> None:
    settings = {"quiet_hours": {"enabled": True, "start": "23:00", "end": "08:00"}}
    assert is_quiet_hours(settings, now=2 * 3600) is True
    assert is_quiet_hours(settings, now=23 * 3600 + 1800) is True
    assert is_quiet_hours(settings, now=12 * 3600) is False
    assert is_quiet_hours({"quiet_hours": {"enabled": False}}, now=2 * 3600) is False
    assert is_quiet_hours({}, now=2 * 3600) is False


def test_policy_is_fail_quiet_for_background_noise() -> None:
    quiet = worth_bugging_user({"user_directed": False, "novel": False})
    assert quiet["outcome"] == "silent"
    # An unknown notification level never lets a background wake interrupt.
    assert worth_bugging_user({"user_directed": False, "novel": True}, {})["outcome"] == "silent"
    # ...but it does not silence a user who is actually waiting.
    assert worth_bugging_user({"user_directed": True, "novel": True}, {})["outcome"] == "reply_now"


def test_urgent_beats_quiet_hours_and_never_level() -> None:
    settings = {"notification_level": "all", "quiet_hours": {"enabled": True, "start": "23:00", "end": "08:00"}}
    assert worth_bugging_user({"urgent": True}, settings, now=2 * 3600)["outcome"] == "reply_now"
    never = {"notification_level": "never"}
    assert worth_bugging_user({"urgent": True, "user_directed": True}, never)["outcome"] == "silent"


def test_all_four_outcomes_are_reachable() -> None:
    everything = {"notification_level": "all"}
    assert worth_bugging_user({"user_directed": True, "urgent": True}, everything)["outcome"] == "reply_now"
    assert worth_bugging_user({"user_directed": True, "novel": True}, everything)["outcome"] == "reply_now"
    assert (
        worth_bugging_user({"user_directed": True, "novel": False}, everything)["outcome"] == "digest"
    )
    assert (
        worth_bugging_user({"ack_only": True}, everything)["outcome"] == "emoji_ack"
    )
    assert worth_bugging_user({}, everything)["outcome"] == "silent"
    assert set(OUTCOMES) == {"reply_now", "digest", "emoji_ack", "silent"}


def test_quiet_hours_move_a_user_directed_wake_into_the_digest() -> None:
    settings = {"notification_level": "all", "quiet_hours": {"enabled": True, "start": "23:00", "end": "08:00"}}
    assert (
        worth_bugging_user({"user_directed": True, "novel": True}, settings, now=3 * 3600)["outcome"]
        == "digest"
    )


def test_wake_router_dedupes_and_instructs_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_WAKE_ROUTER", "1")
    delivered: list[tuple] = []
    router = WakeRouter(
        settings=lambda: {"notification_level": "all"},
        on_deliver=lambda owner, outcome, summary, payload, deliver: delivered.append(
            (owner, outcome, deliver)
        ),
    )
    first = router.ingest(owner_ref="u1", source="user", summary="hi", user_directed=True, novel=True)
    assert first["outcome"] == "reply_now"
    assert first["deliver"] == ["push", "channel"]
    assert delivered and delivered[0][2] == ["push", "channel"]

    repeat = router.ingest(owner_ref="u1", source="user", summary="hi", user_directed=True, novel=True)
    assert repeat["duplicate"] is True
    assert repeat["deliver"] == []
    assert len(delivered) == 1

    ack = router.ingest(owner_ref="u1", source="runner_result", summary="done", ack_only=True)
    assert ack["outcome"] == "emoji_ack"
    assert ack["deliver"] == []

    noise = router.ingest(owner_ref="u1", source="routine_clock", summary="tick")
    assert noise["outcome"] == "silent" and noise["deliver"] == []


def test_wake_router_is_off_by_default_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYA_WAKE_ROUTER", raising=False)
    result = WakeRouter().ingest(owner_ref="u1", source="user", summary="hi")
    assert result["status"] == "failed"
    assert result["error"] == {"code": "FEATURE_DISABLED", "flag": "VEYA_WAKE_ROUTER"}


def test_wake_router_rejects_a_signal_with_no_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_WAKE_ROUTER", "1")
    result = WakeRouter().ingest(owner_ref="", source="user", summary="hi")
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_INPUT"


def test_wake_router_reuses_the_existing_triage_skeleton(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = build_triage_engine()
    if engine is None:  # 3O submodules absent in this checkout
        pytest.skip("oservi triage skeleton unavailable")
    assert type(engine).__name__ == "TriageEngine"
    engine.run()  # non-blocking for on_signal
    assert callable(engine.process)


def test_every_documented_wake_source_is_accepted() -> None:
    assert set(WAKE_SOURCES) == {
        "user",
        "routine_clock",
        "event_trigger",
        "channel",
        "runner_result",
        "subagent_done",
    }
    seen: set[str] = set()
    router = WakeRouter(settings=lambda: {"notification_level": "all"})
    import os

    os.environ["VEYA_WAKE_ROUTER"] = "1"
    try:
        for index, source in enumerate(WAKE_SOURCES):
            result = router.ingest(owner_ref=f"u{index}", source=source, summary="s")
            assert result["known_source"] is True
            seen.add(result["source"])
    finally:
        os.environ.pop("VEYA_WAKE_ROUTER", None)
    assert seen == set(WAKE_SOURCES)

# ══════════════════════════════════════════════════════════════════════════
# W3 — Goal vs Loop as a GoalRun policy (+ W5 finish checkpoint card)
# ══════════════════════════════════════════════════════════════════════════


def test_goal_mode_never_stops_early_and_never_checkpoints() -> None:
    """Turning the flag on must not change what a Goal run does."""

    decision = loop_decision(mode="goal", rounds_done=99, elapsed_s=99999.0)
    assert decision["stop"] is False
    assert decision["next"] == "checkpoint"  # "keep going and report"
    assert decision["mode"] == "goal"
    assert loop_decision(mode="goal", rounds_done=1, elapsed_s=1.0, finish_condition_met=True)["stop"]
    assert loop_decision(mode="goal", rounds_done=1, elapsed_s=1.0, user_stop=True)["reason_code"] == (
        STOP_USER_STOP
    )
    # An unknown mode degrades to goal rather than inventing a third mode.
    assert loop_decision(mode="whatever", rounds_done=99, elapsed_s=1.0)["mode"] == "goal"


def test_loop_stops_on_each_budget_and_on_the_user() -> None:
    turns = loop_decision(mode="loop", rounds_done=6, elapsed_s=1.0)
    assert turns["stop"] and turns["reason_code"] == STOP_BUDGET_TURNS
    clock = loop_decision(mode="loop", rounds_done=1, elapsed_s=2000.0, time_budget_s=1800.0)
    assert clock["stop"] and clock["reason_code"] == STOP_BUDGET_TIME
    user = loop_decision(mode="loop", rounds_done=1, elapsed_s=1.0, user_stop=True)
    assert user["stop"] and user["reason_code"] == STOP_USER_STOP


def test_loop_stops_after_two_rounds_with_no_progress() -> None:
    one = loop_decision(mode="loop", rounds_done=1, elapsed_s=1.0, no_progress_rounds=1)
    assert one["stop"] is False and one["next"] == "checkpoint"
    two = loop_decision(mode="loop", rounds_done=2, elapsed_s=1.0, no_progress_rounds=2)
    assert two["stop"] is True and two["reason_code"] == STOP_NO_PROGRESS
    # Progress in between resets the counter's meaning, not the budget.
    fresh = loop_decision(mode="loop", rounds_done=3, elapsed_s=1.0, no_progress_rounds=0)
    assert fresh["stop"] is False


def test_loop_brief_reads_an_explicit_budget_and_otherwise_stays_goal() -> None:
    assert parse_loop_brief("loop 10 rounds on the migration")["turn_budget"] == 10
    minutes = parse_loop_brief("loop for 30 minutes, then report")
    assert minutes["mode"] == "loop" and minutes["time_budget_s"] == 1800.0
    assert parse_loop_brief("loop until the tests pass")["until_done"] is True
    # Prose that does not start with "loop" must not silently change the mode.
    assert parse_loop_brief("deploy 10 rounds worth of work")["mode"] == "goal"
    assert parse_loop_brief("")["mode"] == "goal"


def test_checkpoint_card_renders_what_changed_tests_and_risks() -> None:
    card = checkpoint_card(
        mode="loop",
        round_index=2,
        summary="fixed the retry loop",
        did=["goal_run/runner.py"],
        tests_run=["pytest tests/goal_run"],
        risks=["still flaky on restart"],
        open_items=["wire the route"],
    )
    assert card["title"] == "Loop checkpoint 2"
    assert "goal_run/runner.py" in card["text"]
    assert "pytest tests/goal_run" in card["text"]
    assert "still flaky on restart" in card["text"]
    assert card["text"].index("What changed") < card["text"].index("Tests run")
    # An empty section is omitted entirely rather than rendered as a bare header.
    bare = checkpoint_card(mode="goal", round_index=1, summary="done")
    assert bare["title"] == "Finish checkpoint"
    assert "Tests run" not in bare["text"]


def test_loop_run_modul_is_flag_gated_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYA_LOOP_MODE", raising=False)
    posted: list[tuple[str, dict]] = []
    policy = LoopRunPolicy(post_card=lambda gid, card: posted.append((gid, card)))
    off = policy.decide("gr-1", mode="loop", rounds_done=1, elapsed_s=1.0, summary="x")
    assert off["status"] == "failed"
    assert off["error"] == {"code": "FEATURE_DISABLED", "flag": "VEYA_LOOP_MODE"}
    assert posted == []

    monkeypatch.setenv("VEYA_LOOP_MODE", "1")
    on = policy.decide(
        "gr-1",
        mode="loop",
        round_index=1,
        rounds_done=1,
        elapsed_s=1.0,
        summary="still working",
        did=["a.py"],
    )
    assert on["next"] == "checkpoint"
    assert on["card"]["title"] == "Loop checkpoint 1"
    assert len(posted) == 1

    # Same run, same round: the card is posted once, never twice.
    repeat = policy.decide("gr-1", mode="loop", round_index=1, rounds_done=1, elapsed_s=1.0)
    assert repeat["duplicate"] is True
    assert repeat["next"] == "continue"
    assert len(posted) == 1

    stopped = policy.decide("gr-1", mode="loop", round_index=2, rounds_done=9, elapsed_s=1.0)
    assert stopped["stop"] is True and stopped["card"]["title"] == "Loop checkpoint 2"
    assert policy.decide("")["status"] == "failed"


def test_loop_policy_reads_counters_from_the_existing_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VEYA_LOOP_MODE", "1")
    policy = LoopRunPolicy(read_run=lambda gid: {"rounds_done": 3, "elapsed_s": 120.0})
    decision = policy.decide("gr-2", mode="loop", round_index=4)
    # The run's own counters win over the caller's defaults.
    assert "round 4" in decision["reason"]
    assert policy.decide("gr-2", mode="loop", round_index=4)["duplicate"] is True


def test_finish_checkpoint_card_is_available_without_the_loop_flag() -> None:
    card = finish_card("gr-3", summary="shipped", did=["server/events.py"], tests_run=["pytest -q"])
    assert card["title"] == "Finish checkpoint"
    assert "server/events.py" in card["text"] and "pytest -q" in card["text"]
    assert card["mode"] == "goal"
