"""Per-bot Markdown memory store (P3 — Lorca-style readable/editable memory).

Each bot keeps its own memory as plain Markdown files that the user can read,
edit, and delete directly on the bot's runner machine.

Layout::

    <bots_root>/<bot_id>/memory/
        MEMORY.md         # core notes (always present after first write)
        <topic>.md        # optional topic files

``bots_root`` defaults to ``~/.veya/bots`` and can be overridden with the
``VEYA_BOTS_ROOT`` environment variable (same root as the bot team feature).

Guard rails:

* **Bot isolation**: :func:`runtime.bot_scope.require_same_bot` is called by
  callers that know the running bot; the store itself validates ``bot_id`` on
  construction but does not do cross-call identity tracking.
* **Path traversal**: only base-name, ``.md``-suffixed, no ``..`` or ``/``
  separators.
* **Non-.md names**: rejected with :class:`ValueError`.
* **Size caps**: single file ≤ ``MAX_FILE_BYTES`` (512 KiB), total store ≤
  ``MAX_TOTAL_BYTES`` (2 MiB).
* **Atomic writes**: ``os.replace`` so a partial write never corrupts the file.

Feature flag: ``VEYA_BOT_MEMORY_MD`` (defaults to ``"0"`` in production;
tests set it to ``"1"`` explicitly where needed).
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import time
from pathlib import Path

from runtime.bot_scope import require_same_bot

# ── Constants ────────────────────────────────────────────────────────────────

CORE_FILE = "MEMORY.md"
"""The canonical core memory file that every bot gets."""

MAX_FILE_BYTES: int = 512 * 1024  # 512 KiB per file
MAX_TOTAL_BYTES: int = 2 * 1024 * 1024  # 2 MiB total per bot store
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+\.md$")


def _default_bots_root() -> Path:
    root = os.environ.get("VEYA_BOTS_ROOT", "")
    if root:
        return Path(root).expanduser()
    return Path.home() / ".veya" / "bots"


def _validate_name(name: str) -> None:
    """Raise ValueError for names that are unsafe or not *.md."""
    if not name.endswith(".md"):
        raise ValueError(f"memory file name must end with .md: {name!r}")
    if not _VALID_NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid memory file name {name!r}: only [A-Za-z0-9_-].md allowed, "
            "no path separators or dots inside the stem"
        )


# ── Store ────────────────────────────────────────────────────────────────────


class BotMemoryStore:
    """Readable, editable per-bot Markdown memory store.

    Parameters
    ----------
    bot_id:
        The bot whose memory this store manages.  Must be a non-empty string.
    bots_root:
        Override the base directory (``~/.veya/bots`` by default or
        ``$VEYA_BOTS_ROOT``).  Primarily for testing.
    """

    def __init__(
        self,
        bot_id: str,
        *,
        bots_root: str | Path | None = None,
    ) -> None:
        if not bot_id:
            raise ValueError("bot_id must be a non-empty string")
        self.bot_id = bot_id
        root = Path(bots_root).expanduser() if bots_root is not None else _default_bots_root()
        self.memory_dir: Path = root / bot_id / "memory"

    # ── Private helpers ───────────────────────────────────────────────────

    def _ensure_dir(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        """Return the absolute path for a validated file name."""
        _validate_name(name)
        # Extra safety: confirm the resolved path is inside memory_dir.
        candidate = (self.memory_dir / name).resolve()
        try:
            candidate.relative_to(self.memory_dir.resolve())
        except ValueError:
            raise ValueError(f"path traversal rejected for name {name!r}") from None
        return self.memory_dir / name  # unresolved is fine; dir is trusted

    def _total_bytes(self) -> int:
        if not self.memory_dir.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.memory_dir.glob("*.md") if p.is_file())

    # ── Public API ────────────────────────────────────────────────────────

    def list_files(self) -> list[str]:
        """Return sorted list of ``*.md`` file names in the memory directory."""
        if not self.memory_dir.is_dir():
            return []
        return sorted(p.name for p in self.memory_dir.glob("*.md") if p.is_file())

    def read(self, name: str) -> str:
        """Return the content of *name*, or empty string if it does not exist."""
        path = self._path(name)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def write(self, name: str, text: str, *, caller_bot_id: str | None = None) -> None:
        """Atomically write *text* to *name*.

        Parameters
        ----------
        name:
            File name (validated: ``*.md``, no traversal).
        text:
            New file content.
        caller_bot_id:
            When provided, :func:`require_same_bot` is called to enforce that
            the caller belongs to this store's bot.
        """
        if caller_bot_id is not None:
            require_same_bot(caller_bot_id, self.bot_id, f"memory:{name}")
        path = self._path(name)
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ValueError(
                f"file {name!r} exceeds size cap of {MAX_FILE_BYTES // 1024} KiB"
            )
        self._ensure_dir()
        # Check total *after* accounting for the existing file being replaced.
        existing_size = path.stat().st_size if path.exists() else 0
        prospective_total = self._total_bytes() - existing_size + len(encoded)
        if prospective_total > MAX_TOTAL_BYTES:
            raise ValueError(
                f"store for bot {self.bot_id!r} would exceed total cap of "
                f"{MAX_TOTAL_BYTES // 1024} KiB"
            )
        # Atomic replace: write to a sibling temp file, then os.replace.
        fd, tmp = tempfile.mkstemp(dir=self.memory_dir, prefix=".tmp_", suffix=".md")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(encoded)
            os.replace(tmp, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def delete(self, name: str, *, caller_bot_id: str | None = None) -> bool:
        """Delete *name*.  Returns ``True`` if deleted, ``False`` if absent.

        Parameters
        ----------
        caller_bot_id:
            When provided, :func:`require_same_bot` is enforced before deletion.
        """
        if caller_bot_id is not None:
            require_same_bot(caller_bot_id, self.bot_id, f"memory:{name}")
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def append_note(self, text: str, *, caller_bot_id: str | None = None) -> None:
        """Append *text* as a timestamped note to :data:`CORE_FILE`.

        A Markdown horizontal rule and ISO-8601 timestamp header are prepended
        so successive notes remain human-readable.

        Parameters
        ----------
        caller_bot_id:
            When provided, :func:`require_same_bot` is enforced.
        """
        if caller_bot_id is not None:
            require_same_bot(caller_bot_id, self.bot_id, f"memory:{CORE_FILE}")
        existing = self.read(CORE_FILE)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        separator = "\n\n---\n\n" if existing else ""
        new_content = f"{existing}{separator}## Note — {ts}\n\n{text.rstrip()}\n"
        self.write(CORE_FILE, new_content, caller_bot_id=None)  # already checked above

    def render_for_prompt(self, max_chars: int = 4000) -> str:
        """Return a compact memory block suitable for injecting into a prompt.

        The block header announces the bot ID and file list; each file's
        content is included in full until *max_chars* is reached, at which
        point remaining files are summarised with their name only.

        Returns an empty string when the store has no files.
        """
        files = self.list_files()
        if not files:
            return ""

        header = f"<!-- bot-memory bot_id={self.bot_id} -->\n"
        parts: list[str] = [header]
        used = len(header)
        budget = max_chars - used

        for name in files:
            content = self.read(name)
            block = f"### {name}\n\n{content.strip()}\n\n"
            if budget > 0 and len(block) <= budget:
                parts.append(block)
                budget -= len(block)
            else:
                # Include just the name so the model knows the file exists.
                stub = f"### {name} [truncated — {len(content)} chars]\n\n"
                parts.append(stub)

        rendered = "".join(parts)
        # Final safety cap (handles edge case where header alone used most budget)
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars]
        return rendered


# ── Feature flag helper ──────────────────────────────────────────────────────


def bot_memory_enabled() -> bool:
    """Return ``True`` when ``VEYA_BOT_MEMORY_MD`` is set to a truthy value."""
    val = os.environ.get("VEYA_BOT_MEMORY_MD", "0").strip().lower()
    return val not in {"", "0", "false", "off", "no"}


__all__ = [
    "CORE_FILE",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "BotMemoryStore",
    "bot_memory_enabled",
]
