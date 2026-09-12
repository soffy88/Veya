"""Supervisor restart fencing for one PersistentComputer (I3) + I4 resume.

A restarted supervisor must never share a computer with a live owner and
must never create a second active runtime for the same logical computer.
The fencing discipline is implemented on top of the existing
``computer_sessions`` mechanism in :class:`PersistentComputerStore`
(``runtime/computer/store.py``): no new lease table, no second lease
system, no acceptance/finalize semantics.

I4 adds :func:`restart_and_resume_canonical_action`: after the lease moves
from supervisor A to supervisor B, the *same* canonical action resumes
through the recovered GoalRun/computer binding.  Idempotency is owned by
the SideEffectLedger stable operation key, not by this helper.
"""

from __future__ import annotations

import inspect
from typing import Any


class SupervisorRestartRefused(RuntimeError):
    """A restart claim was refused because another supervisor owns the computer."""

    def __init__(self, computer_id: str, owner_supervisor_id: str):
        super().__init__(f"computer {computer_id} is actively owned by {owner_supervisor_id}")
        self.computer_id = computer_id
        self.owner_supervisor_id = owner_supervisor_id


def claim_exclusive_session(
    store: Any,
    *,
    computer_id: str,
    owner_id: str,
    supervisor_id: str,
) -> Any:
    """Claim the single active runtime slot for ``computer_id``.

    Fail-closed: if a *different* supervisor holds the active session, the
    claim is refused instead of creating a duplicate active runtime.  The
    same supervisor reclaiming its own slot is idempotent and returns the
    existing session.
    """
    active = store.get_active_session(computer_id)
    if active is not None and active.state == "active":
        if active.supervisor_id != supervisor_id:
            raise SupervisorRestartRefused(computer_id, active.supervisor_id)
        return active
    return store.create_session(computer_id, owner_id, supervisor_id)


def release_session(store: Any, session_id: str) -> bool:
    """Terminate one claimed runtime slot.  Returns True when it existed."""
    return bool(store.end_session(session_id))


async def restart_and_resume_canonical_action(
    store: Any,
    *,
    computer_id: str,
    owner_id: str,
    supervisor_a: str,
    supervisor_b: str,
    request: Any,
    executor: Any,
) -> Any:
    """Transfer the existing computer lease and resume the same action.

    The lease moves A -> B on the *same* logical computer; the next action
    then executes through the recovered GoalRun/computer binding via
    ``executor``.  No new GoalRun is created and no side effect is replayed
    here — replay protection belongs to the SideEffectLedger operation key.
    """
    active = store.get_active_session(computer_id)
    if active is None or active.supervisor_id != supervisor_a:
        raise SupervisorRestartRefused(computer_id, getattr(active, "supervisor_id", "none"))
    release_session(store, active.session_id)
    claim_exclusive_session(
        store,
        computer_id=computer_id,
        owner_id=owner_id,
        supervisor_id=supervisor_b,
    )
    result = executor(request)
    if inspect.isawaitable(result):
        result = await result
    return result


__all__ = [
    "SupervisorRestartRefused",
    "claim_exclusive_session",
    "release_session",
    "restart_and_resume_canonical_action",
]
