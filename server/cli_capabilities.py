"""Frozen inventory of the external CLI agent engines Veya can spawn.

The argv table (``platform/3O/oskill/oskill/_harness_argv.py``) answers *how* to
launch an engine. It does not answer *what the launched engine can do* or *how
long it may stay quiet*. Those two questions are answered here, once, as data:

* the capability facts let routing/UI code ask about an engine without
  importing the argv builder or guessing from the binary name;
* the timeout defaults are the fallback the process supervisor uses when a
  caller passes no explicit deadline.

The record is frozen on purpose. A capability is a claim, and a claim that
drifts silently is how a watchdog ends up killing a healthy run. Anything the
argv table does not settle is recorded conservatively (``False`` /
``read_only``) with the reason next to it — never optimistically.

Values were read off the argv table at the time of writing:

=============  ==========================================================
engine         argv (non-streaming → streaming)
=============  ==========================================================
claude_code    ``claude -p <prompt>`` → ``--output-format stream-json --verbose``
codex          ``codex exec --skip-git-repo-check --sandbox workspace-write``
grok           ``grok -p <prompt>`` → ``--output-format streaming-messages-json``
pi             ``pi -p <prompt>`` (no streaming flags)
dsh            ``dsh --profile headless <prompt>`` (no streaming flags)
opencode       ``opencode run <prompt> --format json`` (same flags when streaming)
=============  ==========================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PermissionScope = Literal["read_only", "workspace_write", "full"]
OutputFormat = Literal["text", "json", "stream-json"]

#: Canonical ids, matching ``veya.remote.executor_registry`` vocabulary where
#: the two overlap (``claude_code``, not ``claude``).
CANONICAL_NAMES: tuple[str, ...] = (
    "claude_code",
    "codex",
    "grok",
    "pi",
    "dsh",
    "opencode",
)

#: Aliases accepted on lookup. ``engine_runner`` talks about ``claude``; the
#: executor registry talks about ``claude_code``; both mean this record.
_ALIASES = {
    "claude": "claude_code",
    "claude-code": "claude_code",
    "claude_code": "claude_code",
    "open-code": "opencode",
    "opencode_go": "opencode",
}


@dataclass(frozen=True, slots=True)
class CliCapability:
    """What one external CLI engine can do, and how long it may run.

    ``output_format`` is the format emitted in the mode Veya actually uses
    (``streaming=True`` when the engine supports it, plain output otherwise).
    ``permission_scope`` describes the argv the table builds, not the engine's
    theoretical maximum: codex passes ``--sandbox workspace-write`` explicitly,
    while the others run with their CLI default and are recorded as
    ``read_only`` until a mode that widens them is verified.
    """

    name: str
    binary: str
    supports_resume: bool
    supports_streaming: bool
    output_format: OutputFormat
    permission_scope: PermissionScope
    default_idle_timeout_s: float
    default_wall_timeout_s: float


# Idle defaults are deliberately generous: an agent that is thinking, compiling
# or waiting on a model can be silent for minutes. 480s only catches a child
# that is truly wedged (no stdout *and* no stderr). The wall default stays at
# the historical 600s so callers without an explicit timeout keep old behavior.
_CAPABILITIES: dict[str, CliCapability] = {
    "claude_code": CliCapability(
        name="claude_code",
        binary="claude",
        # Claude Code ships --resume/--continue; the argv table does not use it
        # yet, so this records the engine, not our launch configuration.
        supports_resume=True,
        supports_streaming=True,
        output_format="stream-json",
        # `-p` without --permission-mode/--dangerously-skip-permissions cannot
        # answer a permission prompt, so tool use stays effectively denied.
        permission_scope="read_only",
        default_idle_timeout_s=480.0,
        default_wall_timeout_s=600.0,
    ),
    "codex": CliCapability(
        name="codex",
        binary="codex",
        # `codex exec resume` exists upstream, but Veya has not exercised it.
        supports_resume=False,
        # exec streams progress, but the argv table adds no --json: the output
        # is human-readable text, not structured events.
        supports_streaming=True,
        output_format="text",
        # Explicit in the argv table: `--sandbox workspace-write`.
        permission_scope="workspace_write",
        default_idle_timeout_s=480.0,
        default_wall_timeout_s=600.0,
    ),
    "grok": CliCapability(
        name="grok",
        binary="grok",
        supports_resume=False,
        supports_streaming=True,
        output_format="stream-json",
        # `grok -p` with no permission flag; unknown, so assume the narrow one.
        permission_scope="read_only",
        default_idle_timeout_s=480.0,
        default_wall_timeout_s=600.0,
    ),
    "pi": CliCapability(
        name="pi",
        binary="pi",
        supports_resume=False,
        # The argv table adds no streaming flag for pi: output arrives as text.
        supports_streaming=False,
        output_format="text",
        permission_scope="read_only",
        default_idle_timeout_s=480.0,
        default_wall_timeout_s=600.0,
    ),
    "dsh": CliCapability(
        name="dsh",
        binary="dsh",
        supports_resume=False,
        supports_streaming=False,
        output_format="text",
        permission_scope="read_only",
        default_idle_timeout_s=480.0,
        default_wall_timeout_s=600.0,
    ),
    "opencode": CliCapability(
        name="opencode",
        binary="opencode",
        # --session/--continue exist, but Veya's argv never passes them.
        supports_resume=False,
        # HTTP SSE path plus line-delimited JSON from `run --format json`.
        supports_streaming=True,
        output_format="json",
        # `--auto` (worktree writes) is only added for a verified coding mode;
        # the default record therefore stays read-only.
        permission_scope="read_only",
        default_idle_timeout_s=480.0,
        default_wall_timeout_s=600.0,
    ),
}


def normalize_name(name: str) -> str:
    """Canonicalise an engine id; unknown names pass through unchanged."""
    key = str(name or "").strip().lower()
    return _ALIASES.get(key, key)


def get_capability(name: str) -> CliCapability:
    """Return the capability record for ``name`` (aliases accepted).

    Raises ``ValueError`` for an id with no record, because a fabricated
    default would make an unknown engine look like a known one.
    """
    key = normalize_name(name)
    record = _CAPABILITIES.get(key)
    if record is None:
        raise ValueError(f"unknown CLI engine {name!r}; known: {sorted(_CAPABILITIES)}")
    return record


def find_capability(name: str) -> CliCapability | None:
    """Non-raising variant for callers that already handle unknown engines."""
    return _CAPABILITIES.get(normalize_name(name))


def list_capabilities() -> tuple[CliCapability, ...]:
    """All records in canonical order (frozen copy, safe to iterate)."""
    return tuple(_CAPABILITIES[name] for name in CANONICAL_NAMES)


__all__ = [
    "CANONICAL_NAMES",
    "CliCapability",
    "OutputFormat",
    "PermissionScope",
    "find_capability",
    "get_capability",
    "list_capabilities",
    "normalize_name",
]
