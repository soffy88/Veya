"""Append-only audit trail with secret redaction (spec §1/§9).

Every tool call produces exactly one record. Mutation and destructive calls are
recorded before execution is attempted (status ``requested``) and amended with
the outcome, so a crash mid-execution still leaves evidence that the action was
authorized and started.

Direct file I/O is the audit sink's job and is documented via ``# 3O-IO-ALLOW``.
"""

# 3O-IO-ALLOW: append-only audit sink for remote tool calls (VEYA_REMOTE_AUDIT_LOG).

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import AuditRecord, RemoteErrorCode

_SECRET_KEY = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|authorization|credential|bearer|private[_-]?key)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
# Credential-shaped opaque tokens only. Deliberately excludes hyphenated /
# underscore identifiers (worktree task ids, branches, enum names) and pure
# lowercase hex (git SHAs) so legitimate tool output is not destroyed.
_LONG_TOKEN = re.compile(
    r"\b(?:"
    r"sk-[A-Za-z0-9_\-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|AIza[0-9A-Za-z_\-]{35}"
    r"|(?=[A-Za-z0-9+/]*[A-Z+/=])[A-Za-z0-9+/]{44,}={0,2}"
    r")\b"
)
_MASK = "***REDACTED***"


class RemoteAudit:
    """Thread-safe, append-only audit log."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_memory: int = 2000,
        secret_values: Iterable[str] = (),
    ) -> None:
        self._path = Path(path).expanduser() if path else None
        self._memory: deque[AuditRecord] = deque(maxlen=max_memory)
        self._secret_values = tuple(s for s in secret_values if s)
        self._lock = threading.Lock()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path | None:
        return self._path

    # ── redaction ───────────────────────────────────────────────────
    def redact(self, value: Any, *, key: str | None = None) -> Any:
        if key is not None and _SECRET_KEY.search(key):
            return _MASK
        if isinstance(value, dict):
            return {str(k): self.redact(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            return self._redact_text(value)
        return value

    def _redact_text(self, text: str) -> str:
        out = _BEARER.sub(f"Bearer {_MASK}", text)
        for secret in self._secret_values:
            if secret:
                out = out.replace(secret, _MASK)
        # A single opaque 32+ char run is almost always a credential.
        out = _LONG_TOKEN.sub(_MASK, out)
        return out

    # ── recording ───────────────────────────────────────────────────
    def record(
        self,
        *,
        tool: str,
        status: str,
        session_id: str | None = None,
        principal: str | None = None,
        token_id: str | None = None,
        veya_tool: str | None = None,
        effect: str | None = None,
        workspace: str | None = None,
        args: dict[str, Any] | None = None,
        error_code: RemoteErrorCode | str | None = None,
        duration_ms: float | None = None,
        execution_id: str | None = None,
        remote_addr: str | None = None,
    ) -> AuditRecord:
        record = AuditRecord(
            ts=time.time(),
            session_id=session_id,
            principal=principal,
            token_id=token_id,
            tool=tool,
            veya_tool=veya_tool,
            effect=effect,
            workspace=workspace,
            args=self.redact(args or {}),
            status=status,
            error_code=str(error_code) if error_code else None,
            duration_ms=duration_ms,
            execution_id=execution_id,
            remote_addr=remote_addr,
        )
        with self._lock:
            self._memory.append(record)
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        return record

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._memory]

    def __len__(self) -> int:
        with self._lock:
            return len(self._memory)
