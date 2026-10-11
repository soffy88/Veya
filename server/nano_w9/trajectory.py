"""server/nano_w9/trajectory — preview bundles + trajectory replay.

NANO-W9 addendum E.  Artifact-producing steps (W4 artifacts, W5 design mode) may
emit a **preview bundle** and a trajectory entry; both are appended to the
**existing** ``server/events.py`` topic ``trajectory.recorded`` — no new store.

The assembly / replay / replay-view transforms are pure
(:mod:`veya.oskill.pure.trajectory_assemble`); this module is the thin layer-4
persistor plus the read model a UI/CLI renders.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from veya.oskill.pure.trajectory_assemble import (
    TRAJECTORY_FIELDS,
    assemble_trajectory,
    build_preview_bundle,
    replay_states,
)

__all__ = [
    "TRAJECTORY_FIELDS",
    "TRAJECTORY_TOPIC",
    "Trajectory",
    "assemble_trajectory",
    "build_preview_bundle",
    "read_trajectory",
    "replay_states",
]

#: The existing lifecycle topic trajectory entries are appended under.
TRAJECTORY_TOPIC = "trajectory.recorded"


class Trajectory:
    """Layer-4 persistor + read model for execution trajectories.

    Args:
        append_event: the event writer; defaults to
            ``server.events.append_canonical_event``.  Injected so a test never
            writes to the real log.
    """

    def __init__(self, *, append_event: Callable[..., Any] | None = None) -> None:
        self._append = append_event

    def _writer(self) -> Callable[..., Any]:
        if self._append is not None:
            return self._append
        from server.events import append_canonical_event

        return append_canonical_event

    # -- write ------------------------------------------------------------

    def record(
        self,
        *,
        step: int,
        command: str,
        artifact_ref: str = "",
        preview_ref: str = "",
        ts: float = 0.0,
        goal_id: str = "",
        session_id: str = "",
        kind: str = "file",
        media_type: str = "",
    ) -> dict[str, Any]:
        """Emit a preview bundle + trajectory entry on the existing event log.

        Args:
            step: 1-based step number within the run.
            command: the command / tool call that produced the artifact.
            artifact_ref: store-native artifact reference.
            preview_ref: renderable preview reference.
            ts: unix timestamp (0 ⇒ the event store stamps its own).
            goal_id: owning GoalRun, when there is one.
            session_id: owning session, when there is one.
            kind / media_type: preview renderer hints.

        Returns:
            The emitted bundle (``{"status", "bundle", "trajectory"}``).  A
            failing writer yields ``status="error"`` — never an exception.
        """
        bundle = build_preview_bundle(
            step=step,
            command=command,
            artifact_ref=artifact_ref,
            preview_ref=preview_ref,
            kind=kind,
            media_type=media_type,
            ts=ts,
        )
        entry = bundle["trajectory"]
        payload: dict[str, Any] = {**entry}
        if goal_id:
            payload["goal_id"] = goal_id
        try:
            self._writer()(
                TRAJECTORY_TOPIC,
                payload,
                actor="trajectory",
                session_id=session_id or None,
                task_id=goal_id or None,
            )
        except Exception as exc:
            return {
                "status": "error",
                "error": {"code": type(exc).__name__, "reason": str(exc)},
                "bundle": bundle,
                "trajectory": entry,
            }
        return {"status": "ok", "error": None, "bundle": bundle, "trajectory": entry}

    # -- read -------------------------------------------------------------

    def read(
        self, goal_id: str = "", session_id: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        """Read raw trajectory entries back off the existing event log.

        Args:
            goal_id: restrict to one GoalRun.
            session_id: restrict to one session.
            limit: maximum entries.

        Returns:
            ``[{step, command, artifact_ref, preview_ref, ts}]`` in log order.
        """
        from server.events import event_store

        try:
            events = event_store.read_all(
                task_id=goal_id or None,
                session_id=session_id or None,
                topics={TRAJECTORY_TOPIC},
            )
        except Exception:
            return []
        return [dict(event.get("payload") or {}) for event in events or []][: max(0, limit)]

    def replay(self, goal_id: str = "", session_id: str = "", limit: int = 200) -> dict[str, Any]:
        """Walk a run's trajectory into command → artifact states.

        Args:
            goal_id: restrict to one GoalRun.
            session_id: restrict to one session.
            limit: maximum entries read.

        Returns:
            ``{"goal_id", "session_id", "states", "artifacts", "step_count"}``;
            ``states`` is empty (never an error) when nothing was recorded.
        """
        entries = assemble_trajectory(self.read(goal_id, session_id, limit))
        states = replay_states(entries)
        artifacts: list[str] = []
        for state in states:
            for artifact in state["artifacts_so_far"]:
                if artifact not in artifacts:
                    artifacts.append(artifact)
        return {
            "goal_id": goal_id,
            "session_id": session_id,
            "states": states,
            "artifacts": artifacts,
            "step_count": len(states),
        }


def read_trajectory(
    goal_id: str = "", session_id: str = "", *, append_event: Callable[..., Any] | None = None
) -> dict[str, Any]:
    """Module-level convenience wrapper for route handlers."""
    return Trajectory(append_event=append_event).replay(goal_id, session_id)
