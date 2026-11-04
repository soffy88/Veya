"""Kernel purity: architecture boundaries enforced by tests.

Autoloop graft, design item 4 (see ~/workspace/veya-autoloop-graft-design.md).
Adapted, not copied: veya's decision modules legitimately read the
environment and the clock, so there is no "time/entropy/files only through
the shell" rule here.  What transfers is: the decision core makes no
outbound request, names no vendor, and only each store module writes its
own tables.  Loosening any rule below needs Soffy's say-so, not a quiet
edit to this file.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Decision core: full rules apply.  attention.py carries the standing/tell
# gate inside a larger store module, so only its import section is checked.
DECISION_CORE = [
    "server/bot_team/sentinel.py",
    "server/decision_ledger.py",
]
IMPORT_ONLY = [
    "server/bot_team/attention.py",
]

_BANNED_IMPORTS = frozenset(
    {
        "httpx",
        "requests",
        "aiohttp",
        "websocket",
        "websockets",
        "socket",
        "smtplib",
        "ftplib",
        "urllib3",
        "http.client",
        "urllib.request",
    }
)

_VENDOR_RE = re.compile(
    r"\b(claude|anthropic|codex|openai|gemini|cursor|hermes|openclaw|feishu|lark|dingtalk|telegram|slack|github|gitlab)\b",
    re.IGNORECASE,
)

# table -> modules allowed to issue INSERT/UPDATE/DELETE against it.
_WRITE_OWNERS = {
    "decisions": {"server/decision_ledger.py"},
    "standing": {"server/decision_ledger.py"},
    "approvals": {"veya/obase/approvals.py"},
    "grants": {"veya/obase/approvals.py"},
    "handoffs": {"veya/obase/approvals.py"},
}
_WRITE_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+[\"'`\[]?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# Files allowed to mention the sentinel grants file by name.
_GRANTS_FILE_OWNERS = {"server/bot_team/sentinel.py"}


def _blank_strings_and_comments(text: str) -> str:
    """Return text with string literals and comments replaced by spaces.

    Vendor names in data (the egress allowlist's "github.com") and in
    comments are not "naming a vendor in decision logic"; only code counts.
    """
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    chars = list(text)
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return text
    for tok in tokens:
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            (srow, scol), (erow, ecol) = tok.start, tok.end
            start = offsets[srow - 1] + scol
            end = offsets[erow - 1] + ecol
            for i in range(start, min(end, len(chars))):
                if chars[i] != "\n":
                    chars[i] = " "
    return "".join(chars)


def _imported_modules(code: str) -> set[str]:
    """Module names from import statements in blanked code."""
    modules: set[str] = set()
    for line in code.splitlines():
        match = re.match(r"\s*import\s+([\w\.]+)", line)
        if match:
            modules.add(match.group(1))
            continue
        match = re.match(r"\s*from\s+([\w\.]+)\s+import\s+(.+)", line)
        if match:
            modules.add(match.group(1))
            for name in match.group(2).split(","):
                name = name.strip().split(" as ")[0].strip()
                if name and name != "*":
                    modules.add(f"{match.group(1)}.{name}")
    return modules


def _check_no_outbound(path: Path) -> None:
    code = _blank_strings_and_comments(path.read_text(encoding="utf-8"))
    imported = _imported_modules(code)
    offenders = sorted(m for m in imported if m in _BANNED_IMPORTS)
    assert not offenders, f"{path}: outbound imports {offenders}"
    dynamic = re.findall(r"__import__\s*\(\s*[\"']([^\"']+)[\"']", code)
    bad_dynamic = [m for m in dynamic if m in _BANNED_IMPORTS]
    assert not bad_dynamic, f"{path}: dynamic outbound imports {bad_dynamic}"


def _check_no_vendor(path: Path) -> None:
    code = _blank_strings_and_comments(path.read_text(encoding="utf-8"))
    for i, line in enumerate(code.splitlines(), 1):
        match = _VENDOR_RE.search(line)
        assert not match, f"{path}:{i}: vendor name {match.group(0)!r}: {line.strip()}"


def test_decision_core_makes_no_outbound_request():
    for rel in DECISION_CORE + IMPORT_ONLY:
        _check_no_outbound(ROOT / rel)


def test_decision_core_names_no_vendor():
    for rel in DECISION_CORE:
        _check_no_vendor(ROOT / rel)


def _python_files(*roots: str):
    for root in roots:
        base = ROOT / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def test_only_owning_store_writes_each_table():
    violations: list[str] = []
    for path in _python_files("server", "veya"):
        rel = str(path.relative_to(ROOT))
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _WRITE_RE.finditer(text):
            table = match.group(1).lower()
            if table in _WRITE_OWNERS and rel not in _WRITE_OWNERS[table]:
                violations.append(f"{rel}: writes table {table!r}")
    assert not violations, violations


def test_sentinel_grants_file_has_one_writer():
    referrers = set()
    for path in _python_files("server", "veya"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "sentinel_grants" in text:
            referrers.add(str(path.relative_to(ROOT)))
    assert referrers <= _GRANTS_FILE_OWNERS, referrers


def test_event_log_is_append_only():
    events_path = ROOT / "server" / "events.py"
    text = events_path.read_text(encoding="utf-8")
    assert ".truncate(" not in text
    write_opens = re.findall(r"\.open\(\s*[\"']([wa][\+]?)[\"']", text)
    assert write_opens and all(mode.startswith("a") for mode in write_opens), write_opens
    for path in _python_files("server"):
        rel = str(path.relative_to(ROOT))
        if rel == "server/events.py":
            continue
        for i, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            lowered = line.lower()
            if ("truncate" in lowered or "unlink" in lowered or "os.remove" in lowered) and (
                "events.jsonl" in lowered or "event_store" in lowered
            ):
                pytest.fail(f"{rel}:{i}: destructive call touching the event log: {line.strip()}")
