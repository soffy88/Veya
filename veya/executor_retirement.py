"""Retired executor policy — neutral home for the deny marker.

This module deliberately sits above ``veya/remote`` so that "this executor was
removed on purpose" is stated in one place that is not part of the remote
execution plane. It carries no runtime, no adapter, and no routing.

Two states are kept strictly distinct:

``RETIRED``
    A known executor that was deliberately removed. A request for it must fail
    closed with an explicit retired error. It must never be reported as merely
    absent, because "unavailable" implies "install it and it will work".

``unknown``
    Never admitted here or in :mod:`veya.remote.executor_registry`. Rejected as
    unknown.

Adding a name here changes the contract for that executor across the whole
system, so it is a one-line, reviewable decision.
"""

from __future__ import annotations

# Retired executors fail closed at admission, discovery, and probing.
RETIRED_EXECUTORS: frozenset[str] = frozenset({"hicode"})


def is_retired_executor(executor_id: str) -> bool:
    """Return whether ``executor_id`` was deliberately removed.

    Callers that need to distinguish "retired" from "unknown" must use this
    rather than a lookup that raises, because a retired executor is a
    deliberate removal and not a missing registration.
    """

    return executor_id in RETIRED_EXECUTORS


__all__ = ["RETIRED_EXECUTORS", "is_retired_executor"]
