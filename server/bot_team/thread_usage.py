"""Per-thread turn usage ledger (OpenMausBot group-thread-usage method, adapted).

Records only what veya can actually observe: turns per thread, the last
speaker and status, and the tool calls a turn made (count, result
characters, wall time). Token counts and cost are NOT recorded: the
harness has no provider metering point — ``GoalRunResponse`` carries no
usage fields, and SpawnGuard only holds budget *estimates*. Each row
therefore carries ``input_tokens``/``output_tokens``/``cached_tokens``/
``cost_usd`` as explicit nulls, and :func:`record_turn` accepts an
optional ``metering`` mapping so a future metering point can feed real
numbers without changing the shape. Nothing here is estimated or
invented.
"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field


@dataclass
class TurnUsage:
    """What one turn observably did, accumulated at the tool funnel."""

    tool_calls: int = 0
    tool_result_chars: int = 0
    tool_duration_ms: float = 0.0

    def note_tool(self, result: object, duration_ms: float) -> None:
        """Count one executed tool call and its result size."""

        self.tool_calls += 1
        try:
            self.tool_result_chars += len(result)  # type: ignore[arg-type]
        except TypeError:
            pass
        self.tool_duration_ms += max(0.0, float(duration_ms))


_usage_ctx: contextvars.ContextVar[TurnUsage | None] = contextvars.ContextVar(
    "veya_turn_usage", default=None
)


def bind_turn_usage() -> tuple[TurnUsage, contextvars.Token]:
    """Bind a fresh accumulator for one turn; reset it with the token."""

    usage = TurnUsage()
    return usage, _usage_ctx.set(usage)


def reset_turn_usage(token: contextvars.Token) -> None:
    """Restore the accumulator that was active before :func:`bind_turn_usage`."""

    _usage_ctx.reset(token)


def current_turn_usage() -> TurnUsage | None:
    """The accumulator bound for the running turn, or ``None`` outside one."""

    return _usage_ctx.get()


def note_tool_call(result: object, duration_ms: float) -> None:
    """Feed one tool execution into the running turn's accumulator, if any."""

    usage = _usage_ctx.get()
    if usage is not None:
        usage.note_tool(result, duration_ms)


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class _ThreadRow:
    turns: int = 0
    last_turn_at: str = ""
    last_speaker: str = ""
    last_status: str = ""
    tool_calls: int = 0
    tool_result_chars: int = 0
    tool_duration_ms: float = 0.0
    metering: dict[str, object] = field(
        default_factory=lambda: {
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "cost_usd": None,
        }
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "turns": self.turns,
            "last_turn_at": self.last_turn_at,
            "last_speaker": self.last_speaker,
            "last_status": self.last_status,
            "tool_calls": self.tool_calls,
            "tool_result_chars": self.tool_result_chars,
            "tool_duration_ms": round(self.tool_duration_ms, 3),
            **self.metering,
        }


_LEDGER: dict[str, _ThreadRow] = {}


def record_turn(
    thread_id: str,
    *,
    bot_id: str,
    status: str,
    usage: TurnUsage | None = None,
    metering: dict[str, object] | None = None,
) -> dict[str, object]:
    """Fold one finished turn into its thread's row; return the row."""

    row = _LEDGER.setdefault(str(thread_id), _ThreadRow())
    row.turns += 1
    row.last_turn_at = _now_utc()
    row.last_speaker = bot_id
    row.last_status = status
    if usage is not None:
        row.tool_calls += usage.tool_calls
        row.tool_result_chars += usage.tool_result_chars
        row.tool_duration_ms += usage.tool_duration_ms
    if metering:
        for key in ("input_tokens", "output_tokens", "cached_tokens", "cost_usd"):
            if key in metering:
                row.metering[key] = metering[key]
    return row.to_dict()


def thread_usage_snapshot() -> dict[str, dict[str, object]]:
    """All thread rows, keyed by thread id, in thread-id order."""

    return {thread_id: _LEDGER[thread_id].to_dict() for thread_id in sorted(_LEDGER)}


def reset_thread_usage() -> None:
    """Drop every row (tests only; the ledger is otherwise append-only)."""

    _LEDGER.clear()


__all__ = [
    "TurnUsage",
    "bind_turn_usage",
    "current_turn_usage",
    "note_tool_call",
    "record_turn",
    "reset_thread_usage",
    "reset_turn_usage",
    "thread_usage_snapshot",
]
