"""Deterministic gate for memory writes (Codync ``codync-mem`` method, adapted).

Every durable memory write passes one pure check before anything reaches
disk. The check is deterministic on purpose: no model call, no heuristics,
no allowlist that a pasted prompt can talk its way around. A write that
fails the gate fails whole -- the file is left exactly as it was -- and the
refusal names the rule that fired so the writer can rephrase instead of
guessing.

Rules (audited, narrow on purpose):

* ``hidden_chars`` -- invisible formatting characters that change what a
  reader (human or model) sees: zero-width spaces/joiners, the byte-order
  mark, and Unicode bidi controls.
* ``unsafe_link`` -- links that cannot be what they claim: non-http(s)
  schemes (``javascript:``, ``data:``, ``file:``) and URLs carrying
  instruction-like query/fragment tokens.
* ``injected_instruction`` -- imperative phrases that try to steer the
  agent itself (ignore/disregard/override prior instructions, "you are
  now ...", reveal/repeat the system prompt). Quoted discussion of such
  phrases is collateral: rephrase the memory line instead.
* ``secret_like`` -- credential material that must never reach a file:
  private-key blocks and well-known token prefixes (``sk-``, GitHub,
  AWS access keys, Slack tokens). This is the repo's own standing rule --
  passwords and keys never enter chat or files -- enforced at the write.

The gate only ever *refuses*; it never rewrites, redacts, or truncates a
write. ``check_memory_text`` is the pure scan; ``refuse_memory_text`` is
the choke-point helper that raises :class:`ValueError` so both the
in-process ``remember()`` path and the HTTP write path (which already
maps ``ValueError`` to 422 / ``ok: false``) refuse uniformly.
"""

from __future__ import annotations

import re
from typing import Any

#: Invisible characters that alter rendering without showing up.
_HIDDEN_CHARS = frozenset(
    [
        "\u200b",  # zero width space
        "\u200c",  # zero width non-joiner
        "\u200d",  # zero width joiner
        "\ufeff",  # byte order mark / zero width no-break space
        "\u2060",  # word joiner
        "\u202a",  # bidi embedding
        "\u202b",
        "\u202c",  # pop directional formatting
        "\u202d",
        "\u202e",  # bidi overrides
        "\u2066",  # bidi isolates
        "\u2067",
        "\u2068",
        "\u2069",
        "\u00ad",  # soft hyphen
    ]
)

#: Schemes a memory link must never use.
_BAD_SCHEMES = ("javascript:", "data:", "file:", "vbscript:")

#: Tokens that make a URL instruction-carrying rather than a reference.
_URL_INSTRUCTION_TOKENS = (
    "ignore",
    "disregard",
    "override",
    "jailbreak",
    "system-prompt",
    "system_prompt",
    "prompt-injection",
)

#: Imperative phrases aimed at steering the agent. Word-boundary anchored
#: and deliberately short: the gate fires on the phrase itself, not on
#: words that merely contain it.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bignore\s+(all\s+|your\s+|the\s+|previous\s+)*(instructions|rules|prompt|directives)\b",
        r"\bdisregard\s+(all\s+|your\s+|the\s+|previous\s+)*(instructions|rules|prompt|directives)\b",
        r"\boverride\s+(your\s+|the\s+)*(instructions|rules|directives)\b",
        r"\byou\s+are\s+now\s+(a|an|the)\b",
        r"\breveal\s+(your\s+|the\s+)*(system|hidden)\s+(prompt|instructions)\b",
        r"\brepeat\s+(your\s+|the\s+)*(system|hidden)\s+(prompt|instructions)\b",
    )
)

#: Credential-shaped material. Prefixes only -- the gate does not need (or
#: want) the full value to recognise it.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
        r"\bsk-[A-Za-z0-9]{16,}\b",
        r"\bghp_[A-Za-z0-9]{20,}\b",
        r"\bgho_[A-Za-z0-9]{20,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
    )
)

_URL_RE = re.compile(r"https?://[^\s)\"'<>]+", re.IGNORECASE)
_SCHEME_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*):", re.IGNORECASE)


def check_memory_text(text: str) -> list[dict[str, Any]]:
    """Scan *text* and return every gate finding, in rule order.

    An empty list means the text passes. The scan is pure: it reads
    nothing, writes nothing, and never raises on any input.
    """

    findings: list[dict[str, Any]] = []
    body = str(text or "")

    hidden = sorted({ch for ch in body if ch in _HIDDEN_CHARS})
    if hidden:
        findings.append(
            {
                "rule": "hidden_chars",
                "detail": "invisible formatting characters: "
                + ", ".join(f"U+{ord(ch):04X}" for ch in hidden),
            }
        )

    for match in _URL_RE.finditer(body):
        url = match.group(0)
        lowered = url.lower()
        if any(token in lowered for token in _URL_INSTRUCTION_TOKENS):
            findings.append(
                {
                    "rule": "unsafe_link",
                    "detail": "URL carries an instruction-like token",
                }
            )
            break
    else:
        for match in _SCHEME_RE.finditer(body):
            scheme = match.group(1).lower() + ":"
            if scheme in _BAD_SCHEMES:
                findings.append(
                    {
                        "rule": "unsafe_link",
                        "detail": f"link scheme {scheme!r} is not a web reference",
                    }
                )
                break

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(body):
            findings.append(
                {
                    "rule": "injected_instruction",
                    "detail": "imperative phrase steering the agent",
                }
            )
            break

    for pattern in _SECRET_PATTERNS:
        if pattern.search(body):
            findings.append(
                {
                    "rule": "secret_like",
                    "detail": "credential-shaped material must never be written to memory",
                }
            )
            break

    return findings


def refuse_memory_text(text: str) -> None:
    """Raise :class:`ValueError` naming the fired rules, or return silently."""

    findings = check_memory_text(text)
    if not findings:
        return
    rules = ", ".join(sorted({finding["rule"] for finding in findings}))
    raise ValueError(
        f"memory write refused by the memory gate ({rules}); "
        "remove the flagged content and rephrase the line"
    )


__all__ = ["check_memory_text", "refuse_memory_text"]
