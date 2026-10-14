"""server/channels/sandbox_enforce.py — IM Sandbox Enforcement (FastClaw Clean-Room).

Enforces execution isolation for non-owner chatters arriving via IM channels
(or public bot chat links) under feature flag VEYA_IM_SANDBOX_ENFORCE
(and VEYA_BOT_PUBLIC_SHARE for public chat).

Hard rules:
- Non-owner IM turns cannot execute arbitrary host commands or mutate owner files.
- Exec/shell/file-write calls must route through the existing sandbox (run_in_sandbox
  via omodul.sandbox_scope, isolated worktrees) or be denied when no sandbox exists.
- Owner turns and flag-off runs behave exactly as today (zero behavior change).
- Reuses existing ToolGuard, PolicyResolver, and PermissionEngine authorities.
"""

from __future__ import annotations

import contextvars
import os
from typing import Any

from server import auth as auth_mod
from server import feature_flags

_im_turn_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "veya_im_turn_context", default=None
)

#: Explicit unsandboxed exec/shell tools that must be denied for non-owner turns
_UNSANDBOXED_EXEC_TOOLS = frozenset(
    {
        "system_secure_exec",
        "system_shell_exec",
        "ssh_run",
        "ssh_bg_start",
        "agent_loop_run",
        "coding_task_run",
        "evolve_solution",
        "delegate_to_genesis",
    }
)

#: Host/repo file mutation tools that must be denied for non-owner turns
_HOST_MUTATION_TOOLS = frozenset(
    {
        "edit_hashline",
        "ast_grep_rewrite",
        "edit_diagram",
        "produce_wechat_article",
        "ssh_write_file",
        "ssh_edit_file",
        "ssh_upload",
        "ssh_download",
        "team_delete",
        "team_approve_shutdown",
    }
)

#: Secret store tools that must be denied for non-owner turns
_SECRETS_TOOLS = frozenset(
    {
        "request_secret",
        "list_secrets",
    }
)


def bind_im_turn(
    *,
    is_im: bool = False,
    is_owner: bool = False,
    is_public_chat: bool = False,
    platform: str = "",
    bot_id: str = "",
) -> contextvars.Token:
    """Bind the current IM or public chat turn context for the duration of a turn."""
    return _im_turn_ctx.set(
        {
            "is_im": bool(is_im),
            "is_owner": bool(is_owner),
            "is_public_chat": bool(is_public_chat),
            "platform": str(platform or ""),
            "bot_id": str(bot_id or ""),
        }
    )


def reset_im_turn(token: contextvars.Token) -> None:
    """Reset the IM turn context."""
    _im_turn_ctx.reset(token)


def current_im_turn() -> dict[str, Any] | None:
    """Read the active IM turn context, if any."""
    return _im_turn_ctx.get()


def is_im_non_owner_turn() -> bool:
    """Whether the current turn is an IM channel or public chat turn from a non-owner."""
    ctx = _im_turn_ctx.get()
    user = auth_mod.current_user()

    # Public chat turn: enforced under VEYA_BOT_PUBLIC_SHARE
    is_public = (ctx is not None and ctx.get("is_public_chat")) or bool(user.get("is_public_chatter"))
    if is_public:
        is_owner = (ctx is not None and ctx.get("is_owner")) or bool(user.get("is_owner"))
        if not is_owner and feature_flags.enabled("VEYA_BOT_PUBLIC_SHARE"):
            return True

    # IM turn: enforced under VEYA_IM_SANDBOX_ENFORCE
    if not feature_flags.enabled("VEYA_IM_SANDBOX_ENFORCE"):
        return False

    if ctx is not None and ctx.get("is_im") and not ctx.get("is_owner"):
        return True

    return bool(user.get("is_im_channel") and not user.get("is_owner"))


def check_im_sandbox_tool(name: str, kwargs: dict[str, Any]) -> str | None:
    """Enforce sandbox routing or denial for one tool call.

    Returns:
        None if allowed (owner turn, flag off, or sandboxed execution),
        or a non-empty string explaining the denial reason.
    """
    if not is_im_non_owner_turn():
        return None

    # 1. Exec / Shell tools
    if name == "run_in_sandbox":
        if kwargs.get("host"):
            return "remote host execution is denied for non-owner IM chatter; use local run_in_sandbox"
        # Local run_in_sandbox runs inside omodul.sandbox_scope ("chat_verify").
        # This IS the existing canonical sandbox path.
        return None

    if name == "run_backtest_coprocessor":
        # Runs in isolated quant coprocessor sandbox
        return None

    if name in _UNSANDBOXED_EXEC_TOOLS:
        return (
            f"unsandboxed exec/shell tool '{name}' is denied for non-owner IM chatter; "
            "execution must use run_in_sandbox"
        )

    # MCP tool adapter commands (shell.exec, test.run, build.run)
    if name in {"shell.exec", "test.run", "build.run"}:
        target = str(kwargs.get("execution_target") or "").upper()
        profile = str(kwargs.get("profile") or "").lower()
        if target in {"CANONICAL_WORKTREE", "HOST"} or profile in {"l0_host", "host"}:
            return (
                f"unsandboxed host command '{name}' is denied for non-owner IM chatter; "
                "execution must be sandboxed"
            )
        return None

    # 2. File-write tools
    if name == "write_file":
        if kwargs.get("host"):
            return "remote host write_file is denied for non-owner IM chatter"
        filepath = str(kwargs.get("filepath") or kwargs.get("path") or "")
        from server.config_protection import is_protected_config_path

        if is_protected_config_path(filepath):
            return f"protected config write is denied for non-owner IM chatter: {filepath}"

        # If writing directly to repository root without bound sandbox root
        from server.tool_registry import _current_write_root

        bound_root = _current_write_root.get()
        if bound_root is None:
            resolved_path = os.path.abspath(os.path.expanduser(filepath))
            cwd = os.getcwd()
            if resolved_path == cwd or resolved_path.startswith(cwd + os.sep):
                return f"direct workspace write is denied for non-owner IM chatter: {filepath}"
        return None

    if name in _HOST_MUTATION_TOOLS:
        return f"workspace file mutation tool '{name}' is denied for non-owner IM chatter"

    if name in {"file.write", "file.patch"}:
        target = str(kwargs.get("execution_target") or "").upper()
        if target in {"CANONICAL_WORKTREE", "HOST"}:
            return f"host file write '{name}' is denied for non-owner IM chatter"
        return None

    # 3. Secret tools
    if name in _SECRETS_TOOLS:
        return f"secrets tool '{name}' is denied for non-owner IM chatter"

    return None
