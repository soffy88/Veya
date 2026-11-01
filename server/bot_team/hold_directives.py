"""Room-member hold directives (OpenMausBot room-holds method, adapted).

A user message that says nothing but ``@beta stop`` holds that member;
``@beta resume`` releases one; a bare resume word releases all held
members. Everything is deterministic: no model is involved, and a
message that mixes a directive with other content is an ordinary
message, not a directive, so a directive can never misfire on chat
about stopping something.

Quoted text and code blocks are masked before matching (a stop word
inside a quote or a code span must not hold anyone), and the stop word
must sit next to the mention it controls (within 16 characters).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Words that hold a member, longest first (Chinese substring match).
STOP_WORDS = ("stop", "停止", "停一下", "暂停", "别说了", "先别说", "安静", "停")

#: Words that release a member, longest first.
RESUME_WORDS = ("resume", "全部恢复", "接着说", "继续", "恢复", "解除")

#: How close a control word must sit to the mention it controls.
_WINDOW = 10

#: Cap on stashed messages per held member (upstream's cap).
STASH_CAP = 20

_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_QUOTE_LINE_RE = re.compile(r"(?m)^[ \t]*>.*$")


@dataclass(frozen=True)
class HoldDirective:
    """A parsed hold/resume directive."""

    holds: tuple[str, ...] = ()
    resumes: tuple[str, ...] = ()
    resume_all: bool = False


def mask_quoted_and_code(text: str) -> str:
    """Blank quoted lines and code spans, keeping offsets stable."""

    def _blank(match: re.Match[str]) -> str:
        return " " * len(match.group(0))

    masked = _FENCED_RE.sub(_blank, str(text or ""))
    masked = _INLINE_CODE_RE.sub(_blank, masked)
    masked = _QUOTE_LINE_RE.sub(_blank, masked)
    return masked


def _mention_spans(masked: str, member_ids: list[str], registry: Any) -> list[tuple[str, int, int]]:
    """``@`` mentions of members: (bot_id, start, end), first-mention order."""

    spans: list[tuple[str, int, int]] = []
    for bot_id in member_ids:
        names = [bot_id]
        spec = registry.get(bot_id) if registry is not None else None
        display = str(getattr(spec, "name", "") or "").strip()
        if display and display.lower() != bot_id.lower():
            names.append(display)
        for name in names:
            pattern = re.compile(
                r"(?<![A-Za-z0-9_@])@" + re.escape(name) + r"(?![A-Za-z0-9_])",
                re.IGNORECASE,
            )
            for match in pattern.finditer(masked):
                spans.append((bot_id, match.start(), match.end()))
    spans.sort(key=lambda span: span[1])
    seen: set[str] = set()
    ordered: list[tuple[str, int, int]] = []
    for span in spans:
        if span[0] in seen:
            continue
        seen.add(span[0])
        ordered.append(span)
    return ordered


def _word_near(masked: str, start: int, end: int, words: tuple[str, ...]) -> bool:
    window = masked[max(0, start - _WINDOW) : end + _WINDOW]
    for word in words:
        if all(ord(char) < 128 for char in word):
            if re.search(r"\b" + re.escape(word) + r"\b", window, re.IGNORECASE):
                return True
        elif word in window:
            return True
    return False


def _has_word(masked: str, words: tuple[str, ...]) -> bool:
    for word in words:
        if all(ord(char) < 128 for char in word):
            if re.search(r"\b" + re.escape(word) + r"\b", masked, re.IGNORECASE):
                return True
        elif word in masked:
            return True
    return False


def _strip_control_words(masked: str, spans: list[tuple[str, int, int]]) -> str:
    text = masked
    for _, start, end in sorted(spans, key=lambda span: span[1], reverse=True):
        text = text[:start] + " " * (end - start) + text[end:]
    for word in STOP_WORDS + RESUME_WORDS:
        if all(ord(char) < 128 for char in word):
            text = re.sub(r"\b" + re.escape(word) + r"\b", " ", text, flags=re.IGNORECASE)
        else:
            text = text.replace(word, " ")
    return re.sub(r"[\s,.，。；;!！?？、：:]+", "", text)


def classify_hold_directive(
    text: str, member_ids: list[str], registry: Any
) -> HoldDirective | None:
    """Parse a hold/resume directive, or ``None`` for an ordinary message."""

    masked = mask_quoted_and_code(text)
    spans = _mention_spans(masked, member_ids, registry)
    if not spans:
        # A bare resume word, and nothing else, releases everyone.
        remainder = _strip_control_words(masked, [])
        if remainder == "" and _has_word(masked, RESUME_WORDS):
            return HoldDirective(resume_all=True)
        return None
    holds: list[str] = []
    resumes: list[str] = []
    for bot_id, start, end in spans:
        if _word_near(masked, start, end, RESUME_WORDS):
            resumes.append(bot_id)
        elif _word_near(masked, start, end, STOP_WORDS):
            holds.append(bot_id)
    if not holds and not resumes:
        return None
    if _strip_control_words(masked, spans) != "":
        # Mixed content: an ordinary message that happens to mention
        # stopping something. Never a directive.
        return None
    return HoldDirective(holds=tuple(holds), resumes=tuple(resumes))


__all__ = [
    "STASH_CAP",
    "HoldDirective",
    "classify_hold_directive",
    "mask_quoted_and_code",
]
