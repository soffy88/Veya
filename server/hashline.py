"""Hashline anchors: content-hash tags on each line so edits fail if the file drifted.

Read renders ``{lineno}|LINE#{hash8}|{text}``. Edit cites start/end tags; apply
recomputes hashes on the live file and refuses if they no longer match. This is
edit safety, not task routing.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_TAG = re.compile(r"(?:LINE#)?([0-9a-f]{8})\b", re.I)
HASH_LEN = 8

#: Read-window bounds, single source of truth.  ``server.tool_registry`` and
#: ``veya.remote.tool_adapter`` both clamp to these so the bound cannot be
#: looser on one call path than another.  HARD_MAX_LINES is a ceiling on what a
#: caller may ask for, not a suggestion.
DEFAULT_MAX_LINES = 2000
HARD_MAX_LINES = 8000


class HashlineError(ValueError):
    """Stale, ambiguous, or malformed hashline edit."""


def line_hash(text: str) -> str:
    body = text.rstrip("\r\n")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:HASH_LEN]


def parse_tag(tag: str) -> str:
    raw = (tag or "").strip()
    m = _TAG.search(raw)
    if not m:
        raise HashlineError(
            f"invalid hashline tag {tag!r}; expected LINE# plus 8 hex chars from read_hashline"
        )
    return m.group(1).lower()


def render(content: str, *, max_lines: int = 2000, start_line: int = 1) -> str:
    """Render a window of ``content`` with hash tags, numbered from ``start_line``.

    The tag is always the hash of a real file line, so an edit citing any tag in
    the output stays verifiable against the live file regardless of the window.
    The printed number is the true 1-based file line, not the position within
    the window, so a caller reading a window from deep in a file is not misled
    about where those lines live.
    """
    every = content.splitlines()
    begin = max(1, int(start_line or 1))
    window = every[begin - 1 :] if begin <= len(every) else []
    truncated = len(window) > max_lines
    if truncated:
        window = window[:max_lines]
    out = [f"{i:>6}|LINE#{line_hash(ln)}|{ln}" for i, ln in enumerate(window, begin)]
    if truncated:
        nxt = begin + max_lines
        out.append(f"... truncated after line {nxt - 1}; re-read with start_line={nxt}")
    return "\n".join(out)


def _find_range(hashes: list[str], start_h: str, end_h: str) -> tuple[int, int]:
    starts = [i for i, h in enumerate(hashes) if h == start_h]
    if not starts:
        raise HashlineError(
            f"start tag LINE#{start_h} not found (file changed). Re-read with read_hashline."
        )
    candidates: list[tuple[int, int]] = []
    for i in starts:
        if start_h == end_h:
            candidates.append((i, i))
            continue
        for j in range(i, len(hashes)):
            if hashes[j] == end_h:
                candidates.append((i, j))
                break
    if not candidates:
        raise HashlineError(
            f"end tag LINE#{end_h} not found after start LINE#{start_h}. Re-read with read_hashline."
        )
    uniq = list(dict.fromkeys(candidates))
    if len(uniq) > 1:
        raise HashlineError(
            f"tags LINE#{start_h}..LINE#{end_h} are ambiguous ({len(uniq)} ranges). "
            "Re-read and pick a unique span, or include more distinct lines."
        )
    return uniq[0]


def apply(
    content: str, *, start_tag: str, new_text: str, end_tag: str | None = None
) -> dict[str, Any]:
    start_h = parse_tag(start_tag)
    end_h = parse_tag(end_tag) if (end_tag or "").strip() else start_h
    nl = "\r\n" if "\r\n" in content else "\n"
    # splitlines() drops a trailing blank line marker; keepends reconstruction
    raw_lines = content.splitlines()
    ended_with_nl = content.endswith("\n") or content.endswith("\r\n")
    hashes = [line_hash(ln) for ln in raw_lines]
    i, j = _find_range(hashes, start_h, end_h)
    replacement = new_text.splitlines()
    updated_lines = raw_lines[:i] + replacement + raw_lines[j + 1 :]
    body = nl.join(updated_lines)
    if ended_with_nl and (body and not body.endswith(("\n", "\r\n"))):
        body += nl
    return {
        "content": body,
        "start_line": i + 1,
        "end_line": j + 1,
        "replaced_lines": j - i + 1,
        "new_lines": len(replacement),
    }
