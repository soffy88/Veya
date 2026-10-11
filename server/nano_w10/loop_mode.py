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

import contextlib
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
        turn_budget: int | None = None,
        time_budget_s: float | None = None,
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

        # The budget the user's own brief stated wins over the oskill default:
        # "loop 10 rounds" must actually run ten rounds, not six.
        config = LoopRunConfig(
            seen_before=self._seen_before,
            mark_seen=self._mark_seen,
            trail_sink=self._trail_sink,
        )
        if turn_budget is not None:
            config.default_turn_budget = max(1, int(turn_budget))
        if time_budget_s is not None:
            config.default_time_budget_s = float(time_budget_s)
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


# ── W5: the finish checkpoint, posted before a run marks itself done ──────

FINISH_CHECKPOINT_FLAG = "VEYA_FINISH_CHECKPOINT"

#: Verdicts the user may give a card. ``send_back`` is a correction, so it
#: lands through the item-1 turn queue — there is no second way to talk to a
#: run.
CHECKPOINT_VERDICTS: tuple[str, ...] = ("accept", "send_back")

CARD_KIND = "finish_checkpoint"


def finish_checkpoint_enabled() -> bool:
    """Is the W5 finish checkpoint on? (spec W5; HARD RULES: default OFF)."""

    return flag_enabled(FINISH_CHECKPOINT_FLAG)


def card_key(
    bot_id: str,
    goal_run_id: str,
    *,
    mode: str = MODE_GOAL,
    round_index: int = 0,
) -> str:
    """Stable id of one card inside one bot's chat.

    A loop posts one card **per round**, so the round is part of the key; a
    finish card is one per run, so it is not. Without this a round-1 card would
    make the run's finish card look like a duplicate.
    """

    base = f"fc-{bot_id}-{goal_run_id}"
    if str(mode).strip().lower() == MODE_LOOP and int(round_index) > 0:
        return f"{base}-r{int(round_index)}"
    return base


class FinishCheckpointGate:
    """Posts the W5 card and records the user's answer to it.

    The gate owns **no store and no approval**:

    * the card is posted into the **existing** bot conversation (so the user
      reads it where the run happened) and mirrored onto the **existing**
      stream/event path;
    * the answer is appended with :func:`server.events.append_canonical_event`
      — the existing event store, never a new table;
    * ``send_back`` lands the user's note through the **item-1** turn queue, so
      a correction and a normal message share one path.

    Parameters
    ----------
    post_to_chat:
        ``(bot_id, text, meta) -> Any`` — the existing ``ConversationStore``.
    record_event:
        ``(topic, payload) -> Any`` — the existing canonical event store.
    deliver_note:
        ``(bot_id, text) -> dict`` — the item-1 ``TurnQueue.steer``/intake.
    """

    def __init__(
        self,
        *,
        post_to_chat: Callable[[str, str, dict[str, Any]], Any] | None = None,
        record_event: Callable[[str, dict[str, Any]], Any] | None = None,
        deliver_note: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self._post_to_chat = post_to_chat
        self._record_event = record_event
        self._deliver_note = deliver_note
        self._posted: dict[str, str] = {}

    # ── public surface ──────────────────────────────────────────────────

    def posted(self, bot_id: str) -> list[str]:
        """Card ids this process already posted for ``bot_id``."""

        prefix = f"fc-{bot_id}-"
        return [key for key in self._posted if key.startswith(prefix)]

    def post(
        self,
        goal_run_id: str,
        *,
        bot_id: str = "",
        summary: str = "",
        did: list[str] | None = None,
        open_items: list[str] | None = None,
        tests_run: list[str] | None = None,
        risks: list[str] | None = None,
        mode: str = MODE_GOAL,
        round_index: int = 1,
    ) -> dict[str, Any]:
        """Build and post one card. Returns the standard status envelope.

        Posting the same run twice in one process is refused rather than
        duplicated — a resumed run that re-finalizes must not double-post.
        """

        if not finish_checkpoint_enabled():
            return _disabled("post")
        run_id = str(goal_run_id or "").strip()
        chat = str(bot_id or "").strip()
        if not run_id or not chat:
            return _invalid("goal_run_id and bot_id are required")

        from veya.oskill.pure.loop_policy import checkpoint_card

        card = checkpoint_card(
            mode=mode,
            round_index=round_index,
            summary=summary,
            did=did,
            open_items=open_items,
            tests_run=tests_run,
            risks=risks,
        )
        return self.publish(bot_id, goal_run_id, card, flag=FINISH_CHECKPOINT_FLAG)

    def publish(
        self,
        bot_id: str,
        goal_run_id: str,
        card: dict[str, Any],
        *,
        flag: str = FINISH_CHECKPOINT_FLAG,
    ) -> dict[str, Any]:
        """Post a card that was already rendered (e.g. a loop round's card).

        ``flag`` is the gate that governs this card: a loop round's checkpoint
        is loop behaviour (``VEYA_LOOP_MODE``), the finish card is W5 behaviour
        (``VEYA_FINISH_CHECKPOINT``). Posting one never requires the other.
        """

        if not flag_enabled(flag):
            return _disabled("publish", flag)
        run_id = str(goal_run_id or "").strip()
        chat = str(bot_id or "").strip()
        if not run_id or not chat or not isinstance(card, dict):
            return _invalid("bot_id, goal_run_id and a rendered card are required")

        key = card_key(
            chat,
            run_id,
            mode=str(card.get("mode") or MODE_GOAL),
            round_index=int(card.get("round") or 0),
        )
        if key in self._posted:
            return {
                "status": "completed",
                "error": None,
                "card": card,
                "card_id": key,
                "posted": False,
                "duplicate": True,
            }

        meta = {
            "kind": CARD_KIND,
            "card_id": key,
            "goal_run_id": run_id,
            "mode": str(card.get("mode") or MODE_GOAL),
            "round": int(card.get("round") or 1),
        }
        self._posted[key] = str(card.get("text") or "")

        if self._post_to_chat is not None:
            try:
                self._post_to_chat(chat, str(card.get("text") or ""), meta)
            except Exception as exc:
                _log.warning("finish checkpoint card not posted to the chat: %s", exc)
                return {
                    "status": "failed",
                    "error": {"code": "CHAT_UNAVAILABLE", "reason": type(exc).__name__},
                    "card": card,
                    "card_id": key,
                    "posted": False,
                    "duplicate": False,
                }
        if self._record_event is not None:
            with contextlib.suppress(Exception):
                self._record_event("goal_run.finish_checkpoint", {"card_id": key, **card})
        return {
            "status": "completed",
            "error": None,
            "card": card,
            "card_id": key,
            "posted": True,
            "duplicate": False,
        }

    def respond(
        self,
        bot_id: str,
        card_id: str,
        verdict: str,
        *,
        note: str = "",
    ) -> dict[str, Any]:
        """Record ``accept`` / ``send_back`` for a posted card.

        ``send_back`` is a correction: the note goes to the item-1 turn queue
        so it lands the same way any other message from this user would.
        """

        if not finish_checkpoint_enabled():
            return _disabled("respond")
        chat = str(bot_id or "").strip()
        answer = str(verdict or "").strip().lower()
        if not chat or not str(card_id or "").strip() or answer not in CHECKPOINT_VERDICTS:
            return _invalid(
                f"verdict must be one of {', '.join(CHECKPOINT_VERDICTS)}", verdict=answer
            )

        payload: dict[str, Any] = {
            "card_id": str(card_id),
            "bot_id": chat,
            "verdict": answer,
            "note": str(note or "").strip(),
        }
        if self._record_event is not None:
            with contextlib.suppress(Exception):
                self._record_event("goal_run.finish_checkpoint.response", payload)

        steered = False
        if answer == "send_back":
            if not payload["note"]:
                return _invalid("sending a card back needs a note", verdict=answer)
            if self._deliver_note is None:
                return {
                    "status": "completed",
                    "error": None,
                    "verdict": answer,
                    "card_id": str(card_id),
                    "steered": False,
                    "reason": "no turn queue bound; the note is recorded as an event only",
                }
            landed = self._deliver_note(chat, str(payload["note"]))
            steered = bool(isinstance(landed, dict) and landed.get("action") in ("steer", "queue"))
            payload["landed"] = (landed or {}).get("action") if isinstance(landed, dict) else None
        return {
            "status": "completed",
            "error": None,
            "verdict": answer,
            "card_id": str(card_id),
            "steered": steered,
            "landed": payload.get("landed"),
        }


def _disabled(op: str, flag: str = FINISH_CHECKPOINT_FLAG) -> dict[str, Any]:
    """The one shape an off flag ever returns."""

    return {
        "status": "failed",
        "error": {"code": "FEATURE_DISABLED", "flag": flag},
        "op": op,
        "card": None,
        "card_id": "",
        "verdict": "",
        "posted": False,
        "steered": False,
        "duplicate": False,
    }


def _invalid(reason: str, *, verdict: str = "") -> dict[str, Any]:
    """Malformed input never raises, and never looks like a feature failure."""

    return {
        "status": "failed",
        "error": {"code": "INVALID_INPUT", "reason": reason},
        "card": None,
        "card_id": "",
        "verdict": verdict,
        "posted": False,
        "steered": False,
        "duplicate": False,
    }


def _default_gate() -> FinishCheckpointGate:
    """Bind the gate to the project's existing conversation/event/turn paths."""

    def _post_to_chat(bot_id: str, text: str, meta: dict[str, Any]) -> Any:
        from server.bot_team.conversations import ConversationStore

        return ConversationStore().append(
            bot_id,
            role="bot",
            text=text,
            meta=meta,
        )

    def _record_event(topic: str, payload: dict[str, Any]) -> Any:
        from server.events import append_canonical_event

        return append_canonical_event(topic, payload, actor="bot")

    def _deliver_note(bot_id: str, text: str) -> dict[str, Any]:
        from server.nano_w10.turn_queue import get_turn_queue

        return get_turn_queue().intake(
            conversation_ref=bot_id,
            text=text,
            kind="direct",
            bot_ref=bot_id,
            user_directed=True,
        )

    return FinishCheckpointGate(
        post_to_chat=_post_to_chat,
        record_event=_record_event,
        deliver_note=_deliver_note,
    )


_GATE: FinishCheckpointGate | None = None


def get_finish_checkpoint_gate() -> FinishCheckpointGate:
    """The process-wide :class:`FinishCheckpointGate`."""

    global _GATE
    if _GATE is None:
        _GATE = _default_gate()
    return _GATE


def get_loop_policy(post_card: Callable[[str, dict[str, Any]], Any] | None = None) -> LoopRunPolicy:
    """Build the :class:`LoopRunPolicy` for one run.

    ``post_card`` is supplied by the caller because the card has to land in
    *that run's* bot chat; there is no ambient bot to guess.
    """

    return LoopRunPolicy(post_card=post_card)


def reset_finish_checkpoint() -> None:
    """Drop the process gate singleton (tests / operator reset)."""

    global _GATE
    _GATE = None


__all__ = [
    "CARD_KIND",
    "CHECKPOINT_VERDICTS",
    "FINISH_CHECKPOINT_FLAG",
    "LOOP_MODE_FLAG",
    "FinishCheckpointGate",
    "LoopRunPolicy",
    "card_key",
    "finish_card",
    "finish_checkpoint_enabled",
    "get_finish_checkpoint_gate",
    "get_loop_policy",
    "loop_mode_enabled",
    "reset_finish_checkpoint",
]
