"""3O-PURE — tool error text the model can act on (NANO-W10 item 7a).

A tool error is the one piece of feedback the model gets for free, so it has to
answer two questions in one line: *what failed* and *what to do next*. A bare
``RuntimeError: boom`` answers neither — the model can only guess, and guessing
on a failing tool is how a ReAct loop burns its turn budget re-running the same
call.

Pure: the tool name, the error text and the schema-shaped arguments arrive as
values; the formatted line comes back. No IO, no clock, no user identity.

The mapping is deliberately conservative — it reads the error text for known
shapes (missing file, missing argument, timeout, permission, network) and falls
back to a generic "state the failure and try one narrower call" for anything it
does not recognise. An unrecognised failure must not get a confident wrong
diagnosis.
"""

from __future__ import annotations

import re
from typing import Any

#: The two parts every tool error must carry. The guard test asserts both are
#: present in the text the model actually reads.
WHAT_FAILED = "What failed:"
NEXT_STEP = "Next step:"

#: How much of the original error survives in the ``what failed`` clause. Long
#: tracebacks are truncated here; the full text stays in the telemetry event.
MAX_DETAIL_CHARS = 400

#: ``(marker, next step)`` pairs, matched case-insensitively against the error
#: text in order. First hit wins, so the more specific markers come first.
_REMEDIES: tuple[tuple[str, str], ...] = (
    (
        r"no such file or directory|filenotfounderror|file not found|enoent",
        "the path does not exist — list the directory first and use a path from that listing",
    ),
    (
        r"permissionerror|permission denied|eacces|epermission|not permitted|errno 13",
        "the target is not writable or readable by this user — pick a path you own, "
        "or ask the user to widen access",
    ),
    (
        r"is a required property|'[a-z_]+' is a required|missing.*argument|"
        r"arguments failed json schema validation|missing 1 required",
        "re-read the tool schema and pass every required argument by its exact name",
    ),
    (
        r"timed out|timeout",
        "the call exceeded its time budget — narrow the scope (smaller range, fewer "
        "items, one file) and try again",
    ),
    (
        r"connection|network|dns|ssl|certificate|unreachable",
        "the remote end could not be reached — check the URL and connectivity, "
        "or use a different source",
    ),
    (
        r"already exists|duplicate",
        "the target already exists — read it first, then update instead of creating",
    ),
    (
        r"not found|unknown tool|no such",
        "check the exact identifier against the tool schema; a name that is not in "
        "the schema cannot be called",
    ),
    (
        r"module.*not found|importerror|no module named",
        "the dependency is not installed in this environment — install it or choose "
        "a tool that does not need it",
    ),
)

#: When nothing matches: honest, and still actionable.
_GENERIC_NEXT_STEP = (
    "do not repeat the same call — change one thing (a path, an argument, or the "
    "approach) and try once more; if it fails again with the same text, the tool "
    "cannot do this and you should use a different one"
)

_WHITESPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WHITESPACE.sub(" ", str(text or "")).strip()


def suggest_next_step(error_text: str) -> str:
    """The actionable half: what the model should do about this failure.

    Args:
        error_text: the raw exception text.

    Returns:
        One sentence naming a concrete next action.
    """
    body = str(error_text or "").lower()
    for marker, remedy in _REMEDIES:
        if re.search(marker, body):
            return remedy
    return _GENERIC_NEXT_STEP


def explain_tool_error(
    tool_name: str,
    error_text: str,
    *,
    arguments: dict[str, Any] | None = None,
) -> str:
    """Format one tool failure so it states what broke and what to do next.

    Args:
        tool_name: the tool the model called.
        error_text: the exception text.
        arguments: the arguments the model passed, summarised by *name* only —
            values are never echoed back, because an argument may itself hold a
            secret the caller already masked elsewhere.

    Returns:
        A single-line string beginning ``tool '<name>' failed — What failed: …;
        Next step: …``.

    Example:
        >>> explain_tool_error("read_file_ast", "FileNotFoundError: missing.py")
        ... # 'tool read_file_ast failed — What failed: ...; Next step: the path does not exist ...'
        True
    """
    name = _normalise(tool_name) or "<unnamed tool>"
    detail = _normalise(error_text) or "no error detail was provided"
    if len(detail) > MAX_DETAIL_CHARS:
        detail = detail[:MAX_DETAIL_CHARS] + " …(truncated)"
    passed = ""
    if arguments:
        names = sorted(str(key) for key in arguments)
        if names:
            passed = f" (called with: {', '.join(names)})"
    return (
        f"tool {name!r} failed{passed} — "
        f"{WHAT_FAILED} {detail}; "
        f"{NEXT_STEP} {suggest_next_step(error_text)}"
    )


__all__ = [
    "MAX_DETAIL_CHARS",
    "NEXT_STEP",
    "WHAT_FAILED",
    "explain_tool_error",
    "suggest_next_step",
]
