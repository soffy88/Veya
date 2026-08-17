"""Small asyncio interoperability helpers shared by host execution paths."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Callable
from typing import Any, TypeVar

_T = TypeVar("_T")


async def run_sync_in_daemon_thread(
    func: Callable[..., _T], /, *args: Any, **kwargs: Any
) -> _T:
    """Run blocking work without relying on asyncio's default executor.

    The Python 3.14 runtime used by Veya can stall while shutting down its
    default executor.  A daemon worker plus event-loop polling keeps sync work
    off the loop without making process shutdown wait for executor threads.
    The caller's contextvars are copied into the worker.
    """
    context = contextvars.copy_context()
    done = threading.Event()
    results: list[_T] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(context.run(func, *args, **kwargs))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    threading.Thread(target=run, name="veya-sync-worker", daemon=True).start()
    while not done.is_set():
        await asyncio.sleep(0.001)
    if errors:
        raise errors[0]
    return results[0]


__all__ = ["run_sync_in_daemon_thread"]
