# 3O-IO-ALLOW — server/bot_team/memory_layers.py
# 理由：per-bot 可读 Markdown 记忆的**物理落盘面**。目录布局、原子写、caps 与
# journal 都是 P3 ``server/bot_team/memory.py`` 既有约定的延伸（同一个
# ``<bots_root>/<bot_id>/memory/``、同一套 ``os.replace`` 原子写），拆到 obase 会
# 造出第二套记忆存储 —— 明确违反「不新增第二套存储」的冻结约束。判定/检索/TTL
# 算法一律在 ``veya.oskill.pure.memory_layers``（3O-PURE，零 IO），本文件只做
# 文件动作。删除 bot 时移动到 archive/ 也在这里，与 conversations.py 同源。

"""Layered per-bot memory: daily logs, scratch TTL, capped core, team scope.

NANO-W10 item 5 (+ ADDENDUM O). Four tiers over the **existing** ``BotMemoryStore``
directory layout — this module adds views, never a second store:

``<bots_root>/<bot_id>/memory/MEMORY.md``     core, always injected, char-capped
``<bots_root>/<bot_id>/memory/scratch.md``    TTL entries, injected while live
``<bots_root>/<bot_id>/memory/daily/*.md``    dated append-only logs, searchable
``<bots_root>/<bot_id>/memory/journal.jsonl`` every memory change, before/after
``<bots_root>/team/MEMORY.md`` + topics       team scope, read by every bot

Scope discipline: a bot reads its own memory and the team memory, and writes
only its own — ``require_same_bot`` is the same fail-closed guard the existing
store uses, extended so a team write additionally requires the caller to be an
authorised writer (injected by layer 4; default deny).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from runtime import bot_scope
from runtime.bot_scope import require_same_bot
from server.bot_team.memory import CORE_FILE, BotMemoryStore, bot_memory_enabled
from veya.oskill.pure.memory_layers import (
    CORE_MEMORY_MAX_CHARS,
    DEFAULT_SCRATCH_TTL_SECONDS,
    TEAM_MEMORY_MAX_CHARS,
    TOPIC_MEMORY_MAX_CHARS,
    cap_report,
    daily_log_line,
    gardening_proposals,
    gc_expired_scratch,
    memory_search_hits,
    normalize_entry,
)

__all__ = [
    "DAILY_DIR",
    "bot_memory_search",
    "JOURNAL_FILE",
    "SCRATCH_FILE",
    "TEAM_SCOPE_ID",
    "LayeredMemoryStore",
    "bot_memory_enabled",
    "scratch_ttl_seconds",
    "team_memory_enabled",
]

DAILY_DIR = "daily"
SCRATCH_FILE = "scratch.md"
JOURNAL_FILE = "journal.jsonl"
TEAM_SCOPE_ID = "team"
ARCHIVE_DIR = "archive"

_VALID_TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def scratch_ttl_seconds() -> float:
    """Scratch TTL in seconds (default 2h; ``VEYA_SCRATCH_TTL_SECONDS`` overrides)."""
    raw = os.environ.get("VEYA_SCRATCH_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SCRATCH_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SCRATCH_TTL_SECONDS
    return value if value > 0 else DEFAULT_SCRATCH_TTL_SECONDS


def team_memory_enabled() -> bool:
    """Whether TEAM-scope memory writes are enabled (default OFF, same flag family)."""
    val = os.environ.get("VEYA_TEAM_MEMORY", "0").strip().lower()
    return val not in {"", "0", "false", "off", "no"}


def _default_bots_root() -> Path:
    root = os.environ.get("VEYA_BOTS_ROOT", "")
    if root:
        return Path(root).expanduser()
    return Path.home() / ".veya" / "bots"


class LayeredMemoryStore:
    """The four memory tiers for one bot, over the existing BotMemoryStore layout."""

    def __init__(self, bot_id: str, *, bots_root: str | Path | None = None) -> None:
        if not bot_id:
            raise ValueError("bot_id must be a non-empty string")
        self.bot_id = bot_id
        root = Path(bots_root).expanduser() if bots_root is not None else _default_bots_root()
        self.bots_root = root
        self._store = BotMemoryStore(bot_id, bots_root=root)
        self._team_store = BotMemoryStore(TEAM_SCOPE_ID, bots_root=root)

    # ── paths ────────────────────────────────────────────────────────────

    @property
    def memory_dir(self) -> Path:
        return self._store.memory_dir

    @property
    def daily_dir(self) -> Path:
        return self.memory_dir / DAILY_DIR

    @property
    def scratch_path(self) -> Path:
        return self.memory_dir / SCRATCH_FILE

    @property
    def journal_path(self) -> Path:
        return self.memory_dir / JOURNAL_FILE

    def daily_path(self, day: str) -> Path:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day or "")):
            raise ValueError(f"daily log day must be YYYY-MM-DD, got {day!r}")
        return self.daily_dir / f"{day}.md"

    # ── daily ────────────────────────────────────────────────────────────

    def append_daily(self, what: str, *, when: float | None = None, caller_bot_id: str | None = None):
        """Append one "what was done" line to today's log. Never injected."""
        if caller_bot_id is not None:
            require_same_bot(caller_bot_id, self.bot_id, "memory:daily")
        stamp = float(when if when is not None else time.time())
        day = time.strftime("%Y-%m-%d", time.gmtime(stamp))
        path = self.daily_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(f"# {day}\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(daily_log_line(when=stamp, what=what) + "\n")
        return {"day": day, "path": str(path)}

    def list_daily(self) -> list[str]:
        """Sorted day file names (``YYYY-MM-DD.md``), oldest first."""
        if not self.daily_dir.is_dir():
            return []
        return sorted(p.name for p in self.daily_dir.glob("*.md") if p.is_file())

    # ── scratch ──────────────────────────────────────────────────────────

    def write_scratch(self, text: str, *, expires_in: float | None = None, caller_bot_id: str | None = None):
        """Write one scratch entry with a TTL stamp. Injected while live."""
        if caller_bot_id is not None:
            require_same_bot(caller_bot_id, self.bot_id, "memory:scratch")
        ttl = scratch_ttl_seconds() if expires_in is None else float(expires_in)
        expires = time.time() + (ttl if ttl > 0 else 0)
        body = " ".join(str(text or "").split())
        if not body:
            raise ValueError("scratch entry text must not be empty")
        self.scratch_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._read(self.scratch_path)
        header = "# Scratch\n\n" if not existing else ""
        with self.scratch_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{header}- {expires:.3f} {body}\n")
        return {"expires_at": expires, "ttl_seconds": ttl}

    def scratch_text(self, *, now: float | None = None) -> str:
        """Scratch content with expired entries removed, and the GC count."""
        return self.collect_scratch(now=now)[0]

    def collect_scratch(self, *, now: float | None = None) -> tuple[str, int]:
        """GC expired scratch entries; returns ``(live_text, removed_count)``."""
        raw = self._read(self.scratch_path)
        if not raw:
            return ("", 0)
        stamp = float(now if now is not None else time.time())
        text, removed = gc_expired_scratch(raw, now=stamp)
        if removed:
            with contextlib.suppress(OSError):
                self.scratch_path.write_text(text, encoding="utf-8")
        return (text, removed)

    def live_scratch_entries(self, *, now: float | None = None) -> list[str]:
        """Lines of the live scratch entries, in file order."""
        stamp = float(now if now is not None else time.time())
        text = self._read(self.scratch_path)
        live: list[str] = []
        for line in text.splitlines():
            match = re.match(r"^-\s*(\d{9,}(?:\.\d+)?)\s+(.*)$", line)
            if match and float(match.group(1)) > stamp:
                live.append(match.group(2).strip())
        return live

    # ── core ─────────────────────────────────────────────────────────────

    def core_text(self, scope: str = "bot") -> str:
        store = self._team_store if scope == TEAM_SCOPE_ID else self._store
        return store.read(CORE_FILE)

    def cap(self, scope: str = "bot") -> dict[str, Any]:
        """Where the core memory sits against its cap."""
        limit = TEAM_MEMORY_MAX_CHARS if scope == TEAM_SCOPE_ID else CORE_MEMORY_MAX_CHARS
        return cap_report(self.core_text(scope), limit=limit)

    def gardening(self, scope: str = "bot", *, keep_lines: int = 3) -> list[dict[str, Any]]:
        """Trim **proposals** for the core memory. Nothing is deleted here."""
        limit = TEAM_MEMORY_MAX_CHARS if scope == TEAM_SCOPE_ID else CORE_MEMORY_MAX_CHARS
        return gardening_proposals(
            self.core_text(scope), max_chars=limit, keep_lines=keep_lines
        )

    def append_entry(
        self,
        text: str,
        *,
        scope: str = "bot",
        source: str = "",
        added: str = "",
        caller_bot_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one normalised entry to the core memory, honouring the cap.

        A save past the cap **fails** with a message saying how to make room —
        it never truncates a user-written line silently.
        """
        if caller_bot_id is not None:
            require_same_bot(caller_bot_id, self.bot_id, f"memory:{scope}")
        store = self._team_store if scope == TEAM_SCOPE_ID else self._store
        if scope == TEAM_SCOPE_ID and not team_memory_enabled():
            raise PermissionError(
                "team memory is off; set VEYA_TEAM_MEMORY=1 to enable it"
            )
        limit = TEAM_MEMORY_MAX_CHARS if scope == TEAM_SCOPE_ID else CORE_MEMORY_MAX_CHARS
        entry = normalize_entry(
            text,
            source=source or f"part 1 #{self.bot_id[:8]}",
            added=added or time.strftime("%Y-%m-%d", time.gmtime()),
            bot_id=self.bot_id,
        )
        existing = store.read(CORE_FILE)
        prospective = len(existing) + (2 if existing else 0) + len(entry) + 1
        if prospective > limit:
            raise ValueError(
                f"{scope} memory would exceed its {limit}-char cap by "
                f"{prospective - limit} chars — delete or shorten a line to make room "
                f"(gardening proposals available)"
            )
        body = f"{existing}\n{entry}\n" if existing else f"{entry}\n"
        store.write(CORE_FILE, body)
        self._journal({"op": "append", "scope": scope, "before": existing, "after": body})
        return {"ok": True, "scope": scope, "entry": entry, "chars": len(body), "limit": limit}

    def remove_entry(self, needle: str, *, scope: str = "bot", caller_bot_id: str | None = None):
        """Remove the first core-memory line containing *needle*; journal the change."""
        if caller_bot_id is not None:
            require_same_bot(caller_bot_id, self.bot_id, f"memory:{scope}")
        store = self._team_store if scope == TEAM_SCOPE_ID else self._store
        existing = store.read(CORE_FILE)
        if needle not in existing:
            return {"ok": False, "removed": "", "reason": "entry not found"}
        kept = []
        removed = ""
        for line in existing.splitlines():
            if not removed and needle in line:
                removed = line
                continue
            kept.append(line)
        body = "\n".join(kept).strip() + ("\n" if kept else "")
        store.write(CORE_FILE, body)
        self._journal({"op": "remove", "scope": scope, "before": existing, "after": body})
        return {"ok": True, "removed": removed}

    # ── journal ──────────────────────────────────────────────────────────

    def _journal(self, record: dict[str, Any]) -> None:
        """Append one change record (before/after) to ``journal.jsonl``."""
        payload = {"ts": time.time(), "bot_id": self.bot_id, **record}
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError:
            return

    def journal(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent change records, newest last."""
        if not self.journal_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with contextlib.suppress(OSError):
            for line in self.journal_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        rows.append(parsed)
        return rows[-max(1, int(limit)) :]

    # ── search ───────────────────────────────────────────────────────────

    def searchable_files(self) -> dict[str, str]:
        """``{relative name: content}`` for core + scratch + daily logs."""
        files: dict[str, str] = {}
        files[CORE_FILE] = self.core_text("bot")
        team_core = self.core_text(TEAM_SCOPE_ID)
        if team_core:
            files[f"{TEAM_SCOPE_ID}/{CORE_FILE}"] = team_core
        for name in self._store.list_files():
            if name == CORE_FILE:
                continue
            files[name] = self._store.read(name)
        for day in self.list_daily():
            files[f"{DAILY_DIR}/{day}"] = self._read(self.daily_dir / day)
        return files

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Grep-style search across every memory tier of this bot."""
        return memory_search_hits(query, self.searchable_files(), limit=limit)

    # ── summary + archive ────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """Short prose summary regenerated from the current files."""
        core = self.core_text("bot").strip()
        entries = [line for line in core.splitlines() if line.strip() and not line.startswith("#")]
        proposals = self.gardening("bot")
        return {
            "bot_id": self.bot_id,
            "summary": "\n".join(entries[:8]),
            "entry_count": len(entries),
            "daily_days": self.list_daily(),
            "scratch_live": len(self.live_scratch_entries()),
            "cap": self.cap("bot"),
            "gardening_pending": len(proposals),
        }

    def archive(self) -> str:
        """Move this bot's memory into ``<bots_root>/archive/<bot_id>``.

        Used when a bot is deleted: its memory is preserved, never destroyed.
        """
        if not self.memory_dir.is_dir():
            return ""
        target = self.bots_root / ARCHIVE_DIR / self.bot_id
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return str(target)
        self.memory_dir.replace(target)
        return str(target)

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def topic_limit(self, topic: str) -> int:
        if not _VALID_TOPIC_RE.match(str(topic or "")):
            raise ValueError(f"invalid topic name {topic!r}")
        return TOPIC_MEMORY_MAX_CHARS


def _memory_tool_descriptions() -> tuple[str, str, str, str]:
    return (
        "Search this bot's whole memory — core MEMORY.md, topic files, live "
        "scratch and the dated daily logs. Use it before asking the user to "
        "repeat something; hits come back with file + line so you can cite them.",
        "Save one fact or decision the user stated into this bot's core memory. "
        "One line, normalised, character-capped. Refuses anything that looks "
        "like a key, password or identity number, and refuses to overwrite "
        "user-written lines to fit.",
        "Remove the first core-memory line of this bot containing the given "
        "text. Use only when the user asks you to forget something.",
        "What this bot currently remembers: core entries plus live scratch "
        "entries, with the cap status.",
    )


_MEMORY_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to look for."},
        "limit": {"type": "integer", "description": "Max hits (default 10)."},
    },
    "required": ["query"],
}

_REMEMBER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "The fact or decision to remember."},
        "source": {
            "type": "string",
            "description": 'Anchor for the entry, e.g. "part 2 #1234". Default: current turn.',
        },
    },
    "required": ["text"],
}

_FORGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "Substring of the line to remove from core memory.",
        }
    },
    "required": ["text"],
}

_RECALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "description": "Max entries to return (default 10)."}
    },
    "required": [],
}


def register_tools(registry: Any) -> int:
    """Register the layered-memory tools into the existing registry.

    Idempotent: returns the number of newly registered tools. Gated on
    ``VEYA_BOT_MEMORY_MD`` — the same flag that gates semantic injection — so a
    deployment with memory off does not grow a model-visible surface it cannot
    serve.
    """
    if not bot_memory_enabled():
        return 0
    if registry.has("bot_memory_search"):
        return 0
    from server.tool_registry import SideEffect

    search_desc, remember_desc, forget_desc, recall_desc = _memory_tool_descriptions()
    registry.register(
        "bot_memory_search",
        search_desc,
        _MEMORY_SEARCH_SCHEMA,
        bot_memory_search,
        side_effect=SideEffect.PURE_READ,
        effect_capability="none",
        parallel_safe=True,
    )
    registry.register(
        "remember",
        remember_desc,
        _REMEMBER_SCHEMA,
        remember,
        side_effect=SideEffect.LOCAL_WRITE,
        effect_capability="idempotency_key",
    )
    registry.register(
        "forget",
        forget_desc,
        _FORGET_SCHEMA,
        forget,
        side_effect=SideEffect.LOCAL_WRITE,
        effect_capability="idempotency_key",
    )
    registry.register(
        "recall",
        recall_desc,
        _RECALL_SCHEMA,
        recall,
        side_effect=SideEffect.PURE_READ,
        effect_capability="none",
        parallel_safe=True,
    )
    return 4


def _bound_bot() -> str:
    bot_id = bot_scope.current_bot()
    if not bot_id:
        raise RuntimeError(
            "bot memory tools require a bound bot scope (no running bot in this execution context)"
        )
    return bot_id


def bot_memory_search(query: str, limit: int = 10) -> str:
    """Grep the running bot's whole memory (core, scratch, daily logs, topics).

    Named ``bot_memory_search`` and not ``memory_search`` because the canonical
    ``memory_search`` tool already exists in ``server/tool_registry.py`` (the
    Personal MemoryRecord v2 search). Two tools cannot share one model-visible
    name without confusing the model, so the per-bot grep surface takes its own
    name and reuses the same pure search algorithm.
    """
    bot_id = _bound_bot()
    hits = LayeredMemoryStore(bot_id).search(query, limit=limit)
    return json.dumps(
        {"ok": True, "bot_id": bot_id, "query": query, "hits": hits},
        ensure_ascii=False,
    )


def remember(text: str, source: str = "") -> str:
    """Save one fact/decision to the running bot's core memory.

    Replaces the older ``bot_memory_note`` behaviour: the entry is normalised
    into the ``[source: ...; added: ...; by: ...]`` format and the character cap
    is enforced instead of the file growing unbounded.
    """
    bot_id = _bound_bot()
    try:
        result = LayeredMemoryStore(bot_id).append_entry(
            text, source=source, caller_bot_id=bot_id
        )
    except (ValueError, PermissionError) as exc:
        return json.dumps(
            {"ok": False, "bot_id": bot_id, "error": str(exc)}, ensure_ascii=False
        )
    return json.dumps({"ok": True, **result}, ensure_ascii=False)


def forget(text: str) -> str:
    """Remove the first core-memory line of the running bot containing *text*."""
    bot_id = _bound_bot()
    result = LayeredMemoryStore(bot_id).remove_entry(text, caller_bot_id=bot_id)
    return json.dumps({"ok": bool(result.get("ok")), "bot_id": bot_id, **result}, ensure_ascii=False)


def recall(limit: int = 10) -> str:
    """What the running bot currently remembers (core entries + live scratch)."""
    bot_id = _bound_bot()
    store = LayeredMemoryStore(bot_id)
    core = [
        line
        for line in store.core_text("bot").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    return json.dumps(
        {
            "ok": True,
            "bot_id": bot_id,
            "entries": core[: max(1, int(limit))],
            "scratch": store.live_scratch_entries(),
            "cap": store.cap("bot"),
        },
        ensure_ascii=False,
    )