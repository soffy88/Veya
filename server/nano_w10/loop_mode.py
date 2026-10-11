"""server/nano_w10/loop_mode — W3 Goal vs Loop, as a GoalRun *policy*.

The run itself is untouched. This module reads the **existing** GoalRun
counters, asks :mod:`veya.omodul.loop_run` whether to continue / checkpoint /
stop, and hands the result back so the existing runner applies it. It creates
no run, starts no timer, and has no queue of its own.

``Goal`` mode (the default, and what every run gets when the flag is off)
takes a path that cannot change behaviour: it reports ``continue`` until the
existing finish condition says otherwise.

Gated by ``VEYA_LOOP_MODE`` (default OFF).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from server.nano_w10.flags import flag_enabled
from veya.omodul.loop_run import LoopRunConfig, LoopRunInput, loop_run, parse_loop_brief
from veya.oskill.pure.loop_policy import MODE_GOAL, MODE_LOOP

_log = logging.getLogger("veya.nano_w10.loop_mode")

LOOP_MODE_FLAG = "VEYA_LOOP_MODE"


def loop_mode_enabled() -> bool:
    """Is Loop mode on? (spec addendum W3, default OFF)."""

    return flag_enabled(LOOP_MODE_FLAG)


class LoopRunPolicy:
    """One GoalRun's loop policy, assembled over the existing run.

    Parameters
    ----------
    read_run:
        ``(goal_run_id) -> dict`` — the existing GoalRun projection. Only
        ``rounds_done``/``elapsed_s``/``finish_condition_met`` are read; this
        module never writes the run.
    post_card:
        ``(goal_run_id, card) -> Any`` — the existing conversation/event sink
        the checkpoint card is posted through. Injected, never re-implemented.
    """

    def __init__(
        self,
        *,
        read_run: Callable[[str], dict[str, Any]] | None = None,
        post_card: Callable[[str, dict[str, Any]], Any] | None = None,
        trail_sink: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._read_run = read_run
        self._post_card = post_card
        self._trail_sink = trail_sink
        self._seen: dict[str, float] = {}

    # ── injected state handles ──────────────────────────────────────────

    def _seen_before(self, fingerprint: str) -> bool:
        if fingerprint in self._seen:
            return True
        cutoff = time.time() - 24 * 3600.0
        for stale in [key for key, at in self._seen.items() if at < cutoff]:
            self._seen.pop(stale, None)
        return False

    def _mark_seen(self, fingerprint: str, _card: dict[str, Any]) -> None:
        self._seen[fingerprint] = time.time()

    # ── public surface ──────────────────────────────────────────────────

    def brief(self, text: str) -> dict[str, Any]:
        """Read a loop budget out of the user's words (flag-independent)."""

        parsed = parse_loop_brief(text)
        if not loop_mode_enabled():
            return {**parsed, "mode": MODE_GOAL, "reason": f"{LOOP_MODE_FLAG} is disabled"}
        return parsed

    def decide(
        self,
        goal_run_id: str,
        *,
        mode: str = MODE_GOAL,
        round_index: int = 1,
        rounds_done: int = 0,
        elapsed_s: float = 0.0,
        no_progress_rounds: int = 0,
        user_stop: bool = False,
        finish_condition_met: bool = False,
        summary: str = "",
        did: list[str] | None = None,
        open_items: list[str] | None = None,
        tests_run: list[str] | None = None,
        risks: list[str] | None = None,
        next_step: str = "",
    ) -> dict[str, Any]:
        """Ask the policy what the existing run should do next.

        With the flag off this returns ``mode="goal"`` and never posts a card,
        so an un-flagged run is byte-for-byte the pre-existing behaviour.
        """

        if not loop_mode_enabled():
            return {
                "status": "failed",
                "error": {"code": "FEATURE_DISABLED", "flag": LOOP_MODE_FLAG},
                "mode": MODE_GOAL,
                "next": "continue",
                "stop": False,
                "reason": f"{LOOP_MODE_FLAG} is disabled",
                "reason_code": "feature_disabled",
                "card": None,
                "duplicate": False,
                "decision_trail": [],
                "trail_path": "",
            }

        run: dict[str, Any] = {}
        if self._read_run is not None:
            try:
                run = dict(self._read_run(goal_run_id) or {})
            except Exception:
                _log.debug("loop policy could not read the run", exc_info=True)
                run = {}

        config = LoopRunConfig(
            seen_before=self._seen_before,
            mark_seen=self._mark_seen,
            trail_sink=self._trail_sink,
        )
        result = loop_run(
            config,
            LoopRunInput(
                goal_run_id=goal_run_id,
                mode=mode,
                round_index=round_index,
                rounds_done=int(run.get("rounds_done", rounds_done) or rounds_done),
                elapsed_s=float(run.get("elapsed_s", elapsed_s) or elapsed_s),
                no_progress_rounds=no_progress_rounds,
                user_stop=user_stop,
                finish_condition_met=finish_condition_met,
                summary=summary,
                did=list(did or []),
                open_items=list(open_items or []),
                tests_run=list(tests_run or []),
                risks=list(risks or []),
                next_step=next_step,
            ),
            None,
        )

        card = result.get("card")
        if card and self._post_card is not None:
            try:
                self._post_card(goal_run_id, card)
            except Exception as exc:
                _log.warning("loop checkpoint card not posted: %s", exc)
                result["reason"] += f" (card not posted: {type(exc).__name__})"
        return result


def finish_card(
    goal_run_id: str,
    *,
    summary: str = "",
    did: list[str] | None = None,
    open_items: list[str] | None = None,
    tests_run: list[str] | None = None,
    risks: list[str] | None = None,
) -> dict[str, Any]:
    """The W5 "what changed / tests run / open risks" card for any run.

    A finish checkpoint is not a loop feature: it applies to every GoalRun that
    is about to mark itself done, so this helper is importable on its own.
    """

    from veya.oskill.pure.loop_policy import checkpoint_card

    return checkpoint_card(
        mode=MODE_GOAL,
        round_index=1,
        summary=summary,
        did=did,
        open_items=open_items,
        tests_run=tests_run,
        risks=risks,
    )


__all__ = ["LOOP_MODE_FLAG", "LoopRunPolicy", "finish_card", "loop_mode_enabled"]