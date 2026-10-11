"""server/nano_w9/acp_members — ACP agents as group/bot members.

NANO-W9 element 3.  An ACP agent (goose / codex-acp / claude-code-acp, …) can be
attached to a P2 group or a bot as a **member**.  Three rules hold everywhere:

* **ACP is not an authority.**  It cannot start, approve, schedule, or decide.
  It is an *executor/harness provider*: turns are forwarded through the existing
  ``project_run_goal`` and its result is a task outcome, nothing more.
* **No new member store.**  A member is an ordinary ``BotSpec`` in the existing
  P1 ``BotRegistry`` whose ``runner_id`` is ``acp:<backend_id>``.
* **Detection reuses W1.**  ``runtime/coding/cli_agents.CLIAgentRegistry`` finds
  installed CLI agents; ``server/backends.py`` already stores ``kind="acp"``
  backends.  W1 adds no probe of its own.

The byte-level stdio JSON-RPC transport is the canonical
:mod:`veya.obase.acp_stdio`; the ACP *semantics* live in
:mod:`server.acp_client`; this module is the glue plus the GoalRun hand-off.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from server.nano_w9.flags import require_flag

__all__ = [
    "ACP_MEMBER_KIND",
    "AcpMembers",
    "acp_runner_id",
    "detect_acp_candidates",
    "is_acp_member",
]

#: ``BotSpec.runner_id`` prefix identifying an ACP-backed member.
ACP_MEMBER_KIND = "acp"

#: ``BotSpec.knowledge_namespace`` used for ACP members (keeps memory separate).
_ACP_NAMESPACE = "acp"


def acp_runner_id(backend_id: str) -> str:
    """The ``BotSpec.runner_id`` that marks a bot as an ACP member.

    Args:
        backend_id: id of the ``kind="acp"`` backend in ``server/backends.py``.

    Returns:
        ``acp:<backend_id>``.

    Example:
        >>> acp_runner_id("b1")
        'acp:b1'
    """
    return f"{ACP_MEMBER_KIND}:{backend_id}"


def is_acp_member(spec: Any) -> bool:
    """Is this bot spec an ACP member?

    Args:
        spec: a ``BotSpec`` or its ``to_dict()``.

    Returns:
        True when ``runner_id`` carries the ``acp:`` prefix.

    Example:
        >>> is_acp_member({"runner_id": "acp:b1"}), is_acp_member({"runner_id": "local"})
        (True, False)
    """
    if isinstance(spec, dict):
        runner_id = str(spec.get("runner_id") or "")
    else:
        runner_id = str(getattr(spec, "runner_id", "") or "")
    return runner_id.startswith(f"{ACP_MEMBER_KIND}:")


def detect_acp_candidates(
    *,
    cli_registry: Any = None,
    backends: Any = None,
    repo_root: str = ".",
) -> list[dict[str, Any]]:
    """Find ACP agents that could become members.

    Detection is **reused**, never reimplemented: W1's ``CLIAgentRegistry``
    reports which CLI agents are installed, and the existing backend store
    reports the already-registered ``kind="acp"`` backends.

    Args:
        cli_registry: an existing ``CLIAgentRegistry`` (constructed if omitted).
        backends: the existing backend store/list (``server/backends.py``); when
            omitted the module is imported lazily.
        repo_root: used only when ``cli_registry`` is omitted.

    Returns:
        ``[{"backend_id", "command", "source", "available", "name"}]`` sorted by
        backend id.  Empty when nothing is installed — never an exception.

    Example:
        >>> _ = detect_acp_candidates(backends=[])
        []
    """
    out: list[dict[str, Any]] = []

    if backends is None:
        try:
            from server import backends as backends_module

            backends = backends_module
        except Exception:
            backends = []

    for entry in _iter_acp_backends(backends):
        out.append(entry)

    registry = cli_registry
    if registry is None:
        try:
            from runtime.coding.cli_agents import get_cli_agent_registry

            registry = get_cli_agent_registry(repo_root)
        except Exception:
            registry = None
    if registry is not None:
        for profile in _iter_cli_profiles(registry):
            command = str(getattr(profile, "command_template", "") or "")
            if not command or command in {str(e.get("command", "")) for e in out}:
                continue
            out.append(
                {
                    "backend_id": str(getattr(profile, "name", "") or ""),
                    "command": command,
                    "source": "cli_agent_registry",
                    "available": bool(getattr(profile, "available", False)),
                    "name": str(getattr(profile, "name", "") or ""),
                }
            )

    out.sort(key=lambda e: str(e.get("backend_id") or ""))
    return out


def _iter_acp_backends(backends: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if backends is None:
        return rows
    source = backends
    if isinstance(backends, dict):
        source = list(backends.values())
    elif hasattr(backends, "list_backends"):
        try:
            source = backends.list_backends() or []
        except Exception:
            source = []
    elif hasattr(backends, "list"):
        try:
            source = backends.list() or []
        except Exception:
            source = []
    if not isinstance(source, (list, tuple)):
        return rows
    for spec in source or []:
        if not isinstance(spec, dict):
            spec = getattr(spec, "__dict__", {}) or {}
        if str(spec.get("kind") or "") != ACP_MEMBER_KIND:
            continue
        rows.append(
            {
                "backend_id": str(spec.get("name") or spec.get("id") or ""),
                "command": list(spec.get("command") or []),
                "source": "backend_store",
                "available": bool(spec.get("enabled", True)),
                "name": str(spec.get("agent") or spec.get("name") or ""),
            }
        )
    return rows


def _iter_cli_profiles(registry: Any) -> list[Any]:
    for attr in ("list_all", "list"):
        fn = getattr(registry, attr, None)
        if callable(fn):
            try:
                return list(fn() or [])
            except Exception:
                return []
    return []


class AcpMembers:
    """Layer-4 facade: register ACP members and forward turns via GoalRun.

    Every dependency is injected so nothing here needs a live ACP process, a real
    ``~/.veya``, or a running GoalRun in a test.
    """

    def __init__(
        self,
        *,
        bot_registry: Any = None,
        goal_runner: Callable[..., Any] | None = None,
        project_root: str = ".",
        cli_registry: Any = None,
        backends: Any = None,
    ) -> None:
        self.bot_registry = bot_registry
        self.goal_runner = goal_runner
        self.project_root = project_root
        self.cli_registry = cli_registry
        self.backends = backends

    # -- membership ------------------------------------------------------

    def members(self) -> list[dict[str, Any]]:
        """Every registered ACP member (``BotSpec`` projections).

        Raises:
            FeatureDisabled: when ``VEYA_ACP_MEMBERS`` is off.
        """
        require_flag("VEYA_ACP_MEMBERS")
        if self.bot_registry is None:
            return []
        try:
            specs = self.bot_registry.list()
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for spec in specs or []:
            data = spec.to_dict() if hasattr(spec, "to_dict") else dict(spec)
            if is_acp_member(data):
                out.append(data)
        return out

    def candidates(self) -> list[dict[str, Any]]:
        """Detectable ACP agents (W1 registry + existing backend store).

        Raises:
            FeatureDisabled: when ``VEYA_ACP_MEMBERS`` is off.
        """
        require_flag("VEYA_ACP_MEMBERS")
        return detect_acp_candidates(
            cli_registry=self.cli_registry,
            backends=self.backends,
            repo_root=self.project_root,
        )

    def register_member(
        self,
        backend_id: str,
        *,
        owner_id: str = "",
        display_name: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """Register an ACP backend as a P1 bot (a group/bot member).

        Args:
            backend_id: id of a ``kind="acp"`` backend.
            owner_id: bot owner; required by the P1 registry's own validation.
            display_name: human name; defaults to ``acp:<backend_id>``.
            description: free-text description shown in the group.

        Returns:
            ``{"status", "error", "member"}``.  Refuses to overwrite an existing
            bot id, and refuses when no registry is injected.

        Raises:
            FeatureDisabled: when ``VEYA_ACP_MEMBERS`` is off.
        """
        require_flag("VEYA_ACP_MEMBERS")
        if not backend_id:
            return {"status": "error", "error": {"code": "INVALID_INPUT"}, "member": {}}
        if not owner_id:
            return {
                "status": "error",
                "error": {"code": "INVALID_INPUT", "reason": "owner_id is required"},
                "member": {},
            }
        if self.bot_registry is None:
            return {
                "status": "error",
                "error": {"code": "NO_REGISTRY", "reason": "bot registry not injected"},
                "member": {},
            }
        bot_id = acp_runner_id(backend_id)
        try:
            if self.bot_registry.get(bot_id) is not None:
                return {
                    "status": "error",
                    "error": {"code": "ALREADY_REGISTERED", "member": bot_id},
                    "member": {},
                }
        except Exception:
            pass

        from server.goal_run.bot_identity import BotSpec

        spec = BotSpec(
            bot_id=bot_id,
            owner_id=owner_id,
            knowledge_namespace=f"{_ACP_NAMESPACE}-{backend_id}",
            context_namespace=f"{_ACP_NAMESPACE}-{backend_id}",
            name=display_name or bot_id,
            description=description or f"ACP agent member ({backend_id})",
            runner_id=bot_id,
        )
        try:
            self.bot_registry.register(spec)
        except Exception as exc:
            return {
                "status": "error",
                "error": {"code": type(exc).__name__, "reason": str(exc)},
                "member": {},
            }
        return {"status": "ok", "error": None, "member": spec.to_dict()}

    # -- turn forwarding (ACP is an executor, never an authority) --------

    async def forward_turn(
        self,
        *,
        member_id: str,
        prompt: str,
        goal_id: str = "",
        wait: bool = False,
    ) -> dict[str, Any]:
        """Forward one turn to an ACP member **through GoalRun**.

        The ACP agent runs as a leaf task inside the existing GoalRun; it never
        gets to pick its own plan, approve anything, or schedule work.

        Args:
            member_id: the ACP member's ``acp:<backend_id>`` bot id.
            prompt: the instruction text handed to the agent.
            goal_id: optional parent goal to attach the task to.
            wait: block until the GoalRun finishes (default: enqueue only).

        Returns:
            ``{"status", "error", "goal_id", "member_id"}``.

        Raises:
            FeatureDisabled: when ``VEYA_ACP_MEMBERS`` is off.
        """
        require_flag("VEYA_ACP_MEMBERS")
        if not member_id or not is_acp_member({"runner_id": member_id}):
            return {
                "status": "error",
                "error": {"code": "NOT_ACP_MEMBER", "member": member_id},
                "goal_id": goal_id,
                "member_id": member_id,
            }
        if not prompt.strip():
            return {
                "status": "error",
                "error": {"code": "INVALID_INPUT", "reason": "prompt is required"},
                "goal_id": goal_id,
                "member_id": member_id,
            }
        runner = self.goal_runner
        if runner is None:
            try:
                from server.goal_run.runner import project_run_goal

                runner = project_run_goal
            except Exception as exc:
                return {
                    "status": "error",
                    "error": {"code": "NO_GOAL_RUNNER", "reason": str(exc)},
                    "goal_id": goal_id,
                    "member_id": member_id,
                }
        objective = (
            f"ACP member {member_id} executes one delegated turn.\n\n"
            f"Command: acp-member turn\nAcceptance: the turn produced a reply "
            f"attributable to {member_id}.\n\n{prompt}"
        )
        try:
            result = await runner(self.project_root, objective, wait=wait)
        except TypeError:
            result = await runner(self.project_root, objective)
        except Exception as exc:
            return {
                "status": "error",
                "error": {"code": type(exc).__name__, "reason": str(exc)},
                "goal_id": goal_id,
                "member_id": member_id,
            }
        goal = result
        if hasattr(result, "goal_id"):
            goal = result.goal_id
        elif isinstance(result, dict):
            goal = result.get("goal_id", goal_id)
        return {
            "status": "ok",
            "error": None,
            "goal_id": str(goal or goal_id),
            "member_id": member_id,
            "result": result if isinstance(result, dict) else {"goal_id": str(goal or goal_id)},
        }
