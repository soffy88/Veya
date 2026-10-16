"""veya/omodul/loop_run — Goal vs Loop as a GoalRun policy (W3).

One transaction: given a GoalRun's current counters, decide whether it keeps
going, posts a checkpoint card, or stops — and hand back the card to post.

**It is not a scheduler and it creates no run.** ``GoalRunState`` remains the
authority: the caller passes the state's ``budget``/status projection in and
applies the returned ``next`` to the *existing* runner. ``Goal`` mode is a
no-op path that reproduces today's behaviour exactly, so turning the flag on
cannot change a Goal run.

Composition (§3): one oskill (:mod:`veya.oskill.pure.loop_policy`) over the
caller's already-decided progress signal — the existing ``NoProgressGuard`` /
``ProgressTracker`` own *what counts as progress*, this module owns only the
loop budget rules.

Pillars:
    decision_trail — why a round continued, checkpointed or stopped
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from veya.oskill.pure.loop_policy import (
    DEFAULT_TIME_BUDGET_S,
    DEFAULT_TURN_BUDGET,
    MODE_GOAL,
    MODE_LOOP,
    STOP_CONTINUE,
    checkpoint_card,
    loop_decision,
    parse_loop_brief,
)

_log = logging.getLogger(__name__)

__all__ = [
    "LoopRunConfig",
    "LoopRunInput",
    "compute_fingerprint_for",
    "loop_run",
]

#: The fingerprint covers the run identity, so the same round of the same run
#: cannot post two cards.
_FINGERPRINT_FIELDS = ("goal_run_id", "round_index")


@dataclass
class LoopRunConfig:
    """Injected collaborators. No state lives in this module."""

    #: (fingerprint) -> bool — True when this round already posted its card.
    seen_before: Callable[[str], bool] | None = None
    #: (fingerprint, card) -> None — records that the card was posted.
    mark_seen: Callable[[str, dict[str, Any]], None] | None = None
    #: Optional trail sink (layer4 ``append_canonical_event``).
    trail_sink: Callable[[dict[str, Any]], Any] | None = None
    #: Turn budget for loop mode when the input does not carry one.
    default_turn_budget: int = DEFAULT_TURN_BUDGET
    #: Wall budget for loop mode when the input does not carry one.
    default_time_budget_s: float = DEFAULT_TIME_BUDGET_S

    _omodul_name: ClassVar[str] = "loop_run"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = set(_FINGERPRINT_FIELDS)
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail"}


@dataclass
class LoopRunInput:
    """One decision point of an existing GoalRun."""

    goal_run_id: str
    mode: str = MODE_GOAL
    round_index: int = 1
    rounds_done: int = 0
    elapsed_s: float = 0.0
    #: The caller's existing progress detector has already decided this.
    no_progress_rounds: int = 0
    user_stop: bool = False
    finish_condition_met: bool = False
    #: Optional card content; when empty the card renders from the summary only.
    summary: str = ""
    did: list[str] = field(default_factory=list)
    open_items: list[str] = field(default_factory=list)
    tests_run: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    next_step: str = ""


def compute_fingerprint_for(config: Any, input_data: Any) -> str:
    """Stable dedup digest for one round; ``""`` when unreadable."""

    fields = set(getattr(config, "_fingerprint_fields", set()) or set())
    if not fields or input_data is None:
        return ""
    if hasattr(input_data, "model_dump"):
        data = input_data.model_dump()
    elif hasattr(input_data, "__dict__"):
        data = dict(input_data.__dict__)
    elif isinstance(input_data, dict):
        data = dict(input_data)
    else:
        return ""
    # Reuse the canonical digest atom rather than re-deriving one here.
    from veya.oprim.turn_envelope import envelope_fingerprint

    return envelope_fingerprint(
        {key: data.get(key) for key in sorted(fields)}, tuple(sorted(fields))
    )


def _call(fn: Callable[..., Any] | None, default: Any, *args: Any) -> Any:
    if fn is None:
        return default
    try:
        return fn(*args)
    except Exception:
        _log.debug("loop_run collaborator failed", exc_info=True)
        return default


def _record(config: LoopRunConfig, trail: list[dict[str, Any]], event: str, **fields: Any) -> None:
    entry = {"event": event, **fields}
    trail.append(entry)
    sink = config.trail_sink
    if sink is None:
        return
    with contextlib.suppress(Exception):
        result = sink(entry)
        if asyncio.iscoroutine(result):
            result.close()


def loop_run(
    config: LoopRunConfig,
    input_data: LoopRunInput,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Decide continue / checkpoint / stop for one round of an existing run.

    Never raises (§5.6); a malformed input returns ``status="failed"``.

    Args:
        config: injected dedupe handles + trail sink.
        input_data: the run's current counters.
        output_dir: optional directory for the trail artifact.

    Returns:
        ``{status, error, mode, next, stop, reason, reason_code, card,
        duplicate, decision_trail, trail_path}``. ``next`` is one of
        ``continue`` / ``checkpoint`` / ``stop``.
    """

    trail: list[dict[str, Any]] = []
    fingerprint = compute_fingerprint_for(config, input_data)

    if not str(getattr(input_data, "goal_run_id", "") or "").strip():
        _record(
            config, trail, "loop.failed", code="INVALID_INPUT", reason="goal_run_id is required"
        )
        return {
            "status": "failed",
            "error": {"code": "INVALID_INPUT", "reason": "goal_run_id is required"},
            "mode": MODE_GOAL,
            "next": "stop",
            "stop": True,
            "reason": "goal_run_id is required",
            "reason_code": "invalid_input",
            "card": None,
            "duplicate": False,
            "decision_trail": trail,
            "trail_path": "",
        }

    mode = str(getattr(input_data, "mode", MODE_GOAL) or MODE_GOAL).strip().lower()
    decision = loop_decision(
        mode=mode,
        rounds_done=int(getattr(input_data, "rounds_done", 0) or 0),
        elapsed_s=float(getattr(input_data, "elapsed_s", 0.0) or 0.0),
        turn_budget=int(config.default_turn_budget),
        time_budget_s=float(config.default_time_budget_s),
        no_progress_rounds=int(getattr(input_data, "no_progress_rounds", 0) or 0),
        user_stop=bool(getattr(input_data, "user_stop", False)),
        finish_condition_met=bool(getattr(input_data, "finish_condition_met", False)),
    )

    # Goal mode never posts a card and never stops early: the run behaves
    # exactly as it did before this module existed.
    card: dict[str, Any] | None = None
    duplicate = False
    if decision["next"] == "checkpoint" and decision["mode"] == MODE_LOOP:
        if _call(config.seen_before, False, fingerprint):
            duplicate = True
            decision = {**decision, "next": "continue", "reason": "this round already checkpointed"}
        else:
            card = checkpoint_card(
                mode=decision["mode"],
                round_index=int(getattr(input_data, "round_index", 1) or 1),
                summary=str(getattr(input_data, "summary", "") or ""),
                did=list(getattr(input_data, "did", []) or []),
                open_items=list(getattr(input_data, "open_items", []) or []),
                tests_run=list(getattr(input_data, "tests_run", []) or []),
                risks=list(getattr(input_data, "risks", []) or []),
                next_step=str(getattr(input_data, "next_step", "") or ""),
            )
            _call(config.mark_seen, None, fingerprint, card)
    elif decision["stop"] and decision["mode"] == MODE_LOOP:
        # The finish card (spec W5) rides the stop edge, not a separate path.
        card = checkpoint_card(
            mode=MODE_LOOP,
            round_index=int(getattr(input_data, "round_index", 1) or 1),
            summary=str(getattr(input_data, "summary", "") or ""),
            did=list(getattr(input_data, "did", []) or []),
            open_items=list(getattr(input_data, "open_items", []) or []),
            tests_run=list(getattr(input_data, "tests_run", []) or []),
            risks=list(getattr(input_data, "risks", []) or []),
        )

    _record(
        config,
        trail,
        "loop.decided",
        mode=decision["mode"],
        next=decision["next"],
        reason_code=decision["reason_code"],
        reason=decision["reason"],
        duplicate=duplicate,
    )

    return {
        "status": "completed",
        "error": None,
        "mode": decision["mode"],
        "next": decision["next"],
        "stop": decision["stop"],
        "reason": decision["reason"],
        "reason_code": decision["reason_code"],
        "card": card,
        "duplicate": duplicate,
        "fingerprint": fingerprint,
        "decision_trail": trail,
        "trail_path": _write_trail(output_dir, trail),
    }


def _write_trail(output_dir: Path | None, trail: list[dict[str, Any]]) -> str:
    if output_dir is None:
        return ""
    try:
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        target = path / "loop_run_trail.jsonl"
        with target.open("a", encoding="utf-8") as handle:
            for entry in trail:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return str(target)
    except Exception:
        _log.debug("loop_run trail write failed", exc_info=True)
        return ""


# Re-exported so the layer-4 call site imports the brief parser and the stop
# vocabulary from the same place it imports the policy decision.
__all__ += ["STOP_CONTINUE", "parse_loop_brief"]
