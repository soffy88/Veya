"""Trigger dispatch: event/schedule → Routine lookup → handoff record (P2-C).

The dispatcher is a pure routing step. It resolves enabled routines for a
trigger topic, consumes the trigger identity exactly once per routine, and
returns handoff records for the Persistent Veya Bot → MasterAgent → existing
canonical GoalRun path. It never executes, never finalizes, never creates a
GoalRun, and never starts a parallel run: all trigger state lands in the
passed-in GoalRunState.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from runtime.execution.models import (
    ROUTINE_EXECUTION_AUTHORITY,
    assert_routine_execution_authority_zero,
)
from server.goal_run.bot_identity import require_same_bot
from server.goal_run.models import (
    ROUTINE_MAX_STARTED,
    ROUTINE_TRIGGER_TOPICS,
    GoalRunState,
    RoutineSpec,
    RoutineState,
)

HANDOFF_VIA = ("veya-bot", "master-agent", "goal-run")


def run_probe(
    probe: dict[str, Any],
    workspace_path: str | None = None,
    *,
    owner_authorized: bool = False,
    approved: bool = False,
) -> dict[str, Any]:
    """Execute a probe and return result dict with success status and details.

    Probe kinds:
    - "none": always returns success
    - "git_changes": checks for uncommitted changes in a git repo at probe.path
    - "command": runs a shell command, success if exit code == 0

    Returns: {"success": bool, "kind": str, "detail": str, "data": dict}
    """
    kind = probe.get("kind", "none")

    if kind == "none":
        return {"success": True, "kind": "none", "detail": "no probe", "data": {}}

    if kind == "git_changes":
        path = probe.get("path")
        if not path:
            return {
                "success": False,
                "kind": "git_changes",
                "detail": "probe.path is required",
                "data": {},
            }

        try:
            # Check if path is a git repository
            result = subprocess.run(
                ["git", "-C", path, "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return {
                    "success": False,
                    "kind": "git_changes",
                    "detail": f"not a git repository: {path}",
                    "data": {"path": path},
                }

            # Check for uncommitted changes
            result = subprocess.run(
                ["git", "-C", path, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            has_changes = bool(result.stdout.strip())
            return {
                "success": True,
                "kind": "git_changes",
                "detail": "changes detected" if has_changes else "no changes",
                "data": {"path": path, "has_changes": has_changes, "output": result.stdout.strip()},
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "kind": "git_changes",
                "detail": "git command timed out",
                "data": {"path": path},
            }
        except Exception as e:
            return {
                "success": False,
                "kind": "git_changes",
                "detail": f"git probe error: {e}",
                "data": {"path": path},
            }

    if kind == "command":
        # Arbitrary routine commands are opt-in only after platform-owner and
        # durable human-approval checks in the authenticated route. Never accept
        # a shell string: `execute_args` is passed to execve with shell disabled.
        if not owner_authorized or not approved:
            return {
                "success": False,
                "kind": "command",
                "detail": "command probes require an authenticated owner and explicit approval",
                "data": {},
            }
        argv = probe.get("execute_args")
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 32
            or any(
                not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 2048
                for arg in argv
            )
            or probe.get("command") not in (None, "")
        ):
            return {
                "success": False,
                "kind": "command",
                "detail": "probe.execute_args must be a bounded list of non-empty strings; shell commands are not accepted",
                "data": {},
            }
        if not workspace_path:
            return {
                "success": False,
                "kind": "command",
                "detail": "a bound workspace is required",
                "data": {},
            }
        try:
            workspace = Path(workspace_path).expanduser().resolve(strict=True)
            if not workspace.is_dir():
                raise ValueError("workspace is not a directory")
            requested_cwd = probe.get("cwd") or "."
            requested_path = Path(requested_cwd).expanduser()
            cwd = (
                requested_path if requested_path.is_absolute() else workspace / requested_path
            ).resolve(strict=True)
            if not cwd.is_relative_to(workspace) or not cwd.is_dir():
                raise ValueError("command cwd must be a directory inside the bound workspace")
            timeout = max(1, min(int(probe.get("timeout", 10)), 30))
            safe_env = {
                "PATH": os.defpath,
                "HOME": str(Path.home()),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
            if os.environ.get("TMPDIR"):
                safe_env["TMPDIR"] = os.environ["TMPDIR"]
            result = subprocess.run(
                argv,
                shell=False,
                cwd=str(cwd),
                env=safe_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                close_fds=True,
            )
            return {
                "success": result.returncode == 0,
                "kind": "command",
                "detail": "command succeeded"
                if result.returncode == 0
                else f"command failed with exit code {result.returncode}",
                "data": {
                    "execute_args": argv,
                    "cwd": str(cwd),
                    "exit_code": result.returncode,
                    "stdout": result.stdout.strip()[-1000:],
                    "stderr": result.stderr.strip()[-1000:],
                },
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "kind": "command",
                "detail": "command timed out",
                "data": {"cwd": str(workspace_path)},
            }
        except Exception as exc:
            return {
                "success": False,
                "kind": "command",
                "detail": f"command probe rejected: {exc}",
                "data": {},
            }

    return {"success": False, "kind": kind, "detail": f"unknown probe kind: {kind}", "data": {}}


def trigger_identity(trigger_topic: str, trigger_id: str) -> str:
    """Stable identity for one trigger occurrence (idempotency key)."""
    return f"{trigger_topic}:{trigger_id}"


def dispatch_trigger(
    state: GoalRunState,
    specs: list[RoutineSpec],
    *,
    topic: str,
    trigger_id: str,
    payload: dict[str, Any] | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Consume one trigger and return handoff records for MasterAgent.

    Idempotent: a trigger identity already consumed by a routine is never
    started again, including after restart (state survives in taskgraph.json).
    Capped: at most ROUTINE_MAX_STARTED routines start per dispatch call.
    Same-GoalRun: a routine already bound to another GoalRun is refused.
    """
    assert_routine_execution_authority_zero()
    if ROUTINE_EXECUTION_AUTHORITY != 0:
        raise AssertionError("routine execution authority must stay 0")
    if topic not in ROUTINE_TRIGGER_TOPICS:
        raise ValueError(f"unknown trigger topic {topic!r}")
    if not trigger_id:
        raise ValueError("trigger_id is required")

    identity = trigger_identity(topic, trigger_id)
    moment = time.time() if now is None else now
    handoffs: list[dict[str, Any]] = []
    started = 0
    for spec in specs:
        if not spec.enabled or spec.trigger_topic != topic:
            continue
        # P3-A: another bot's routine never dispatches into this GoalRun.
        require_same_bot(state.bot_id, spec.bot_id, f"routine:{spec.routine_id}")
        entry = state.routine_states.get(spec.routine_id)
        if entry is not None and entry.canonical_goal_run_id not in (None, state.goal_id):
            raise ValueError(
                f"routine {spec.routine_id!r} belongs to GoalRun "
                f"{entry.canonical_goal_run_id!r}, not {state.goal_id!r}"
            )
        if entry is not None and identity in entry.consumed_trigger_ids:
            continue  # already consumed — never start twice.
        if started >= ROUTINE_MAX_STARTED:
            break
        if entry is None:
            entry = RoutineState(
                routine_id=spec.routine_id,
                canonical_goal_run_id=state.goal_id,
                version=spec.version,
                bot_id=state.bot_id,
            )
            state.routine_states[spec.routine_id] = entry
        entry.version = spec.version
        entry.consumed_trigger_ids.append(identity)
        entry.started_count += 1
        entry.last_trigger_ref = trigger_id
        entry.canonical_goal_run_id = state.goal_id
        entry.trigger_metadata = {"trigger_id": trigger_id, **(payload or {})}

        target: dict[str, Any] = {}
        if spec.target_skill_id:
            target = {"skill_id": spec.target_skill_id}
        elif spec.target_playbook_id:
            target = {"playbook_id": spec.target_playbook_id}
        else:
            target = {"goal_template": spec.goal_template}
        handoffs.append(
            {
                "routine_id": spec.routine_id,
                "version": spec.version,
                "trigger_topic": topic,
                "trigger_id": trigger_id,
                "trigger_identity": identity,
                "target": target,
                "goal_run_id": state.goal_id,
                "evidence_requirements": list(spec.evidence_requirements),
                "dispatched_at": moment,
                "via": list(HANDOFF_VIA),
            }
        )
        started += 1
    return handoffs


__all__ = ["HANDOFF_VIA", "dispatch_trigger", "trigger_identity"]
