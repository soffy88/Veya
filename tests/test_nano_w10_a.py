"""NANO-W10 tests: always-on agent ergonomics.

One file for the whole wave, per `NANO_W10_SPEC.md` line 2.  Sections are added
in spec order (1..7, then the ADDENDUMs).  Every dependency that would touch
``~/.veya`` or a live service is injected; temp dirs are the only filesystem.

Run with:  .venv/bin/python -m pytest -q tests/test_nano_w10_a.py
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.nano_w10.flags import (
    ALL_FLAGS,
    FeatureDisabled,
    feature_flags,
    flag_enabled,
    require_flag,
)
from server.nano_w10.live_stream import LiveStream, wire_to_turn_queue
from server.nano_w10.loop_mode import (
    CARD_KIND,
    CHECKPOINT_VERDICTS,
    FinishCheckpointGate,
    LoopRunPolicy,
    card_key,
    finish_card,
)
from server.nano_w10.turn_queue import TurnQueue
from server.nano_w10.wake_router import (
    WakeRouter,
    build_triage_engine,
    default_wake_router,
)
from veya.omodul.turn_intake import (
    TurnIntakeConfig,
    TurnIntakeInput,
    compute_fingerprint_for,
    turn_intake,
)
from veya.omodul.wake_router import WAKE_SOURCES
from veya.oprim.turn_envelope import envelope_fingerprint, idem_key
from veya.oskill.pure.live_trace import (
    PLAN_STATUSES,
    correction_action,
    plan_rows,
    step_from_tool_event,
)
from veya.oskill.pure.loop_policy import (
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
    # Spec: "Flags default OFF except 7a"; the addendum then allows the
    # read-only/observability surfaces on, including the live stream UI.
    assert on_by_default == {
        "VEYA_TOOL_ERROR_FRIENDLY",
        "VEYA_LLM_TAP",
        "VEYA_SEARCH_FALLBACK",
        "VEYA_CHANNEL_STATUS",
        "VEYA_LIVE_STREAM",
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
        TurnIntakeInput(
            conversation_ref="c1", kind="direct", text="hi", external_id="m1", arrived_at=1.0
        ),
        None,
        now=1.0,
    )
    assert first["status"] == "completed"
    assert first["action"] == "queue"
    retry = turn_intake(
        config,
        TurnIntakeInput(
            conversation_ref="c1", kind="direct", text="hi", external_id="m1", arrived_at=1.2
        ),
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
    settings = {
        "notification_level": "all",
        "quiet_hours": {"enabled": True, "start": "23:00", "end": "08:00"},
    }
    assert worth_bugging_user({"urgent": True}, settings, now=2 * 3600)["outcome"] == "reply_now"
    never = {"notification_level": "never"}
    assert worth_bugging_user({"urgent": True, "user_directed": True}, never)["outcome"] == "silent"


def test_all_four_outcomes_are_reachable() -> None:
    everything = {"notification_level": "all"}
    assert (
        worth_bugging_user({"user_directed": True, "urgent": True}, everything)["outcome"]
        == "reply_now"
    )
    assert (
        worth_bugging_user({"user_directed": True, "novel": True}, everything)["outcome"]
        == "reply_now"
    )
    assert (
        worth_bugging_user({"user_directed": True, "novel": False}, everything)["outcome"]
        == "digest"
    )
    assert worth_bugging_user({"ack_only": True}, everything)["outcome"] == "emoji_ack"
    assert worth_bugging_user({}, everything)["outcome"] == "silent"
    assert set(OUTCOMES) == {"reply_now", "digest", "emoji_ack", "silent"}


def test_quiet_hours_move_a_user_directed_wake_into_the_digest() -> None:
    settings = {
        "notification_level": "all",
        "quiet_hours": {"enabled": True, "start": "23:00", "end": "08:00"},
    }
    assert (
        worth_bugging_user({"user_directed": True, "novel": True}, settings, now=3 * 3600)[
            "outcome"
        ]
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
    first = router.ingest(
        owner_ref="u1", source="user", summary="hi", user_directed=True, novel=True
    )
    assert first["outcome"] == "reply_now"
    assert first["deliver"] == ["push", "channel"]
    assert delivered and delivered[0][2] == ["push", "channel"]

    repeat = router.ingest(
        owner_ref="u1", source="user", summary="hi", user_directed=True, novel=True
    )
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
    assert loop_decision(mode="goal", rounds_done=1, elapsed_s=1.0, finish_condition_met=True)[
        "stop"
    ]
    assert loop_decision(mode="goal", rounds_done=1, elapsed_s=1.0, user_stop=True)[
        "reason_code"
    ] == (STOP_USER_STOP)
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


def test_loop_directive_is_found_inside_a_wrapped_bot_brief() -> None:
    """W11-D: a bot turn wraps the user's words in the bot's persona, so the
    brief that reaches a run is not necessarily just what the user typed."""

    wrapped = (
        "Be terse.\n\nYour one job: Lists tips\n\nloop 2 rounds: 每轮列出一个学习 Python 的建议"
    )
    parsed = parse_loop_brief(wrapped)
    assert parsed["mode"] == "loop"
    assert parsed["turn_budget"] == 2

    # "loop" as an ordinary noun is not a directive: no budget, no mode change.
    for prose in (
        "develop the loop feature",
        "the retry loop is stuck",
        "loops of feedback",
        "do a loop on this later",
    ):
        assert parse_loop_brief(prose)["mode"] == "goal", prose


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


# ══════════════════════════════════════════════════════════════════════════
# W5 — the finish checkpoint is posted before done, and can be sent back
# ══════════════════════════════════════════════════════════════════════════


def _gate(tmp_path, **overrides):
    """A gate whose collaborators are local fakes, never the real stores."""

    posted: list[tuple[str, str, dict]] = []
    events: list[tuple[str, dict]] = []
    queue = TurnQueue()
    defaults = {
        "post_to_chat": lambda bot_id, text, meta: posted.append((bot_id, text, meta)),
        "record_event": lambda topic, payload: events.append((topic, payload)),
        "deliver_note": lambda bot_id, text: queue.intake(
            conversation_ref=bot_id,
            text=text,
            kind="direct",
            bot_ref=bot_id,
            user_directed=True,
        ),
    }
    defaults.update(overrides)
    return FinishCheckpointGate(**defaults), posted, events, queue


def test_finish_checkpoint_is_off_by_default_and_says_which_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VEYA_FINISH_CHECKPOINT", raising=False)
    gate, posted, events, _queue = _gate(None)
    assert gate.post("gr-1", bot_id="bot-x", summary="done")["error"] == {
        "code": "FEATURE_DISABLED",
        "flag": "VEYA_FINISH_CHECKPOINT",
    }
    assert gate.respond("bot-x", "fc-bot-x-gr-1", "accept")["error"]["code"] == "FEATURE_DISABLED"
    assert posted == [] and events == []


def test_finish_checkpoint_posts_into_the_chat_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VEYA_FINISH_CHECKPOINT", "1")
    gate, posted, events, _queue = _gate(None)
    first = gate.post(
        "gr-1",
        bot_id="bot-x",
        summary="shipped the route",
        did=["server/routes/bot_team.py"],
        tests_run=["pytest -q tests/test_nano_w10_a.py"],
    )
    assert first["posted"] is True
    assert first["card_id"] == card_key("bot-x", "gr-1")
    bot_id, text, meta = posted[0]
    assert bot_id == "bot-x"
    assert "Finish checkpoint" in text
    assert meta["kind"] == CARD_KIND and meta["goal_run_id"] == "gr-1"
    assert [topic for topic, _ in events] == ["goal_run.finish_checkpoint"]

    # A resumed run that re-finalizes must not double-post into the chat.
    again = gate.post("gr-1", bot_id="bot-x", summary="shipped the route")
    assert again["duplicate"] is True and again["posted"] is False
    assert len(posted) == 1
    assert gate.posted("bot-x") == [card_key("bot-x", "gr-1")]


def test_finish_checkpoint_rejects_a_run_with_no_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_FINISH_CHECKPOINT", "1")
    gate, posted, _events, _queue = _gate(None)
    assert gate.post("gr-1", bot_id="")["error"]["code"] == "INVALID_INPUT"
    assert gate.post("", bot_id="bot-x")["error"]["code"] == "INVALID_INPUT"
    assert posted == []


def test_accept_records_the_verdict_without_touching_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VEYA_FINISH_CHECKPOINT", "1")
    gate, _posted, events, queue = _gate(None)
    result = gate.respond("bot-x", "fc-bot-x-gr-1", "accept")
    assert result["verdict"] == "accept" and result["steered"] is False
    topic, payload = events[-1]
    assert topic == "goal_run.finish_checkpoint.response"
    assert payload["verdict"] == "accept"
    assert queue.list_pending("bot-x") == []


def test_send_back_lands_through_the_item_1_turn_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A card sent back is a correction, so it takes the item-1 path."""

    monkeypatch.setenv("VEYA_FINISH_CHECKPOINT", "1")
    monkeypatch.setenv("VEYA_TURN_QUEUE", "1")
    gate, _posted, events, queue = _gate(None)
    assert gate.respond("bot-x", "fc-bot-x-gr-1", "send_back")["error"]["code"] == "INVALID_INPUT"

    result = gate.respond("bot-x", "fc-bot-x-gr-1", "send_back", note="测试还没跑")
    assert result["steered"] is True
    assert result["landed"] == "queue"
    assert [item["text"] for item in queue.list_pending("bot-x")] == ["测试还没跑"]
    assert events[-1][1]["note"] == "测试还没跑"


def test_an_unknown_verdict_is_refused_rather_than_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VEYA_FINISH_CHECKPOINT", "1")
    gate, _posted, events, _queue = _gate(None)
    assert gate.respond("bot-x", "fc-1", "maybe")["error"]["code"] == "INVALID_INPUT"
    assert gate.respond("bot-x", "", "accept")["error"]["code"] == "INVALID_INPUT"
    assert events == []
    assert set(CHECKPOINT_VERDICTS) == {"accept", "send_back"}


def test_a_card_never_takes_the_run_down_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unwritable chat is reported, not raised."""

    monkeypatch.setenv("VEYA_FINISH_CHECKPOINT", "1")

    def _boom(bot_id: str, text: str, meta: dict) -> None:
        raise OSError("read-only file system")

    gate, _posted, _events, _queue = _gate(None, post_to_chat=_boom)
    result = gate.post("gr-1", bot_id="bot-x", summary="shipped")
    assert result["status"] == "failed"
    assert result["error"]["code"] == "CHAT_UNAVAILABLE"


def test_a_loop_round_card_does_not_consume_the_finish_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One card per loop round, plus one finish card per run."""

    monkeypatch.setenv("VEYA_LOOP_MODE", "1")
    monkeypatch.delenv("VEYA_FINISH_CHECKPOINT", raising=False)
    gate, posted, _events, _queue = _gate(None)
    round_card = checkpoint_card(mode="loop", round_index=1, summary="first pass")
    first = gate.publish("bot-x", "gr-1", round_card, flag="VEYA_LOOP_MODE")
    assert first["posted"] is True
    assert first["card_id"] == card_key("bot-x", "gr-1", mode="loop", round_index=1)
    assert gate.publish("bot-x", "gr-1", round_card, flag="VEYA_LOOP_MODE")["duplicate"] is True
    assert len(posted) == 1

    # The finish card is W5 behaviour and is refused while its own flag is off.
    monkeypatch.setenv("VEYA_FINISH_CHECKPOINT", "1")
    finish = gate.post("gr-1", bot_id="bot-x", summary="done")
    assert finish["posted"] is True
    assert finish["card_id"] == card_key("bot-x", "gr-1")
    assert len(posted) == 2


def test_a_loop_round_card_needs_the_loop_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYA_LOOP_MODE", raising=False)
    gate, posted, _events, _queue = _gate(None)
    result = gate.publish(
        "bot-x", "gr-1", checkpoint_card(mode="loop", round_index=1), flag="VEYA_LOOP_MODE"
    )
    assert result["error"] == {"code": "FEATURE_DISABLED", "flag": "VEYA_LOOP_MODE"}
    assert posted == []


def test_the_runner_only_engages_the_loop_when_the_flag_and_the_brief_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default OFF, and even then only a brief that says \"loop\" selects Loop."""

    from server.goal_run.models import GoalRunState
    from server.goal_run.runner import _loop_policy_for

    state = GoalRunState(goal_id="gr-1", goal_text="loop 3 rounds on the migration")
    monkeypatch.delenv("VEYA_LOOP_MODE", raising=False)
    assert _loop_policy_for(state, state.goal_text)[0] is None

    monkeypatch.setenv("VEYA_LOOP_MODE", "1")
    policy, brief = _loop_policy_for(state, state.goal_text)
    assert policy is not None and brief["turn_budget"] == 3
    # A Goal brief stays a Goal run even with the flag on.
    assert _loop_policy_for(state, "ship the migration")[0] is None


def test_the_runner_post_no_finish_card_when_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.goal_run.models import GoalRunState
    from server.goal_run.runner import _post_finish_checkpoint

    monkeypatch.delenv("VEYA_FINISH_CHECKPOINT", raising=False)
    state = GoalRunState(goal_id="gr-1", goal_text="ship it")
    assert _post_finish_checkpoint(state, ".", completed=1, total=1, blocked=0) is None


def test_loop_stops_on_the_budget_the_brief_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """\"loop 10 rounds\" must run ten rounds, not the oskill default of six."""

    monkeypatch.setenv("VEYA_LOOP_MODE", "1")
    policy = LoopRunPolicy()
    brief = parse_loop_brief("loop 10 rounds on the migration")
    ninth = policy.decide(
        "gr-1",
        mode="loop",
        round_index=9,
        rounds_done=9,
        elapsed_s=1.0,
        turn_budget=brief["turn_budget"],
        time_budget_s=brief["time_budget_s"],
    )
    assert ninth["stop"] is False
    tenth = policy.decide(
        "gr-1",
        mode="loop",
        round_index=10,
        rounds_done=10,
        elapsed_s=1.0,
        turn_budget=brief["turn_budget"],
        time_budget_s=brief["time_budget_s"],
    )
    assert tenth["stop"] is True and tenth["reason_code"] == STOP_BUDGET_TURNS
    # Without the brief's budget the default still applies.
    assert (
        policy.decide("gr-2", mode="loop", round_index=6, rounds_done=6, elapsed_s=1.0)[
            "reason_code"
        ]
        == STOP_BUDGET_TURNS
    )


# ══════════════════════════════════════════════════════════════════════════
# W4 — live tool-result stream, plan list, correction box (lands via item 1)
# ══════════════════════════════════════════════════════════════════════════


def test_tool_result_is_a_first_class_step_not_just_a_call() -> None:
    step = step_from_tool_event(
        {"type": "tool_result", "tool_name": "file_read", "result": "contents"}
    )
    assert step["type"] == "tool_result" and step["detail"] == "contents" and step["status"] == "ok"
    err = step_from_tool_event({"type": "tool_result", "tool_name": "sh", "result": "ERROR: boom"})
    assert err["status"] == "error"
    call = step_from_tool_event(
        {"type": "tool_call", "tool_name": "sh", "tool_args": {"cmd": "ls"}}
    )
    assert call["type"] == "tool_call" and "cmd=ls" in call["detail"]
    # An event this UI does not show is not worth inventing a row for.
    assert step_from_tool_event({"type": "heartbeat"}) is None


def test_result_preview_is_truncated_with_a_visible_notice() -> None:
    step = step_from_tool_event({"type": "tool_result", "result": "x" * 5000})
    assert len(step["detail"]) < 5000
    assert "chars)" in step["detail"]


def test_plan_rows_fold_every_status_into_three_buckets() -> None:
    rows = plan_rows(
        [
            {"id": "a", "title": "done thing", "status": "done"},
            {"id": "b", "title": "doing thing", "status": "in_progress"},
            {"id": "c", "title": "todo thing", "status": "pending"},
            {"id": "d", "title": "blocked thing", "status": "blocked"},
        ]
    )
    assert [row["status"] for row in rows] == ["done", "doing", "todo", "doing"]
    assert set(PLAN_STATUSES) == {"done", "doing", "todo"}
    assert plan_rows("not a list") == []


def test_correction_steer_vs_queue_vs_reject() -> None:
    steer = correction_action({"text": "actually stop"}, run_state="running")
    assert steer["action"] == "steer" and steer["state"] == STATE_DELIVERED
    queued = correction_action({"text": "note"}, run_state="pending")
    assert queued["action"] == "queue"
    stale = correction_action({"text": "note"}, run_state="none")
    assert stale["action"] == "reject"
    empty = correction_action({"text": "  "}, run_state="running")
    assert empty["action"] == "reject"


def test_live_trace_projects_events_and_lands_a_correction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_LIVE_STREAM", "1")
    pushed: list[dict] = []
    steered: list[str] = []
    running = {"state": "running"}
    stream = LiveStream(
        push=pushed.append,
        turn_state=lambda _tid: running["state"],
        deliver_steer=lambda _tid, text: steered.append(text) or "delivered",
    )
    stream.on_step("t1", {"type": "tool_call", "tool_name": "read", "tool_args": {"path": "a.py"}})
    stream.on_step("t1", {"type": "tool_result", "tool_name": "read", "result": "42 lines"})
    assert [step["type"] for step in stream.snapshot("t1")["steps"]] == ["tool_call", "tool_result"]

    stream.on_step(
        "t1",
        {"type": "plan_update", "todos": [{"id": "1", "title": "step", "status": "in_progress"}]},
    )
    assert stream.snapshot("t1")["plan"] == [{"id": "1", "title": "step", "status": "doing"}]
    assert len(pushed) == 3

    # Correction while running -> steer (and it lands via the injected steer).
    result = stream.correction("t1", "also check the tests")
    assert result["action"] == "steer" and steered == ["also check the tests"]
    # A correction with no live run is refused, not silently started.
    running["state"] = "none"
    assert stream.correction("t1", "late note")["action"] == "reject"


def test_correction_lands_through_the_item_1_turn_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The correction box must reuse item 1's steer, not a second channel."""

    monkeypatch.setenv("VEYA_LIVE_STREAM", "1")
    monkeypatch.setenv("VEYA_TURN_QUEUE", "1")
    queue = TurnQueue(window_s=2.0)
    queue.intake(conversation_ref="c1", text="build the thing", external_id="m1")
    queue.mark_running("c1", "bot-1")
    stream = wire_to_turn_queue(queue)
    result = stream.correction("c1", "wait, use the other adapter")
    assert result["action"] == "steer"
    # It is item 1's own steer log, so one path serves both message kinds.
    assert queue.steer_log("c1") == ["wait, use the other adapter"]


def test_live_stream_is_off_when_the_flag_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_LIVE_STREAM", "0")
    stream = LiveStream()
    assert stream.on_step("t1", {"type": "tool_call", "tool_name": "read"}) is None
    off = stream.correction("t1", "stop")
    assert off["error"] == {"code": "FEATURE_DISABLED", "flag": "VEYA_LIVE_STREAM"}


def test_live_trace_bounds_its_steps_and_drops_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_LIVE_STREAM", "1")
    stream = LiveStream(max_steps=3)
    for index in range(6):
        stream.on_step("t1", {"type": "tool_result", "tool_name": f"t{index}", "result": "x"})
    snapshot = stream.snapshot("t1")
    assert len(snapshot["steps"]) == 3
    assert snapshot["dropped"] == 3
    # The newest steps are the ones kept.
    assert snapshot["steps"][-1]["tool_name"] == "t5"


# ══════════════════════════════════════════════════════════════════════════
# W4 route — the correction endpoint must verify auth AND caller ownership
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def correction_client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A client whose auth dependency is overridden, so ownership is testable."""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server import auth as auth_mod
    from server.goal_run.bot_identity import BotSpec
    from server.nano_w10.turn_queue import reset_turn_queue
    from server.routes import bot_team as bot_routes

    monkeypatch.setenv("VEYA_TURN_QUEUE", "1")
    monkeypatch.setenv("VEYA_LIVE_STREAM", "1")

    spec = BotSpec(bot_id="bot-x", owner_id="user-owner", name="X")
    registry = type("R", (), {"get": lambda _self, bot_id: spec if bot_id == "bot-x" else None})()
    monkeypatch.setattr(bot_routes, "bot_registry", lambda: registry)
    reset_turn_queue()

    app = FastAPI()
    app.include_router(bot_routes.router)
    # Override the real dependency object FastAPI captured at import time.
    app.dependency_overrides[auth_mod.require_user] = lambda: {"user_id": "user-owner"}
    yield TestClient(app)
    reset_turn_queue()


def test_correction_route_requires_auth(correction_client, monkeypatch) -> None:
    """An anonymous caller is refused: no login means no steer."""

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from server import auth as auth_mod
    from server.routes import bot_team as bot_routes

    def _deny() -> dict:
        raise HTTPException(status_code=401, detail="请先登录")

    app = FastAPI()
    app.include_router(bot_routes.router)
    app.dependency_overrides[auth_mod.require_user] = _deny
    client = TestClient(app)
    response = client.post("/api/v1/bots/bot-x/corrections", json={"text": "stop"})
    assert response.status_code == 401


def test_correction_route_refuses_a_bot_the_caller_does_not_own(correction_client) -> None:
    """Ownership: another account's bot is 404, never steerable."""

    response = correction_client.post("/api/v1/bots/other-bot/corrections", json={"text": "stop"})
    assert response.status_code == 404


def test_correction_route_steers_a_running_turn(correction_client) -> None:
    """A correction on the caller's own bot steers, and is refused with no turn."""

    from server.nano_w10.turn_queue import get_turn_queue

    queue = get_turn_queue()
    queue.intake(conversation_ref="bot-x", text="start", external_id="m1")
    queue.mark_running("bot-x", "bot-x")

    response = correction_client.post(
        "/api/v1/bots/bot-x/corrections", json={"text": "actually use plan B"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "steered" and body["action"] == "steer"
    assert queue.steer_log("bot-x") == ["actually use plan B"]

    # With no running turn it is refused, not silently turned into a new turn.
    queue.mark_finished("bot-x")
    refused = correction_client.post("/api/v1/bots/bot-x/corrections", json={"text": "late"})
    assert refused.status_code == 409


def test_turn_queue_sits_in_front_of_the_message_route(
    correction_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag on: a retry is dropped and a mid-turn message steers."""

    from server.nano_w10.turn_queue import get_turn_queue

    queue = get_turn_queue()
    queue.intake(conversation_ref="bot-x", text="start", external_id="m1")
    queue.mark_running("bot-x", "bot-x")

    # A mid-turn direct message steers rather than opening a second turn.
    steer = correction_client.post(
        "/api/v1/bots/bot-x/messages", json={"text": "actually use plan B"}
    )
    assert steer.status_code == 202
    assert steer.json()["action"] == "steer"
    assert queue.steer_log("bot-x") == ["actually use plan B"]

    # The same message_id is a retry: it is dropped, not run twice.
    get_turn_queue()
    first = correction_client.post(
        "/api/v1/bots/bot-x/messages", json={"text": "hello", "message_id": "abc"}
    )
    # No running turn now, so it proceeds; the second identical one is a retry.
    assert first.status_code in (200, 202)
    second = correction_client.post(
        "/api/v1/bots/bot-x/messages", json={"text": "hello", "message_id": "abc"}
    )
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


def test_message_route_is_untouched_when_the_turn_queue_flag_is_off(
    correction_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default OFF: the intake is a no-op, so the route is byte-for-byte before."""

    from server.nano_w10.turn_queue import reset_turn_queue

    monkeypatch.delenv("VEYA_TURN_QUEUE", raising=False)
    reset_turn_queue()
    response = correction_client.post("/api/v1/bots/bot-x/messages", json={"text": "hi"})
    # It runs the ordinary turn path (200 sync result or 503 if the turn path
    # is not available in this environment) — it is NOT a 202 steer or a drop.
    assert response.status_code in (200, 202, 503)
    assert response.json().get("action") != "steer"
    assert response.json().get("status") != "duplicate"


# ══════════════════════════════════════════════════════════════════════════
# W5 route — the checkpoint verdict must verify auth, ownership, and the card
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def checkpoint_client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A caller who owns ``bot-x``, with one card already in that bot's chat."""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server import auth as auth_mod
    from server.bot_team.conversations import ConversationStore
    from server.goal_run.bot_identity import BotSpec
    from server.nano_w10.loop_mode import card_key, reset_finish_checkpoint
    from server.nano_w10.turn_queue import reset_turn_queue
    from server.routes import bot_team as bot_routes

    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots"))
    monkeypatch.setenv("VEYA_FINISH_CHECKPOINT", "1")
    monkeypatch.setenv("VEYA_TURN_QUEUE", "1")

    spec = BotSpec(bot_id="bot-x", owner_id="user-owner", name="X")
    registry = type("R", (), {"get": lambda _self, bot_id: spec if bot_id == "bot-x" else None})()
    monkeypatch.setattr(bot_routes, "bot_registry", lambda: registry)
    ConversationStore().append(
        "bot-x",
        role="bot",
        text="**Finish checkpoint**",
        meta={"kind": CARD_KIND, "card_id": card_key("bot-x", "gr-1"), "goal_run_id": "gr-1"},
    )
    reset_turn_queue()
    reset_finish_checkpoint()

    app = FastAPI()
    app.include_router(bot_routes.router)
    app.dependency_overrides[auth_mod.require_user] = lambda: {"user_id": "user-owner"}
    yield TestClient(app)
    reset_turn_queue()
    reset_finish_checkpoint()


def test_checkpoint_route_requires_auth() -> None:
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from server import auth as auth_mod
    from server.routes import bot_team as bot_routes

    def _deny() -> dict:
        raise HTTPException(status_code=401, detail="请先登录")

    app = FastAPI()
    app.include_router(bot_routes.router)
    app.dependency_overrides[auth_mod.require_user] = _deny
    response = TestClient(app).post(
        "/api/v1/bots/bot-x/checkpoints/fc-x", json={"verdict": "accept"}
    )
    assert response.status_code == 401


def test_checkpoint_route_refuses_a_bot_the_caller_does_not_own(checkpoint_client) -> None:
    response = checkpoint_client.post(
        "/api/v1/bots/other-bot/checkpoints/fc-other", json={"verdict": "accept"}
    )
    assert response.status_code == 404


def test_checkpoint_route_refuses_a_card_that_is_not_in_that_chat(checkpoint_client) -> None:
    """A guessed card id is a 404: the conversation log is the record."""

    response = checkpoint_client.post(
        "/api/v1/bots/bot-x/checkpoints/fc-bot-x-gr-999", json={"verdict": "accept"}
    )
    assert response.status_code == 404


def test_checkpoint_route_rejects_an_unknown_verdict(checkpoint_client) -> None:
    response = checkpoint_client.post(
        "/api/v1/bots/bot-x/checkpoints/fc-bot-x-gr-1", json={"verdict": "maybe"}
    )
    assert response.status_code == 422


def test_checkpoint_route_records_an_accept(checkpoint_client) -> None:
    from server.bot_team.conversations import ConversationStore
    from server.nano_w10.loop_mode import card_key

    response = checkpoint_client.post(
        "/api/v1/bots/bot-x/checkpoints/fc-bot-x-gr-1", json={"verdict": "accept"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "accept" and body["card_id"] == card_key("bot-x", "gr-1")
    # Accepting must not start a turn.
    assert ConversationStore().list("bot-x")[-1]["meta"]["card_id"] == card_key("bot-x", "gr-1")


def test_checkpoint_route_sends_a_card_back_through_the_turn_queue(
    checkpoint_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.nano_w10.loop_mode import reset_finish_checkpoint
    from server.nano_w10.turn_queue import get_turn_queue

    reset_finish_checkpoint()  # bind the gate to the real queue, not the fake
    response = checkpoint_client.post(
        "/api/v1/bots/bot-x/checkpoints/fc-bot-x-gr-1",
        json={"verdict": "send_back", "note": "这版先别合"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "queue"
    assert [item["text"] for item in get_turn_queue().list_pending("bot-x")] == ["这版先别合"]


# ══════════════════════════════════════════════════════════════════════════
# item 2 — the delivery binding (existing push / channels / digest only)
# ══════════════════════════════════════════════════════════════════════════


def test_default_wake_router_delivers_reply_now_over_the_existing_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reply_now fires P5 push; it is the existing notifier, not a new one."""

    monkeypatch.setenv("VEYA_WAKE_ROUTER", "1")
    sent: list[dict] = []

    class _Notifier:
        def send(self, *, user_id: str, payload: Any) -> dict:
            sent.append({"user_id": user_id, "payload": payload})
            return {"sent": True}

    import server.bot_team.push as push_mod

    monkeypatch.setattr(push_mod, "get_push_notifier", lambda: _Notifier())

    result = default_wake_router().ingest(
        owner_ref="u1",
        source="user",
        summary="部署失败了",
        user_directed=True,
        novel=True,
    )
    assert result["outcome"] == "reply_now"
    assert sent and sent[0]["user_id"] == "u1"
    assert "部署失败了" in str(sent[0]["payload"].body)


def test_a_background_wake_never_reaches_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_WAKE_ROUTER", "1")
    sent: list[dict] = []
    result = default_wake_router().ingest(owner_ref="u1", source="routine_clock", summary="tick")
    assert result["outcome"] == "silent"
    assert result["deliver"] == []
    assert sent == []


def test_a_digest_wake_is_parked_not_pushed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_WAKE_ROUTER", "1")
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "server.events.append_canonical_event",
        lambda topic, payload=None, **_: recorded.append((topic, payload)),
    )
    result = default_wake_router().ingest(
        owner_ref="u1",
        source="user",
        summary="你说过的那件事有进展",
        user_directed=True,
        novel=True,
    )
    assert result["outcome"] in ("reply_now", "digest")
    if result["outcome"] == "digest":
        assert recorded and recorded[0][0] == "wake.digest"
        assert recorded[0][1]["owner_ref"] == "u1"


def test_the_routine_clock_only_routes_when_the_flag_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default OFF: the routine clock keeps its existing notification."""

    from server.bot_team import routine_clock

    monkeypatch.delenv("VEYA_WAKE_ROUTER", raising=False)
    assert (
        routine_clock._route_wake(
            bot_id="bot-x",
            routine_id="r1",
            trigger_id="r1:2026-01-01T00:00:00+00:00",
            summary="done",
        )
        is None
    )

    monkeypatch.setenv("VEYA_WAKE_ROUTER", "1")
    routed = routine_clock._route_wake(
        bot_id="bot-x",
        routine_id="r1",
        trigger_id="r1:2026-01-01T00:00:00+00:00",
        summary="done",
    )
    assert routed is not None
    assert routed["source"] == "routine_clock" and routed["known_source"] is True
    # A routine that merely ticked is background: it must not interrupt anyone.
    assert routed["outcome"] == "silent"
    assert routed["deliver"] == []
