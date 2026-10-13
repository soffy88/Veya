"""W13 remote control — drive tailnet agents from phone/web.

Flag: ``VEYA_REMOTE_CONTROL`` (default OFF, registered in
``server/feature_flags.py``).

W13 is a **projection**, never a second authority. It reuses, and does not
replace:

* the flag registry (``server.feature_flags``) for rollout state;
* ``atomic_write_json`` for every durable write;
* ``server.approvals.ApprovalService`` as the *only* approval authority;
* ``server.bot_team.routine_clock`` + ``RoutineRegistry`` as the *only*
  scheduler;
* ``server.goal_run.verify`` / ``GoalStatus`` as the *only* source of
  completion evidence;
* ``server.notification_center`` / ``server.bot_team.push`` as the *only*
  notification path.

Design notes: ``W13_REMOTE_CONTROL_SPEC.md``.
Licence hygiene: Paseo (AGPL-3.0) was not consulted as source — clean-room
only. The three design ideas borrowed from Yobiru (MIT) are recorded in
``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

from server.remote_control.flags import FLAG_NAME, enabled

__all__ = ["FLAG_NAME", "enabled"]
