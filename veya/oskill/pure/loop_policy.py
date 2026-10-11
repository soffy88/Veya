"""3O-PURE — loop_policy: Goal vs Loop as a GoalRun *policy* (W3).

``Goal`` is the existing behaviour: keep going until the stated finish
condition is met. ``Loop`` is the same GoalRun with a budget: keep working
inside the user's brief for a turn/time budget, post a checkpoint each round,
and stop on budget, on an explicit user stop, or on two rounds with no
progress.

This module is **only** the stop/checkpoint arithmetic. It is not a scheduler,
it holds no state, and it never creates a run: the caller passes the current
round counters and gets back a decision. The authority to stop stays
``GoalRunState`` / the existing runner; this oskill tells it *when*.

Reuse (single-source, §1.4): the "no progress" notion already exists twice in
the repo — :class:`runtime.execution.no_progress.NoProgressGuard` and
:func:`server.no_progress.detect_no_progress` (idle-cycle counting). This module
does **not** re-implement either: it takes the caller's already-decided
``no_progress`` boolean and only owns the *loop* budget rules on top.

No IO, no clock, no ``user_id``: the caller supplies ``now``.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "LOOP_MODES",
    "MODE_GOAL",
    "MODE_LOOP",
    "STOP_BUDGET_TIME",
    "STOP_BUDGET_TURNS",
    "STOP_COMPLETED",
    "STOP_CONTINUE",
    "STOP_NO_PROGRESS",
    "STOP_USER_STOP",
    "checkpoint_card",
    "loop_decision",
    "parse_loop_brief",
]

MODE_GOAL = "goal"
MODE_LOOP = "loop"

#: The two GoalRun modes. Anything unrecognised degrades to ``goal`` (the
#: pre-existing behaviour) rather than inventing a third mode.
LOOP_MODES: tuple[str, ...] = (MODE_GOAL, MODE_LOOP)

STOP_CONTINUE = "continue"
STOP_BUDGET_TURNS = "budget_turns"
STOP_BUDGET_TIME = "budget_time"
STOP_USER_STOP = "user_stop"
STOP_NO_PROGRESS = "no_progress"
STOP_COMPLETED = "completed"

#: Rounds in a row with no progress before a loop gives up (spec: 2).
NO_PROGRESS_ROUNDS = 2

DEFAULT_TURN_BUDGET = 6
DEFAULT_TIME_BUDGET_S = 1800.0


def parse_loop_brief(text: str) -> dict[str, Any]:
    """Read a loop budget out of the user's own words.

    Only explicit, unambiguous forms are read — ``loop 10 rounds``,
    ``loop for 30 minutes``, ``loop until done``. Anything else yields
    ``mode="goal"`` with the default budgets, because a budget invented from
    ambiguous prose would silently change when work stops.

    Args:
        text: the user's brief.

    Returns:
        ``{"mode", "turn_budget", "time_budget_s", "until_done", "reason"}``.

    Example:
        >>> parse_loop_brief("loop 10 rounds on the migration")["turn_budget"]
        10
    """

    lowered = str(text or "").strip().lower()
    if not lowered.startswith("loop"):
        return _goal_brief("no loop directive in the brief")
    if "until" in lowered:
        # "loop until done" — runs on the Goal criterion, budget is the cap.
        return {
            "mode": MODE_LOOP,
            "turn_budget": DEFAULT_TURN_BUDGET,
            "time_budget_s": DEFAULT_TIME_BUDGET_S,
            "until_done": True,
            "reason": "loop until the finish condition is met, capped by the budget",
        }

    turns = _first_int(lowered, "round", "turn", "轮")
    if turns is not None:
        return {
            "mode": MODE_LOOP,
            "turn_budget": max(1, turns),
            "time_budget_s": DEFAULT_TIME_BUDGET_S,
            "until_done": False,
            "reason": f"loop with an explicit budget of {turns} rounds",
        }

    minutes = _first_int(lowered, "minute", "min", "分钟")
    if minutes is not None:
        return {
            "mode": MODE_LOOP,
            "turn_budget": DEFAULT_TURN_BUDGET,
            "time_budget_s": float(minutes) * 60.0,
            "until_done": False,
            "reason": f"loop with an explicit budget of {minutes} minutes",
        }
    return {
        "mode": MODE_LOOP,
        "turn_budget": DEFAULT_TURN_BUDGET,
        "time_budget_s": DEFAULT_TIME_BUDGET_S,
        "until_done": False,
        "reason": "loop directive with no explicit budget; defaults applied",
    }


def loop_decision(
    *,
    mode: str,
    rounds_done: int,
    elapsed_s: float,
    turn_budget: int = DEFAULT_TURN_BUDGET,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
    no_progress_rounds: int = 0,
    user_stop: bool = False,
    finish_condition_met: bool = False,
) -> dict[str, Any]:
    """Should this run keep going, stop, or post a checkpoint?

    Args:
        mode: ``"goal"`` or ``"loop"``.
        rounds_done: rounds completed so far.
        elapsed_s: seconds since the run started (caller-supplied clock).
        turn_budget: maximum rounds in loop mode.
        time_budget_s: maximum wall seconds in loop mode.
        no_progress_rounds: consecutive rounds the caller's own progress
            detector (the existing ``NoProgressGuard`` / ``ProgressTracker``)
            reported no progress in.
        user_stop: the user explicitly asked it to stop.
        finish_condition_met: the Goal finish condition is satisfied.

    Returns:
        ``{"stop", "reason", "next": "continue"|"checkpoint"|"stop", "mode",
        "rounds_done", "budget"}``.

    Example:
        >>> loop_decision(mode="loop", rounds_done=2, elapsed_s=1.0)["next"]
        'checkpoint'
    """

    resolved = str(mode or MODE_GOAL).strip().lower()
    if resolved not in LOOP_MODES:
        resolved = MODE_GOAL

    # Goal mode is untouched: it stops only on the finish condition or an
    # explicit user stop, exactly as before this module existed.
    if resolved == MODE_GOAL:
        if user_stop:
            return _decision(MODE_GOAL, True, STOP_USER_STOP, "the user asked the run to stop")
        if finish_condition_met:
            return _decision(MODE_GOAL, True, STOP_COMPLETED, "the finish condition is met")
        return _decision(MODE_GOAL, False, STOP_CONTINUE, "goal mode: keep going until the finish condition")

    if user_stop:
        return _decision(MODE_LOOP, True, STOP_USER_STOP, "the user asked the loop to stop")
    if no_progress_rounds >= NO_PROGRESS_ROUNDS:
        return _decision(
            MODE_LOOP,
            True,
            STOP_NO_PROGRESS,
            f"no progress for {no_progress_rounds} rounds (threshold {NO_PROGRESS_ROUNDS})",
        )
    if finish_condition_met:
        return _decision(MODE_LOOP, True, STOP_COMPLETED, "the finish condition is met")
    if rounds_done >= int(turn_budget):
        return _decision(
            MODE_LOOP, True, STOP_BUDGET_TURNS, f"round budget of {int(turn_budget)} reached"
        )
    if float(elapsed_s) >= float(time_budget_s):
        return _decision(
            MODE_LOOP, True, STOP_BUDGET_TIME, f"time budget of {float(time_budget_s):.0f}s reached"
        )
    return _decision(
        MODE_LOOP,
        False,
        STOP_CONTINUE,
        f"loop round {int(rounds_done) + 1} of {int(turn_budget)} "
        f"({float(elapsed_s):.0f}s/{float(time_budget_s):.0f}s)",
    )


def checkpoint_card(
    *,
    mode: str,
    round_index: int,
    summary: str = "",
    did: list[str] | None = None,
    open_items: list[str] | None = None,
    tests_run: list[str] | None = None,
    risks: list[str] | None = None,
    next_step: str = "",
) -> dict[str, Any]:
    """Build one round checkpoint / finish card (spec W3 and W5 share it).

    The card is the user-visible "what changed / tests run / open risks" record
    posted at the end of a loop round and just before a run marks itself done.

    Args:
        mode: ``"goal"`` or ``"loop"``.
        round_index: 1-based round number.
        summary: one line of prose.
        did: what changed this round.
        open_items: what is still open.
        tests_run: commands or checks that were actually run.
        risks: open risks.
        next_step: what the next round will do.

    Returns:
        ``{"card_id", "mode", "round", "title", "summary", "did", "open_items",
        "tests_run", "risks", "next_step", "text"}`` — ``text`` is the rendered
        markdown the UI posts verbatim.

    Example:
        >>> card = checkpoint_card(mode="loop", round_index=1, summary="did a thing")
        >>> card["title"]
        'Loop checkpoint 1'
    """

    index = max(1, int(round_index))
    done = [str(item) for item in (did or []) if str(item).strip()]
    open_left = [str(item) for item in (open_items or []) if str(item).strip()]
    checks = [str(item) for item in (tests_run or []) if str(item).strip()]
    open_risks = [str(item) for item in (risks or []) if str(item).strip()]

    if str(mode or MODE_GOAL).strip().lower() == MODE_LOOP:
        title = f"Loop checkpoint {index}"
    else:
        title = "Finish checkpoint"

    lines = [f"**{title}**"]
    if summary.strip():
        lines.append(str(summary).strip())
    lines.extend(_section("What changed", done))
    lines.extend(_section("Tests run", checks))
    lines.extend(_section("Open risks", open_risks))
    lines.extend(_section("Still open", open_left))
    if str(next_step or "").strip():
        lines.append(f"Next: {str(next_step).strip()}")

    card_id = f"{MODE_LOOP if str(mode).strip().lower() == MODE_LOOP else MODE_GOAL}-{index}"
    return {
        "card_id": card_id,
        "mode": str(mode or MODE_GOAL).strip().lower(),
        "round": index,
        "title": title,
        "summary": str(summary or "").strip(),
        "did": done,
        "open_items": open_left,
        "tests_run": checks,
        "risks": open_risks,
        "next_step": str(next_step or "").strip(),
        "text": "\n".join(lines),
    }


def _section(heading: str, items: list[str]) -> list[str]:
    """One rendered section, omitted entirely when it has nothing in it."""

    if not items:
        return []
    return [f"_{heading}:_"] + [f"- {item}" for item in items]


def _decision(mode: str, stop: bool, reason_code: str, reason: str) -> dict[str, Any]:
    return {
        "stop": stop,
        "next": "stop" if stop else "checkpoint",
        "reason_code": reason_code,
        "reason": reason,
        "mode": mode,
    }


def _goal_brief(reason: str) -> dict[str, Any]:
    return {
        "mode": MODE_GOAL,
        "turn_budget": DEFAULT_TURN_BUDGET,
        "time_budget_s": DEFAULT_TIME_BUDGET_S,
        "until_done": False,
        "reason": reason,
    }


def _first_int(text: str, *units: str) -> int | None:
    """First ``<number> <unit>`` in ``text``; ``None`` when there is none.

    Whitespace between the number and the unit is allowed, because that is how
    people write it ("loop 10 rounds", "loop for 30 minutes").
    """

    for unit in units:
        marker = f"{unit}"
        index = text.find(marker)
        while index != -1:
            digits = ""
            cursor = index - 1
            while cursor >= 0 and text[cursor].isspace():
                cursor -= 1
            while cursor >= 0 and text[cursor].isdigit():
                digits = text[cursor] + digits
                cursor -= 1
            if digits:
                try:
                    return int(digits)
                except ValueError:
                    break
            index = text.find(marker, index + 1)
    return None
