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


def render(content: str, *, max_lines: int = 2000) -> str:
    lines = content.splitlines()
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines]
    out = [f"{i:>6}|LINE#{line_hash(ln)}|{ln}" for i, ln in enumerate(lines, 1)]
    if truncated:
        out.append(f"... truncated after {max_lines} lines; re-read a smaller range or grep first")
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


def apply(content: str, *, start_tag: str, new_text: str, end_tag: str | None = None) -> dict[str, Any]:
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
    if not ended_with_nl and body.endswith(nl) and not new_text.endswith("\n"):
        # original had no trailing newline and we didn't intend to add one
        if not replacement or not new_text.endswith("\n"):
            pass
    return {
        "content": body,
        "start_line": i + 1,
        "end_line": j + 1,
        "replaced_lines": j - i + 1,
        "new_lines": len(replacement),
    }
