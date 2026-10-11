"""3O-PURE — memory_layers: memory tiering rules (NANO-W10 item 5 + ADDENDUM O).

Four memory tiers, four deterministic rule sets, zero I/O:

* **daily** — dated append-only logs, searchable but never injected;
* **scratch** — TTL entries injected while live, garbage-collected after;
* **core** (``MEMORY.md``) — always injected, size-capped, *proposed* trims only;
* **review** — which candidate entries may be kept at all (ADDENDUM O).

Everything here is a pure function of its inputs. ``now`` is always injected —
this layer may not read a clock — and nothing is ever silently deleted: a
gardening pass returns *proposals* and the caller decides.

The one clock-shaped helper, :func:`daily_log_line`, is formatting-only:
``time.strftime`` over an **injected** timestamp is a pure function of that
timestamp, which is why ``when`` is a parameter and is never read from the
system clock.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "CORE_MEMORY_MAX_CHARS",
    "DEFAULT_SCRATCH_TTL_SECONDS",
    "ENTRY_MAX_CHARS",
    "TEAM_MEMORY_MAX_CHARS",
    "TOPIC_MEMORY_MAX_CHARS",
    "cap_report",
    "daily_log_line",
    "entry_allowed",
    "gardening_proposals",
    "gc_expired_scratch",
    "gc_scratch",
    "memory_search_hits",
    "normalize_entry",
    "review_decision",
    "scratch_is_live",
]

#: Default scratch lifetime (spec: 2h).
DEFAULT_SCRATCH_TTL_SECONDS: float = 2 * 60 * 60.0

#: Caps from ADDENDUM O.
CORE_MEMORY_MAX_CHARS: int = 4000
TOPIC_MEMORY_MAX_CHARS: int = 16000
TEAM_MEMORY_MAX_CHARS: int = 16000
ENTRY_MAX_CHARS: int = 300

#: ``[source: <anchor>; added: YYYY-MM-DD; by: <bot>]``
_ENTRY_SUFFIX_RE = re.compile(
    r"\[source:[^\]]*;\s*added:\s*\d{4}-\d{2}-\d{2};\s*by:\s*[^\]]+\]\s*$"
)

#: Scratch entries are ``- <epoch> <text>`` bullets.
_SCRATCH_LINE_RE = re.compile(r"^-\s*(\d{9,}(?:\.\d+)?)\s+(.*)$")

#: Shapes that must never be persisted (ADDENDUM O). Deliberately conservative:
# a false positive costs the user one remembered line, a false negative writes a
# credential to disk in plaintext.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|bearer)\b"),
    re.compile(r"(?i)\b(?:password|passwd|pwd|pin)\b\s*[:=]"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b\d{17}[\dXx]\b"),  # CN national id
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # card-like digit runs
)

#: Provenances whose content is data, never instructions — never remembered.
_UNTRUSTED_SOURCES: frozenset[str] = frozenset({"web", "file", "tool", "fetch", "url"})


# ── scratch TTL ──────────────────────────────────────────────────────────────


def scratch_is_live(entry_ts: float, *, now: float, ttl_seconds: float) -> bool:
    """Whether one scratch entry is still inside its TTL.

    A non-positive TTL means "never expires" — a caller that wants no scratch
    layer simply sets the TTL to 0 rather than having entries silently vanish.

    Example:
        >>> scratch_is_live(100.0, now=150.0, ttl_seconds=100.0)
        True
        >>> scratch_is_live(100.0, now=300.0, ttl_seconds=100.0)
        False
    """
    if ttl_seconds <= 0:
        return True
    try:
        return (float(now) - float(entry_ts)) < float(ttl_seconds)
    except (TypeError, ValueError):
        return False


def _split_scratch(text: str) -> tuple[list[str], list[tuple[float, str, str]]]:
    """Split scratch text into (header/other lines, ``(ts, body, raw)`` entries).

    ``raw`` is the original line so a live entry can be re-emitted byte-for-byte
    — reformatting the timestamp would break the round-trip through
    :data:`_SCRATCH_LINE_RE` (``%g`` turns ``1757000000`` into ``1.757e+09``).
    """
    plain: list[str] = []
    entries: list[tuple[float, str, str]] = []
    for line in str(text or "").splitlines():
        match = _SCRATCH_LINE_RE.match(line)
        if match:
            try:
                entries.append((float(match.group(1)), match.group(2).strip(), line))
            except ValueError:
                plain.append(line)
        else:
            plain.append(line)
    return (plain, entries)


def gc_scratch(
    text: str,
    *,
    now: float,
    ttl_seconds: float = DEFAULT_SCRATCH_TTL_SECONDS,
) -> tuple[str, int]:
    """Drop expired scratch entries, keeping the file's prose structure.

    Only lines that parse as timed entries are ever removed; anything else (a
    heading, a blank line, a hand-written note) is preserved verbatim. Removing
    a live entry's line is not attempted — a live entry is rewritten with its
    original timestamp so the next GC round still judges it correctly.

    Args:
        text: current ``scratch.md`` content.
        now: injected current timestamp.
        ttl_seconds: entry lifetime.

    Returns:
        ``(new_text, removed_count)``.

    Example:
        >>> gc_scratch("- 1757000000 keep\\n- 1756000000 drop", now=1757000050.0,
        ...            ttl_seconds=100.0)[1]
        1
    """
    plain, entries = _split_scratch(text)
    kept: list[str] = []
    removed = 0
    for ts, _body, raw in entries:
        if scratch_is_live(ts, now=now, ttl_seconds=ttl_seconds):
            kept.append(raw)
        else:
            removed += 1
    out: list[str] = []
    emitted = False
    for line in plain:
        out.append(line)
        if line.strip() and not emitted and kept:
            out.extend(kept)
            emitted = True
    if kept and not emitted:
        out.extend(kept)
    return ("\n".join(out).strip() + "\n" if out else "", removed)


def gc_expired_scratch(text: str, *, now: float) -> tuple[str, int]:
    """Drop scratch entries whose **expiry** stamp has passed.

    :func:`gc_scratch` judges entries by *creation* time plus a TTL; this one
    judges them by an absolute expiry already written on the line. The server
    layer writes ``expires_at`` on each entry, which is what lets a restart
    still know an entry is dead without re-deriving when it was created.

    Lines that do not parse as timed entries are preserved verbatim.

    Args:
        text: current scratch content.
        now: injected current timestamp.

    Returns:
        ``(new_text, removed_count)``.

    Example:
        >>> gc_expired_scratch("- 1757000000 old\\n- 1758000000 new", now=1757500000.0)[1]
        1
    """
    plain, entries = _split_scratch(text)
    kept: list[str] = []
    removed = 0
    for expires, _body, raw in entries:
        try:
            stamp = float(now)
        except (TypeError, ValueError):
            stamp = 0.0
        if expires > stamp:
            kept.append(raw)
        else:
            removed += 1
    out: list[str] = []
    emitted = False
    for line in plain:
        out.append(line)
        if line.strip() and not emitted and kept:
            out.extend(kept)
            emitted = True
    if kept and not emitted:
        out.extend(kept)
    return ("\n".join(out).strip() + "\n" if out else "", removed)


# ── daily log ────────────────────────────────────────────────────────────────


def daily_log_line(*, when: float, what: str) -> str:
    """Render one dated "what was done" bullet for ``daily/YYYY-MM-DD.md``.

    Deterministic and UTC, so re-running a turn's end hook over the same
    timestamp produces the same line.

    Example:
        >>> daily_log_line(when=0.0, what="ran the migration")
        '- [1970-01-01T00:00:00Z] ran the migration'
    """
    import time

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(when)))
    return f"- [{stamp}] {str(what or '').strip()}"


# ── core memory gardening ────────────────────────────────────────────────────


def _lines_with_offsets(text: str) -> list[tuple[int, str]]:
    return [(index, line) for index, line in enumerate(str(text or "").splitlines())]


def gardening_proposals(
    text: str,
    *,
    max_chars: int = CORE_MEMORY_MAX_CHARS,
    keep_lines: int = 3,
    warn_ratio: float = 0.8,
) -> list[dict[str, Any]]:
    """Propose trims that bring *text* under *max_chars*.

    Proposals are computed from the START of the file forwards, because the last
    lines of a memory file are the most recently written and therefore the ones a
    user is least likely to still need injected. Each proposal names the exact
    line and the saving it produces.

    The pass runs at ``warn_ratio`` of the cap rather than only once the cap is
    already exceeded: a user who is told how to make room *before* the next save
    fails is far better served than one who is only told after the refusal.

    **Nothing is deleted.** The caller (a model or the user) decides; the
    ``kept`` field on every proposal is the original line, so a proposal can be
    applied by a blind line-equality filter.

    ``keep_lines`` protects the newest lines **only while older lines remain to
    propose**. A file with no more lines than the protected tail would otherwise
    never yield a single proposal, which makes the pass useless exactly when the
    file is too big for its cap.

    Args:
        text: current core memory content.
        max_chars: hard cap the file must never cross.
        keep_lines: the last N lines are preferred over older ones.
        warn_ratio: fraction of the cap at which proposals start and stop.

    Returns:
        ``[{"line_no", "text", "chars", "kept", "projected_chars"}]``; empty
        when the file is comfortably under the cap.

    Example:
        >>> gardening_proposals("x" * 50, max_chars=10, keep_lines=0)[0]["chars"]
        50
    """
    body = str(text or "")
    ratio = max(0.0, min(1.0, float(warn_ratio)))
    target = max_chars * ratio
    if max_chars <= 0 or len(body) <= target:
        return []
    rows = _lines_with_offsets(body)
    if not rows:
        return []
    keep_from = max(0, len(rows) - max(0, keep_lines))
    if keep_from == 0:
        keep_from = len(rows)
    projected = len(body)
    proposals: list[dict[str, Any]] = []
    for line_no, line in rows[:keep_from]:
        saving = len(line) + 1
        if projected <= target:
            break
        projected -= saving
        proposals.append(
            {
                "line_no": line_no,
                "text": line,
                "chars": len(line),
                "kept": line,
                "projected_chars": projected,
            }
        )
    return proposals


def cap_report(text: str, *, limit: int) -> dict[str, Any]:
    """Report how a memory file sits against its character cap.

    Example:
        >>> cap_report("abcd", limit=4)["over_cap"]
        False
    """
    body = str(text or "")
    return {
        "chars": len(body),
        "limit": int(limit),
        "over_cap": len(body) > int(limit),
        "over_by": max(0, len(body) - int(limit)),
        "pct_of_cap": round(len(body) / int(limit), 4) if limit else 0.0,
    }


# ── search ───────────────────────────────────────────────────────────────────


def _tokenize(query: str) -> list[str]:
    return [token for token in re.split(r"\W+", str(query or "").lower()) if token]


def memory_search_hits(
    query: str,
    files: Mapping[str, str],
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Grep-style search across memory files.

    Scoring is deliberately simple and deterministic: every query token found on
    a line contributes 1, a whole-phrase match contributes 3. Ties break on the
    file name then the line number, so repeated searches return the same order.

    Args:
        query: free text (a phrase, or several tokens).
        files: ``{filename: content}`` — including ``daily/*.md``.
        limit: maximum hits returned.

    Returns:
        ``[{"file", "line_no", "line", "score"}]``, best first.

    Example:
        >>> memory_search_hits("deploy", {"a.md": "deploy plan"})[0]["file"]
        'a.md'
    """
    tokens = _tokenize(query)
    if not tokens:
        return []
    phrase = " ".join(tokens)
    hits: list[dict[str, Any]] = []
    for name in sorted(files):
        for line_no, line in enumerate(str(files.get(name) or "").splitlines(), start=1):
            lowered = line.lower()
            score = sum(1 for token in tokens if token in lowered)
            if phrase and phrase in lowered:
                score += 3
            if score <= 0:
                continue
            hits.append({"file": name, "line_no": line_no, "line": line.strip(), "score": score})
    hits.sort(key=lambda hit: (-hit["score"], hit["file"], hit["line_no"]))
    return hits[: max(0, int(limit))]


# ── review rules (ADDENDUM O) ────────────────────────────────────────────────


def entry_allowed(text: str) -> tuple[bool, str]:
    """Whether one candidate memory entry may be persisted, and why not.

    Rejects anything that *looks* like a credential or an identity number, and
    anything whose stated source is untrusted (a web page, a file, a tool
    result) — content from outside the conversation is data, never an instruction
    to remember.

    Args:
        text: the candidate line (with or without the entry suffix).

    Returns:
        ``(allowed, reason)``; ``reason`` is ``""`` when allowed.

    Example:
        >>> entry_allowed("user prefers metric units")[0]
        True
        >>> entry_allowed("api_key: sk-abcdefghijklmnopqrstuvwx")[0]
        False
    """
    body = str(text or "")
    if not body.strip():
        return (False, "empty entry")
    for pattern in _SECRET_PATTERNS:
        if pattern.search(body):
            return (False, f"looks like a credential or identity number: {pattern.pattern}")
    lowered = body.lower()
    for source in _UNTRUSTED_SOURCES:
        if f"[source: {source}" in lowered or f"[source:{source}" in lowered:
            return (False, f"untrusted source '{source}': page/file/tool content is data")
    return (True, "")


def normalize_entry(
    text: str,
    *,
    source: str,
    added: str,
    bot_id: str,
    max_chars: int | None = ENTRY_MAX_CHARS,
) -> str:
    """Render one memory entry in the canonical one-line format.

    ``<text> [source: <anchor>; added: YYYY-MM-DD; by: <bot>]``

    The text is collapsed to a single line and truncated so that text plus
    suffix never exceeds *max_chars*. An entry that already carries a suffix is
    re-normalised rather than double-suffixed.

    ``max_chars=None`` disables truncation. The store's append path uses that:
    silently dropping the tail of a long user line is a deletion the user never
    asked for, so the store sizes the entry itself and refuses a save past the
    file cap with advice instead of quietly keeping a third of it.

    Args:
        text: the entry body.
        source: anchor recorded in the suffix.
        added: ``YYYY-MM-DD`` recorded in the suffix.
        bot_id: writer recorded in the suffix.
        max_chars: per-entry budget, or ``None`` for no truncation.

    Returns:
        The one-line entry.

    Example:
        >>> normalize_entry("likes tea", source="part 1 #a1", added="2026-10-11", bot_id="chef")
        'likes tea [source: part 1 #a1; added: 2026-10-11; by: chef]'
        >>> len(normalize_entry("y" * 900, source="p", added="d", bot_id="b"))
        300
        >>> normalize_entry("y" * 900, source="p", added="d", bot_id="b", max_chars=None)[:3]
        'yyy'
    """
    body = " ".join(str(text or "").split())
    body = _ENTRY_SUFFIX_RE.sub("", body).strip()
    suffix = f" [source: {source}; added: {added}; by: {bot_id}]"
    if max_chars is None:
        return f"{body}{suffix}"
    budget = max(1, int(max_chars) - len(suffix))
    if len(body) > budget:
        body = body[: max(1, budget - 1)] + "…"
    return f"{body}{suffix}"


def review_decision(text: str) -> dict[str, Any]:
    """Parse one memory-review model reply into a decision.

    Accepted shape (JSON or ``ACTION:`` lines)::

        {"action": "keep|update|remove", "entries": ["..."], "reason": "..."}

    An unparseable reply is **not** an error and **not** a silent keep: it
    resolves to ``keep`` with whatever single-line entries it contained, so a
    flaky model degrades to "do less" instead of "erase memory".

    Example:
        >>> review_decision('{"action": "remove", "entries": ["old fact"]}')["action"]
        'remove'
    """
    import json

    raw = str(text or "").strip()
    payload: dict[str, Any] = {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except (json.JSONDecodeError, ValueError):
            payload = {}
    if not payload:
        action = ""
        entries: list[str] = []
        for line in raw.splitlines():
            lowered = line.strip().lower()
            if lowered.startswith("action:"):
                action = lowered.split(":", 1)[1].strip()
            elif lowered.startswith("- "):
                entries.append(line.strip()[2:].strip())
        if action or entries:
            payload = {"action": action, "entries": entries}
    action = str(payload.get("action") or "keep").strip().lower()
    if action not in {"keep", "update", "remove"}:
        action = "keep"
    raw_entries = payload.get("entries")
    if isinstance(raw_entries, str):
        entries = [raw_entries.strip()]
    elif isinstance(raw_entries, Sequence):
        entries = [str(item).strip() for item in raw_entries if str(item).strip()]
    else:
        entries = []
    if not entries:
        entries = [line.strip() for line in raw.splitlines() if line.strip().startswith("- ")]
        entries = [entry[2:].strip() for entry in entries]
    allowed: list[str] = []
    refused: list[dict[str, str]] = []
    for entry in entries:
        ok, reason = entry_allowed(entry)
        (allowed if ok else refused).append(entry if ok else reason)
    return {
        "action": action,
        "entries": allowed,
        "refused": refused,
        "reason": str(payload.get("reason") or ""),
    }
