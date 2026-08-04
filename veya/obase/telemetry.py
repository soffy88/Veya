"""obase.telemetry — JSONL tracing/telemetry (3O §7, an obase cross-cutting concern).

Key point (§5.6 C1 iron rule — asyncio ContextVar with shared mutable objects):
- Every Task holds its own context copy (PEP 567); a ``.set()`` inside a child task
  never propagates back to the parent context.
- Hence ``TraceContext`` is a **shared mutable object**: top-level ``begin_trace``
  binds the reference once; all downstream code (including concurrent child tasks)
  only ``.get()`` the same object and accumulate onto it (``add_step`` rather than
  creating new objects).

Integration with the on_step channel (dependency direction: service layer → obase):
- This module never imports ``server.events`` (§7.4 forbids reverse dependencies);
- The service layer injects the callback via ``set_emitter(fire_step_wrapper)``;
  emit() writes the trace and forwards the event.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import inspect
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "TraceContext",
    "begin_trace",
    "emit",
    "end_trace",
    "jsonl_write",
    "latest_trace",
    "set_emitter",
    "traced",
]

# ── ContextVar 通道 ───────────────────────────────────────────────────
_current: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "obase_trace", default=None
)
_emitter_ctx: contextvars.ContextVar[Callable[[dict], None] | None] = contextvars.ContextVar(
    "obase_emitter", default=None
)


@dataclasses.dataclass
class TraceContext:
    """Shared mutable trace object (ContextVar holds a reference; concurrent child tasks accumulate into the same object)."""

    name: str
    trace_id: str
    parent_id: str | None = None
    started_at: float = dataclasses.field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "running"  # running | completed | failed | cancelled
    steps: list[dict] = dataclasses.field(default_factory=list)
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)
    _token: contextvars.Token | None = dataclasses.field(default=None, init=False, repr=False)

    def add_step(self, event: dict[str, Any]) -> None:
        """Append an event (list.append, not set — same as the C1 iron-rule decision_trail)."""
        step = dict(event)
        step.setdefault("ts", time.time())
        step.setdefault("trace_id", self.trace_id)
        self.steps.append(step)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": (
                round((self.finished_at - self.started_at) * 1000, 3)
                if self.finished_at is not None
                else None
            ),
            "steps": self.steps,
            "meta": self.meta,
        }

    # ── ContextVar 访问 ──────────────────────────────────────────────
    def __enter__(self) -> TraceContext:
        self._token = _current.set(self)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._token is not None:
            _current.reset(self._token)
            self._token = None

    def close(self, *, status: str = "completed") -> None:
        self.status = status
        self.finished_at = time.time()


def current_trace() -> TraceContext | None:
    """The trace bound to the current context (None if absent; get-only, never set)."""
    return _current.get()


# ── 生命周期 ──────────────────────────────────────────────────────────
def begin_trace(
    name: str,
    *,
    trace_id: str | None = None,
    parent_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> TraceContext:
    """Begin a top-level trace (binds the reference in the context; must ``end_trace``/``close`` when done)."""
    parent = current_trace()
    trace = TraceContext(
        name=name,
        trace_id=trace_id or uuid.uuid4().hex[:12],
        parent_id=parent_id or (parent.trace_id if parent else None),
        meta=dict(meta or {}),
    )
    return trace


def end_trace(trace: TraceContext, *, status: str = "completed") -> TraceContext:
    """Close the trace and emit a terminal event (emit before mutating status — same as StreamingManager)."""
    emit(
        {
            "span": trace.name,
            "event": "end",
            "status": status,
            "duration_ms": round((time.time() - trace.started_at) * 1000, 3),
        }
    )
    trace.close(status=status)
    return trace


def emit(event: dict[str, Any]) -> None:
    """Write the current trace steps and forward them to the injected emitter (never raises, matching on_step semantics)."""
    trace = _current.get()
    if trace is not None:
        trace.add_step(event)
    cb = _emitter_ctx.get()
    if cb is not None:
        with contextlib.suppress(Exception):
            cb(event)


def set_emitter(cb: Callable[[dict], None] | None) -> contextvars.Token:
    """Service layer injects an on_step callback (e.g. ``server.events.fire_step``). Returns a reset token."""
    return _emitter_ctx.set(cb)


# ── @traced 装饰器 ────────────────────────────────────────────────────
def traced(name: str | None = None) -> Callable:
    """Sync/async universal span decorator: records enter/exit/error + duration automatically.

    Execution model follows the callable kind (§0.2): async def → awaited wrapper;
    sync def → synchronous wrapper. Exceptions are never swallowed (status=failed is
    recorded, then re-raised); CancelledError is recorded as cancelled and re-raised.
    """

    def decorator(func: Callable) -> Callable:
        span = name or func.__qualname__
        if inspect.iscoroutinefunction(func):

            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                emit({"span": span, "event": "enter", "args": _args_summary(args, kwargs)})
                start = time.time()
                try:
                    result = await func(*args, **kwargs)
                except asyncio.CancelledError:
                    emit({"span": span, "event": "error", "status": "cancelled"})
                    raise
                except Exception as exc:
                    emit(
                        {
                            "span": span,
                            "event": "error",
                            "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                            "duration_ms": _elapsed_ms(start),
                        }
                    )
                    raise
                emit(
                    {
                        "span": span,
                        "event": "exit",
                        "status": "completed",
                        "duration_ms": _elapsed_ms(start),
                    }
                )
                return result

            return async_wrapper

        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            emit({"span": span, "event": "enter", "args": _args_summary(args, kwargs)})
            start = time.time()
            try:
                result = func(*args, **kwargs)
            except asyncio.CancelledError:
                emit({"span": span, "event": "error", "status": "cancelled"})
                raise
            except Exception as exc:
                emit(
                    {
                        "span": span,
                        "event": "error",
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "duration_ms": _elapsed_ms(start),
                    }
                )
                raise
            emit(
                {
                    "span": span,
                    "event": "exit",
                    "status": "completed",
                    "duration_ms": _elapsed_ms(start),
                }
            )
            return result

        return sync_wrapper

    return decorator


def _args_summary(args: tuple, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Argument summary (prevents PII/large objects from leaking into traces, §5.5.1 spirit)."""
    summary: dict[str, Any] = {}
    for i, a in enumerate(args[:2]):  # 最多前 2 个位置参数
        summary[f"arg{i}"] = _safe_repr(a)
    for k, v in list(kwargs.items())[:4]:  # 最多前 4 个 kw
        summary[k] = _safe_repr(v)
    return summary


def _safe_repr(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return value[:80] + ("…" if len(value) > 80 else "")
    try:
        text = repr(value)
        return text[:80] + ("…" if len(text) > 80 else "")
    except Exception:
        return "<unrepresentable>"


def _elapsed_ms(start: float) -> float:
    return round((time.time() - start) * 1000, 3)


# ── JSONL 汇出 ────────────────────────────────────────────────────────
def jsonl_write(trace: TraceContext, *, path: Path) -> Path:
    """Append one JSON line (reproducible event order). Single source: reads reuse compat.jsonl_latest."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(trace.as_dict(), ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path


def latest_trace(*, path: Path) -> dict | None:
    """Read the latest trace (delegates to compat.jsonl_latest — §1.4 single source, no reimplementation)."""
    from veya.compat import jsonl_latest  # 委托，非复制

    return jsonl_latest(path=Path(path), by_key="trace_id")
