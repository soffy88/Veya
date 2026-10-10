"""server.goal_run.ai_attribution — per-line AI authorship ledger.

Ledger lives under .veya/ai-attribution/ (gitignored, never committed).
Each file gets one JSON ledger keyed by a sha256 prefix of the canonical path.

Recording: record_ai_write() is called by coding tools after file-write/edit.
Reverting: revert_to_human() removes ledger entries that overlap a line range
  (called when a human diff is detected on those lines).
Export: get_file_attribution() returns per-line ai|human for a given file;
        export_all() dumps the full ledger.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

_LEDGER_DIR = ".veya/ai-attribution"
_LOCK = threading.RLock()

LineAttribution = Literal["ai", "human"]


@dataclass
class AttributionEntry:
    line_start: int
    line_end: int
    session_id: str
    agent: str
    model: str
    timestamp: str
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttributionEntry:
        return cls(**data)

    def overlaps(self, line_start: int, line_end: int) -> bool:
        return not (self.line_end < line_start or self.line_start > line_end)

    def remove_overlap(
        self, line_start: int, line_end: int
    ) -> list[AttributionEntry]:
        """Return 0-2 non-overlapping sub-entries after removing [line_start, line_end]."""
        result = []
        if self.line_start < line_start:
            result.append(
                AttributionEntry(
                    line_start=self.line_start,
                    line_end=line_start - 1,
                    session_id=self.session_id,
                    agent=self.agent,
                    model=self.model,
                    timestamp=self.timestamp,
                    content_hash=self.content_hash,
                )
            )
        if self.line_end > line_end:
            result.append(
                AttributionEntry(
                    line_start=line_end + 1,
                    line_end=self.line_end,
                    session_id=self.session_id,
                    agent=self.agent,
                    model=self.model,
                    timestamp=self.timestamp,
                    content_hash=self.content_hash,
                )
            )
        return result


def _ledger_path(repo_root: str | Path, file_path: str) -> Path:
    key = hashlib.sha256(file_path.encode()).hexdigest()[:16]
    return Path(repo_root) / _LEDGER_DIR / f"{key}.json"


def _read_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"file": "", "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"file": "", "entries": []}
    except (json.JSONDecodeError, OSError):
        return {"file": "", "entries": []}


def _write_ledger(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]


def record_ai_write(
    repo_root: str | Path,
    file_path: str,
    line_start: int,
    line_end: int,
    *,
    session_id: str,
    agent: str = "MasterAgent",
    model: str = "",
    content: str = "",
) -> AttributionEntry:
    """Record AI authorship of a line range in a file."""
    entry = AttributionEntry(
        line_start=line_start,
        line_end=line_end,
        session_id=session_id,
        agent=agent,
        model=model,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        content_hash=_content_hash(content) if content else "",
    )
    lpath = _ledger_path(repo_root, file_path)
    with _LOCK:
        data = _read_ledger(lpath)
        data["file"] = file_path
        entries = [AttributionEntry.from_dict(e) for e in data.get("entries", [])]
        # Remove any overlapping existing entries for this range, then add new
        surviving: list[AttributionEntry] = []
        for existing in entries:
            if existing.overlaps(line_start, line_end):
                surviving.extend(existing.remove_overlap(line_start, line_end))
            else:
                surviving.append(existing)
        surviving.append(entry)
        data["entries"] = [e.to_dict() for e in surviving]
        _write_ledger(lpath, data)
    return entry


def revert_to_human(
    repo_root: str | Path,
    file_path: str,
    line_start: int,
    line_end: int,
) -> int:
    """Remove AI attribution for a line range (human edited those lines).

    Returns the number of entries affected (split or removed).
    """
    lpath = _ledger_path(repo_root, file_path)
    with _LOCK:
        data = _read_ledger(lpath)
        entries = [AttributionEntry.from_dict(e) for e in data.get("entries", [])]
        surviving: list[AttributionEntry] = []
        affected = 0
        for entry in entries:
            if entry.overlaps(line_start, line_end):
                surviving.extend(entry.remove_overlap(line_start, line_end))
                affected += 1
            else:
                surviving.append(entry)
        data["entries"] = [e.to_dict() for e in surviving]
        if affected:
            _write_ledger(lpath, data)
    return affected


def get_file_attribution(
    repo_root: str | Path,
    file_path: str,
    total_lines: int,
) -> list[dict[str, Any]]:
    """Return per-line attribution for a file.

    Returns a list of {line, attribution: 'ai'|'human', session_id?, model?}
    for lines 1..total_lines.
    """
    lpath = _ledger_path(repo_root, file_path)
    with _LOCK:
        data = _read_ledger(lpath)
    entries = [AttributionEntry.from_dict(e) for e in data.get("entries", [])]

    result: list[dict[str, Any]] = []
    for line in range(1, total_lines + 1):
        matched = next((e for e in entries if e.line_start <= line <= e.line_end), None)
        if matched:
            result.append(
                {
                    "line": line,
                    "attribution": "ai",
                    "session_id": matched.session_id,
                    "model": matched.model,
                }
            )
        else:
            result.append({"line": line, "attribution": "human"})
    return result


def export_all(repo_root: str | Path) -> list[dict[str, Any]]:
    """Dump the full attribution ledger (all files)."""
    ledger_dir = Path(repo_root) / _LEDGER_DIR
    if not ledger_dir.exists():
        return []
    result = []
    for path in sorted(ledger_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result.append(data)
        except (json.JSONDecodeError, OSError):
            pass
    return result
