"""Deployed-commit reader for the D4 verify probe (event-hub design D4).

The commit file is written by the deploy step (docker cp) next to the
deployed code: it names the exact commit the running tree came from.
Precedence: commit file > VEYA_BUILD_SHA env > unknown. A missing or
malformed file reports unknown -- never a guessed commit -- and the
verify probe treats unknown as unverifiable, not as a match.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def commit_file_path() -> str:
    return os.environ.get("VEYA_COMMIT_FILE", "/app/.veya-commit.json")


def read_deployed_commit() -> dict[str, Any]:
    """Return {commit, version?, source, written_at?}. commit may be None."""

    path = Path(commit_file_path())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        commit = str(data.get("commit") or "").strip().lower()
        if _COMMIT_RE.match(commit):
            return {
                "commit": commit,
                "source": "commit-file",
                "written_at": str(data.get("written_at") or ""),
            }
    env_sha = os.environ.get("VEYA_BUILD_SHA", "").strip().lower()
    if _COMMIT_RE.match(env_sha):
        return {"commit": env_sha, "source": "env", "written_at": ""}
    return {"commit": None, "source": "unknown", "written_at": ""}
