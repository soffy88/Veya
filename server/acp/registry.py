"""ACP adapter startup commands and availability checks."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from .client import acp_enabled, normalize_adapter


@dataclass(frozen=True, slots=True)
class ACPAdapter:
    name: str
    argv: tuple[str, ...]


def adapter_for(name: str, *, env: dict[str, str] | None = None) -> ACPAdapter:
    """Resolve one adapter's ACP command; raises when missing/unavailable."""

    source_env = os.environ if env is None else env
    if not acp_enabled():
        raise RuntimeError("ACP is disabled (VEYA_ACP)")
    key = normalize_adapter(name)
    override = str(source_env.get(f"VEYA_ACP_{key.upper().replace('-', '_')}_CMD") or "").strip()
    defaults = {
        "goose": "goose acp",
        "codex": "codex acp",
        "claude-code": "claude-agent-acp",
    }
    raw = override or defaults.get(key, "")
    if not raw:
        raise RuntimeError(f"ACP adapter {name!r} has no startup command")
    command = raw.split()
    if not shutil.which(command[0]):
        raise RuntimeError(f"ACP adapter {name!r} binary {command[0]!r} is unavailable")
    return ACPAdapter(key, tuple(command))


def available_adapters(*, env: dict[str, str] | None = None) -> tuple[str, ...]:
    """Adapters with a configured, present binary; empty when ACP is off."""

    names = ("goose", "codex", "claude-code")
    return tuple(name for name in names if _available(name, env=env))


def _available(name: str, *, env: dict[str, str] | None = None) -> bool:
    try:
        adapter_for(name, env=env)
    except RuntimeError:
        return False
    return True
