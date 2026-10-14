"""W17: Configuration self-tampering protection.

Agent tools writing to:
- ~/.veya/*.json
- deploy/*flags*.env
- ~/.config/veya/*
MUST go through explicit human approval (never auto-allowed).
"""

from __future__ import annotations

from pathlib import Path


def is_protected_config_path(target: str | Path | None) -> bool:
    """Check if target matches ~/.veya/*.json, deploy/*flags*.env, or ~/.config/veya/*."""
    if not target:
        return False
    path_str = str(target).strip()
    if not path_str:
        return False

    home = Path.home().resolve()
    try:
        p = Path(path_str).expanduser()
        resolved = p.resolve()
    except Exception:
        p = Path(path_str)
        resolved = p

    # 1. ~/.veya/*.json (direct json files in ~/.veya)
    veya_dir = home / ".veya"
    try:
        if (
            resolved.parent == veya_dir or p.parent == Path("~/.veya").expanduser()
        ) and resolved.suffix == ".json":
            return True
        parts = Path(path_str).parts
        if ".veya" in parts:
            idx = parts.index(".veya")
            if len(parts) == idx + 2 and parts[-1].endswith(".json"):
                return True
    except Exception:
        pass

    # 2. deploy/*flags*.env
    parts = Path(path_str).parts
    filename = Path(path_str).name
    if "deploy" in parts and "flags" in filename and filename.endswith(".env"):
        return True
    if (
        (path_str.startswith("deploy/") or "/deploy/" in path_str)
        and "flags" in filename
        and filename.endswith(".env")
    ):
        return True

    # 3. ~/.config/veya/*
    config_veya = home / ".config" / "veya"
    try:
        if resolved == config_veya or config_veya in resolved.parents:
            return True
        if (
            path_str.startswith("~/.config/veya/")
            or "/.config/veya/" in path_str
            or path_str.startswith(".config/veya/")
        ):
            return True
    except Exception:
        pass

    return False


def check_config_protection(
    tool: str,
    path: str | Path | None,
    *,
    approved: bool = False,
    approval_id: str = "",
) -> None:
    """Raise ToolExecutionError if an unapproved write to a protected config path is attempted."""
    if not is_protected_config_path(path):
        return

    from server.tool_registry import ToolExecutionError

    if not approved:
        raise ToolExecutionError(
            f"已拒绝：配置防自篡改保护。{tool} 写入受保护配置（~/.veya/*.json、deploy/*flags*.env、~/.config/veya/*）"
            "必须走人工审批（approved=true 且持有效 approval_id）。"
        )

    if not approval_id:
        raise ToolExecutionError(
            "已拒绝：配置防自篡改保护。写入受保护配置必须携带经批准的 approval_id。"
        )

    from server.approvals import get_approval_service

    service = get_approval_service()
    rec = service.get(str(approval_id))
    if not rec:
        raise ToolExecutionError("已拒绝：未找到对应的 approval_id。")
    if rec.get("status") != "completed":
        raise ToolExecutionError("已拒绝：审批尚未完成。")
    if rec.get("decision") not in {"allow", "once", "session", "always"}:
        raise ToolExecutionError(f"已拒绝：审批未获批准（当前决策: {rec.get('decision')}）。")
    if str(rec.get("principal") or "") == "agent":
        raise ToolExecutionError(
            "已拒绝：配置防自篡改保护。该审批必须由人类审批者批准，不可由 agent 自动批准。"
        )


__all__ = [
    "check_config_protection",
    "is_protected_config_path",
]
