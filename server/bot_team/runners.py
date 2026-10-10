"""Runner registry: the machines a Veya bot may run on (Lorca P6).

A *runner* is another machine on the tailnet that also runs a Veya server. A
bot whose ``runner_id`` names one of them executes its turns there, while this
server (the hub) keeps the bot catalog, the conversations and the group
transcripts. ``"local"`` is implicit and never stored: it means this server.

What is persisted is a **reference** to the runner's secret — an environment
variable name or a file path — never the secret itself. ``resolve_token_ref``
is the only function that turns a ref into material, and nothing in this
package writes the result to a file, a response body or a log line.

No authority lives here: this is a catalog plus a health projection, exactly
like ``BotRegistry`` (identity only). Execution stays with the runner's own
GoalRun.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json

#: The runner that is this server. Implicit: there is no record for it.
LOCAL_RUNNER_ID = "local"

#: Health states. ``local`` is only used by the implicit runner view.
STATUS_LOCAL = "local"
STATUS_UNKNOWN = "unknown"
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"

KNOWN_STATUSES = (STATUS_LOCAL, STATUS_UNKNOWN, STATUS_ONLINE, STATUS_OFFLINE)

#: A runner id names a machine, so it is restricted like a bot id.
_RUNNER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: A token ref is either an environment variable name …
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_RUNNERS_FILE = "runners.json"


class RunnerError(RuntimeError):
    """A runner request was refused (bad id/url/token ref, or not registered)."""


def default_runners_path() -> Path:
    """Where runners are persisted (``VEYA_RUNNERS_PATH`` overrides)."""

    raw = os.environ.get("VEYA_RUNNERS_PATH") or str(Path.home() / ".veya" / "runners.json")
    return Path(raw).expanduser()


def _normalise_base_url(base_url: str) -> str:
    """Validate and normalise a runner's base URL (tailnet address)."""

    value = str(base_url or "").strip()
    if not value:
        raise RunnerError("base_url is required")
    if any(char.isspace() for char in value):
        raise RunnerError("base_url must not contain whitespace")
    if not (value.startswith("http://") or value.startswith("https://")):
        raise RunnerError("base_url must start with http:// or https://")
    return value.rstrip("/")


def is_token_ref(token_ref: str) -> bool:
    """Whether ``token_ref`` is an acceptable *reference* to a secret.

    It must name an environment variable or look like a path. This is what
    keeps a raw token from being persisted: a 40-character blob with no
    separator is refused here rather than written to disk.
    """

    value = str(token_ref or "").strip()
    if not value or len(value) > 512 or any(char.isspace() for char in value):
        return False
    if _ENV_NAME_RE.match(value):
        return True
    return "/" in value or value.startswith("~")


def token_ref_label(token_ref: str) -> str:
    """A display form of a token ref that can never echo a raw secret.

    Error text uses this instead of the ref itself, so a misconfigured ref
    cannot leak the very secret it was meant to point at.
    """

    value = str(token_ref or "").strip()
    if not value:
        return "<none>"
    if _ENV_NAME_RE.match(value) or is_token_ref(value):
        return value
    return "<redacted>"


def resolve_token_ref(token_ref: str, *, environ: Mapping[str, str] | None = None) -> str:
    """Resolve a token ref to the secret it points at.

    An environment variable wins over a file with the same name. Anything that
    resolves to nothing returns ``""`` — the caller then refuses the turn
    instead of sending an empty bearer header.
    """

    ref = str(token_ref or "").strip()
    if not ref:
        return ""
    env: Mapping[str, str] = os.environ if environ is None else environ
    from_env = str(env.get(ref) or "").strip()
    if from_env:
        return from_env
    try:
        candidate = Path(ref).expanduser()
        if not candidate.is_file():
            return ""
        # A secret file may hold one line; take the first non-empty one.
        for line in candidate.read_text(encoding="utf-8").splitlines():
            if line.strip():
                return line.strip()
    except OSError:
        return ""
    return ""


@dataclass(frozen=True)
class RunnerRecord:
    """One registered runner. ``token_ref`` is a ref, never a secret."""

    runner_id: str
    name: str
    base_url: str
    token_ref: str
    last_seen: float | None = None
    status: str = STATUS_UNKNOWN

    @property
    def is_local(self) -> bool:
        return self.runner_id == LOCAL_RUNNER_ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "runner_id": self.runner_id,
            "name": self.name,
            "base_url": self.base_url,
            "token_ref": self.token_ref,
            "last_seen": self.last_seen,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunnerRecord:
        status = str(value.get("status") or STATUS_UNKNOWN)
        return cls(
            runner_id=str(value.get("runner_id") or ""),
            name=str(value.get("name") or ""),
            base_url=str(value.get("base_url") or ""),
            token_ref=str(value.get("token_ref") or ""),
            last_seen=(float(value["last_seen"]) if value.get("last_seen") else None),
            status=status if status in KNOWN_STATUSES else STATUS_UNKNOWN,
        )

    def to_view(self) -> dict[str, Any]:
        """The response form: the ref, never a secret."""

        return {
            "runner_id": self.runner_id,
            "name": self.name or self.runner_id,
            "base_url": self.base_url,
            "token_ref": self.token_ref,
            "last_seen": self.last_seen,
            "status": self.status,
        }


def local_runner_view() -> dict[str, Any]:
    """The implicit ``local`` runner: this server, no record, no secret."""

    return {
        "runner_id": LOCAL_RUNNER_ID,
        "name": "this server",
        "base_url": "",
        "token_ref": "",
        "last_seen": None,
        "status": STATUS_LOCAL,
        "implicit": True,
    }


class RunnerRegistry:
    """Runners, persisted to one JSON file (same pattern as ``BotRegistry``).

    Every instance re-reads the file on construction, so a process never acts
    on a stale catalog; writes are atomic and a corrupt file degrades to "no
    runners" instead of breaking the hub.
    """

    def __init__(self, storage_path: str | Path | None = None):
        self.storage_path = (
            Path(storage_path) if storage_path is not None else default_runners_path()
        ).expanduser()
        self._items: dict[str, RunnerRecord] = {}
        self._load()

    # ── persistence ───────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for runner_id, record in data.items():
            if not isinstance(record, dict):
                continue
            item = RunnerRecord.from_dict(
                {**record, "runner_id": record.get("runner_id") or runner_id}
            )
            if item.runner_id:
                self._items[item.runner_id] = item

    def _save(self) -> None:
        atomic_write_json(
            self.storage_path, {rid: record.to_dict() for rid, record in self._items.items()}
        )

    # ── reads ─────────────────────────────────────────────────────────
    def get(self, runner_id: str) -> RunnerRecord | None:
        return self._items.get(str(runner_id or ""))

    def list(self) -> list[RunnerRecord]:
        return list(self._items.values())

    def views(self) -> list[dict[str, Any]]:
        """Every runner, the implicit ``local`` one first."""

        return [local_runner_view()] + [record.to_view() for record in self.list()]

    def require(self, runner_id: str) -> RunnerRecord:
        """One runner, or a refusal naming what is missing."""

        value = str(runner_id or "").strip()
        if not value:
            raise RunnerError("runner_id is required")
        if value == LOCAL_RUNNER_ID:
            raise RunnerError("the local runner is this server and has no record")
        record = self.get(value)
        if record is None:
            raise RunnerError(f"runner {value!r} is not registered")
        return record

    # ── writes ────────────────────────────────────────────────────────
    def add(
        self,
        *,
        runner_id: str,
        name: str,
        base_url: str,
        token_ref: str,
    ) -> RunnerRecord:
        """Register one runner. Refused, never repaired, on a bad field."""

        value = str(runner_id or "").strip()
        if not _RUNNER_ID_RE.match(value):
            raise RunnerError(
                "runner_id must be lowercase (letters, digits, '.', '_', '-') and start with a"
                " letter or digit"
            )
        if value == LOCAL_RUNNER_ID:
            raise RunnerError(
                f"{LOCAL_RUNNER_ID!r} is the implicit local runner and cannot be added"
            )
        if value in self._items:
            raise RunnerError(f"runner {value!r} already exists")
        url = _normalise_base_url(base_url)
        ref = str(token_ref or "").strip()
        if not is_token_ref(ref):
            raise RunnerError(
                "token_ref must be an environment variable name or a file path that holds the"
                " runner's secret"
            )
        record = RunnerRecord(
            runner_id=value,
            name=str(name or "").strip() or value,
            base_url=url,
            token_ref=ref,
        )
        self._items[value] = record
        self._save()
        return record

    def remove(self, runner_id: str) -> bool:
        value = str(runner_id or "").strip()
        if value == LOCAL_RUNNER_ID or value not in self._items:
            return False
        del self._items[value]
        self._save()
        return True

    def record_health(
        self, runner_id: str, *, status: str, seen_at: float | None = None
    ) -> RunnerRecord | None:
        """Store the outcome of one health check.

        A runner that does not answer keeps its record: forgetting it would
        silently send its bots back to ``local``.
        """

        value = str(runner_id or "")
        record = self._items.get(value)
        if record is None:
            return None
        updated = RunnerRecord(
            runner_id=record.runner_id,
            name=record.name,
            base_url=record.base_url,
            token_ref=record.token_ref,
            last_seen=float(seen_at if seen_at is not None else time.time()),
            status=status if status in KNOWN_STATUSES else STATUS_UNKNOWN,
        )
        self._items[value] = updated
        self._save()
        return updated


def runner_registry(storage_path: str | Path | None = None) -> RunnerRegistry:
    """Registry bound to ``storage_path`` or the process default path."""

    return RunnerRegistry(storage_path)


__all__ = [
    "KNOWN_STATUSES",
    "LOCAL_RUNNER_ID",
    "STATUS_LOCAL",
    "STATUS_OFFLINE",
    "STATUS_ONLINE",
    "STATUS_UNKNOWN",
    "RunnerError",
    "RunnerRecord",
    "RunnerRegistry",
    "default_runners_path",
    "is_token_ref",
    "local_runner_view",
    "resolve_token_ref",
    "runner_registry",
    "token_ref_label",
]
