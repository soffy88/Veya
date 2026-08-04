"""obase.telemetry — JSONL 追踪/遥测（3O §7 obase 横切关注点）。

要点（§5.6 C1 铁律 — asyncio ContextVar 共享可变对象）：
- 每个 Task 持有独立 context 副本（PEP 567），子 Task 内 ``.set()`` 的新值
  不回传父 context。
- 因此 ``TraceContext`` 是**共享可变对象**：顶层 ``begin_trace`` 只 set 一次引用，
  所有下游（含并发子 Task）只 ``.get()`` 拿到同一对象并在对象上累加
  （``add_step`` 而非新建对象）。

与 on_step 通道集成（依赖方向：服务层 → obase）：
- 本模块不 import ``server.events``（§7.4 禁止反向依赖）；
- 服务层用 ``set_emitter(fire_step_wrapper)`` 把回调注入，emit 时写 trace 并转发。
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
    """共享可变 trace 对象（ContextVar 持有引用；并发子 Task 累加同一对象）。"""

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
        """追加事件（list.append 而非 set —— C1 铁律的 decision_trail 同款）。"""
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
    """当前 context 的 trace（无则 None；只 get 不 set）。"""
    return _current.get()


# ── 生命周期 ──────────────────────────────────────────────────────────
def begin_trace(
    name: str,
    *,
    trace_id: str | None = None,
    parent_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> TraceContext:
    """顶层开启 trace（在 context 中 set 引用；结束后须 ``end_trace``/``close``）。"""
    parent = current_trace()
    trace = TraceContext(
        name=name,
        trace_id=trace_id or uuid.uuid4().hex[:12],
        parent_id=parent_id or (parent.trace_id if parent else None),
        meta=dict(meta or {}),
    )
    return trace


def end_trace(trace: TraceContext, *, status: str = "completed") -> TraceContext:
    """关闭 trace 并 emit 终结事件（须先 emit 再改 status —— StreamingManager 同款）。"""
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
    """写当前 trace 的 steps 并转发给注入的 emitter（不 raise，与 on_step 语义一致）。"""
    trace = _current.get()
    if trace is not None:
        trace.add_step(event)
    cb = _emitter_ctx.get()
    if cb is not None:
        with contextlib.suppress(Exception):
            cb(event)


def set_emitter(cb: Callable[[dict], None] | None) -> contextvars.Token:
    """服务层注入 on_step 回调（如 ``server.events.fire_step``）。返回 reset token。"""
    return _emitter_ctx.set(cb)


# ── @traced 装饰器 ────────────────────────────────────────────────────
def traced(name: str | None = None) -> Callable:
    """Sync/async 通用 span 装饰器：自动记 enter/exit/error + duration。

    执行模型由本性决定（§0.2）：async def → await 包装；sync def → 同步包装。
    异常不吞（记录 status=failed 后重新 raise）；CancelledError 记 cancelled 后重抛。
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
    """参数摘要（防 PII/大对象泄漏进 trace，§5.5.1 精神）。"""
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
    """追加一行 JSON（事件顺序可复现）。单源：读取复用 compat.jsonl_latest。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(trace.as_dict(), ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path


def latest_trace(*, path: Path) -> dict | None:
    """读取最新一条 trace（委托 compat.jsonl_latest —— §1.4 单源，不重复实现）。"""
    from hicode.compat import jsonl_latest  # 委托，非复制

    return jsonl_latest(path=Path(path), by_key="trace_id")
