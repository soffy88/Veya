"""Attention queue (Soulgent method, adapted for veya).

Needs-you covers approvals: tool actions gated on the owner. This covers
the other half: questions a bot or a room asks the owner in conversation.
An item stays open until the owner answers it (delivered as a user
message to the bot conversation or the room transcript, so the bot sees
it on its next turn) or dismisses it.

Status derivation (used by the board and the inbox) reads only persisted
state, never guesses:
- ``waiting``: the bot/room has open attention items.
- ``working``: the last visible message is user-authored, so a reply is
  still owed (turn in flight, queued, or failed — all mean "not idle").
- ``idle``: anything else.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from server.bot_team.conversations import ConversationStore, atomic_write_json, default_bots_root
from server.bot_team.groups import GroupChat, GroupStore
from server.goal_run.bot_identity import BotRegistry, BotSpec

ATTENTION_FILENAME = "attention.json"

#: Decision categories worth interrupting the owner for. Anything else
#: defaults to silent (recorded, never pushed). Policy lives in code, not
#: config, so changing what interrupts the owner is a reviewed change.
DECISION_CATEGORIES: dict[str, str] = {
    "delete-data": "needed",
    "stop-service": "needed",
    "spend-money": "needed",
    "outbound-message": "needed",
    "product-direction": "needed",
    "key-rotation": "needed",
    "permission-change": "needed",
    "publish-external": "needed",
}

#: How loudly an item may reach the owner. ``silent`` records only;
#: ``digest`` parks a digest event; ``needed``/``always`` may use the
#: existing push/channel delivery. ``always`` is only for an explicit
#: owner call; rule files (item 3) may never raise themselves to it.
TELLS = ("silent", "digest", "needed", "always")
DEFAULT_TELL = "silent"

#: Feature flag gating tell enforcement (digest recording). Storage of
#: category/tell is unconditional metadata; the flag only gates new
#: behaviour (the digest event).
TELL_POLICY_FLAG = "VEYA_TELL_POLICY"


def resolve_tell(*, category: str | None = None, tell: str | None = None) -> str:
    """Resolve an item's tell: explicit tell > category default > silent.

    Unknown categories and tells are producer bugs: refuse the whole
    write (ValueError) instead of storing a meaningless label.
    """
    if tell is not None:
        t = str(tell).strip().lower()
        if t not in TELLS:
            raise ValueError(f"unknown tell: {tell!r}")
        return t
    if category is not None:
        c = str(category).strip()
        if c not in DECISION_CATEGORIES:
            raise ValueError(f"unknown attention category: {category!r}")
        return DECISION_CATEGORIES[c]
    return DEFAULT_TELL


def tell_for(item: dict[str, Any]) -> str:
    """An item's effective tell, including legacy items.

    Items written before categories existed carry neither field; they
    were created expecting to be seen, so they read as ``needed``.
    """
    stored = item.get("tell")
    if isinstance(stored, str) and stored in TELLS:
        return stored
    category = item.get("category")
    if isinstance(category, str) and category in DECISION_CATEGORIES:
        return DECISION_CATEGORIES[category]
    return "needed"


def tell_policy_enabled() -> bool:
    """Is tell enforcement on? Off by default; off never crashes."""
    try:
        from server.nano_w10.flags import flag_enabled

        return bool(flag_enabled(TELL_POLICY_FLAG))
    except Exception:
        return False


class StandingRefusedError(ValueError):
    """Raised when a standing *declined* rule refuses a new question.

    A ValueError subclass so existing validation handling still applies,
    but routes distinguish it for a 200 refused payload. Carries the
    matched standing row on ``.standing``.
    """

    def __init__(self, standing: dict[str, Any]) -> None:
        self.standing = standing
        super().__init__(f"refused by standing rule {standing.get('id')}: {standing.get('statement', '')[:80]}")


def _record_tell_digest(item: dict[str, Any]) -> None:
    """Park a digest event for a silent item; never fails the write."""
    try:
        from server.events import append_canonical_event

        append_canonical_event(
            "attention.digest",
            {
                "item_id": item.get("id"),
                "question": str(item.get("question") or "")[:200],
                "category": item.get("category"),
            },
            actor="system",
        )
    except Exception:
        pass


def _record_standing_refusal(standing: dict[str, Any], question: str) -> None:
    """Best-effort audit event for a refused question; never fails create."""
    try:
        from server.events import append_canonical_event

        append_canonical_event(
            "standing.refused",
            {
                "standing_id": standing.get("id"),
                "verdict": standing.get("verdict"),
                "question": str(question or "")[:200],
            },
            actor="system",
        )
    except Exception:
        pass

OPEN = "open"

#: Question items live this long; expiry only marks state, rows are kept.
#: Same 7-day bound as approvals and turn resumes.
ATTENTION_TTL_S = 7 * 24 * 3600


def is_expired(item: dict[str, Any], *, now: float | None = None) -> bool:
    """True when an open item is past its answer window.

    Items created before the expiry field existed derive their deadline
    from ``created_ts``; items with no timestamp at all never expire.
    """

    if item.get("status") != OPEN:
        return False
    expires_at = item.get("expires_at")
    if expires_at is None:
        created_ts = item.get("created_ts")
        if created_ts is None:
            return False
        expires_at = float(created_ts) + ATTENTION_TTL_S
    stamp = time.time() if now is None else float(now)
    return float(expires_at) <= stamp


#: What an attention item is waiting on. ``question`` is the
#: default; ``escalation`` is a room handing a stuck round to a human;
#: ``incident`` is a failed delegation or job reporting itself.
ATTENTION_KINDS = ("question", "escalation", "incident")
MAX_OPTIONS = 6
MAX_OPTION_CHARS = 200


def _validate_options(options: object) -> list[str] | None:
    """Normalise an options list, or refuse it whole (fail-closed).

    An option is one tappable answer on the card. Empty or over-long
    entries and more than MAX_OPTIONS are producer bugs: refuse the
    whole write instead of rendering a broken card.
    """

    if options is None:
        return None
    if not isinstance(options, list):
        raise ValueError("options must be a list of strings")
    cleaned: list[str] = []
    for entry in options:
        if not isinstance(entry, str):
            raise ValueError("options must be a list of strings")
        text = entry.strip()
        if not text:
            raise ValueError("options must not contain an empty option")
        if len(text) > MAX_OPTION_CHARS:
            raise ValueError(f"option exceeds {MAX_OPTION_CHARS} characters")
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > MAX_OPTIONS:
        raise ValueError(f"at most {MAX_OPTIONS} options")
    return cleaned or None


ANSWERED = "answered"
DISMISSED = "dismissed"


class AttentionStore:
    """Open/dismissed/answered question items, one JSON file under bots root."""

    def __init__(self, root: str | Path | None = None, ledger: Any | None = None) -> None:
        self.root = Path(root) if root is not None else default_bots_root()
        self.path = self.root / ATTENTION_FILENAME
        # Standing-gate ledger handle; None means the process-global ledger
        # (tests inject a temp-path DecisionLedger here).
        self._ledger = ledger

    def _load(self) -> list[dict[str, Any]]:
        try:
            import json

            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, items)

    def create(
        self,
        *,
        question: str,
        bot_id: str | None = None,
        group_id: str | None = None,
        context: str | None = None,
        kind: str = "question",
        options: list[str] | None = None,
        bypass_standing: bool = False,
        category: str | None = None,
        tell: str | None = None,
    ) -> dict[str, Any]:
        if kind not in ATTENTION_KINDS:
            raise ValueError(f"unknown attention kind: {kind!r}")
        resolved_tell = resolve_tell(category=category, tell=tell)
        # Standing gate (item 1): questions only. Escalations and incidents
        # are already human-bound by definition and always pass through.
        standing_id: str | None = None
        if kind == "question" and not bypass_standing:
            from server import decision_ledger as dl_mod

            match = dl_mod.check_standing(
                f"{question}\n{context or ''}", ledger=self._ledger
            )
            if match is not None and match.get("verdict") == "declined":
                _record_standing_refusal(match, question)
                raise StandingRefusedError(match)
            if match is not None and match.get("verdict") == "decided":
                standing_id = str(match.get("id") or "")
                context = ((context + "\n") if context else "") + (
                    f"[定规] {match.get('statement', '')}"
                )
        item = {
            "id": f"att_{uuid.uuid4().hex[:16]}",
            "bot_id": bot_id,
            "group_id": group_id,
            "question": question,
            "context": context,
            "kind": kind,
            "options": _validate_options(options),
            "category": category,
            "tell": resolved_tell,
            "status": OPEN,
            "created_ts": time.time(),
            "expires_at": time.time() + ATTENTION_TTL_S,
            "answered_ts": None,
            "answer": None,
            "standing_id": standing_id,
        }
        items = self._load()
        items.append(item)
        self._save(items)
        from server.bot_team.rev import bump_rev

        bump_rev(root=self.root)
        if resolved_tell == "silent" and tell_policy_enabled():
            _record_tell_digest(item)
        return {**item, "expired": is_expired(item)}

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        items = self._load()
        if status is not None:
            items = [item for item in items if item.get("status") == status]
        for item in items:
            item["expired"] = is_expired(item)
        if status == OPEN:
            # The open list is the actionable list: expired items are
            # visible under status=None (all) but no longer waiting.
            items = [item for item in items if not item["expired"]]
        return items

    def get(self, item_id: str) -> dict[str, Any] | None:
        for item in self._load():
            if item.get("id") == item_id:
                item["expired"] = is_expired(item)
                return item
        return None

    def _close(self, item_id: str, *, status: str, answer: str | None = None) -> dict[str, Any] | None:
        items = self._load()
        for item in items:
            if item.get("id") == item_id:
                if item.get("status") != OPEN:
                    return None
                if answer is not None and is_expired(item):
                    # Expired questions stay visible but can no longer be
                    # answered: the answer would land in a turn that has
                    # long since moved on. Dismissal stays allowed so the
                    # owner can clear the stale item.
                    return None
                item["status"] = status
                item["answered_ts"] = time.time()
                if answer is not None:
                    item["answer"] = answer
                self._save(items)
                from server.bot_team.rev import bump_rev

                bump_rev(root=self.root)
                return {**item, "expired": is_expired(item)}
        return None

    def answer(self, item_id: str, answer: str) -> dict[str, Any] | None:
        return self._close(item_id, status=ANSWERED, answer=answer)

    def dismiss(self, item_id: str) -> dict[str, Any] | None:
        return self._close(item_id, status=DISMISSED)

    def open_count(self, *, bot_id: str | None = None, group_id: str | None = None) -> int:
        count = 0
        for item in self._load():
            if item.get("status") != OPEN:
                continue
            if is_expired(item):
                continue
            if bot_id is not None and item.get("bot_id") != bot_id:
                continue
            if group_id is not None and item.get("group_id") != group_id:
                continue
            count += 1
        return count


def _status_from_messages(
    messages: list[dict[str, Any]], *, open_attention: int
) -> tuple[str, float | None]:
    if open_attention > 0:
        status = "waiting"
    elif messages and messages[-1].get("role") == "user":
        status = "working"
    else:
        status = "idle"
    last_ts = None
    if messages:
        raw_ts = messages[-1].get("ts")
        if isinstance(raw_ts, (int, float)):
            last_ts = float(raw_ts)
    return status, last_ts


def bot_board_entry(
    spec: BotSpec, *, conversations: ConversationStore, store: AttentionStore
) -> dict[str, Any]:
    messages = conversations.list(spec.bot_id)
    open_attention = store.open_count(bot_id=spec.bot_id)
    status, last_ts = _status_from_messages(messages, open_attention=open_attention)
    return {
        "bot_id": spec.bot_id,
        "name": spec.name or spec.bot_id,
        "status": status,
        "open_attention": open_attention,
        "last_active_ts": last_ts,
    }


def build_board(
    registry: BotRegistry,
    *,
    conversations: ConversationStore,
    store: AttentionStore,
    owner_id: str | None = None,
) -> list[dict[str, Any]]:
    from server.bot_team.chef import CHEF_BOT_ID
    from server.goal_run.bot_identity import DEFAULT_BOT_ID

    entries = []
    for spec in registry.list():
        if spec.bot_id not in (DEFAULT_BOT_ID, CHEF_BOT_ID):
            if owner_id is not None and spec.owner_id != owner_id:
                continue
        entries.append(bot_board_entry(spec, conversations=conversations, store=store))
    return entries


def build_inbox(
    registry: BotRegistry,
    *,
    conversations: ConversationStore,
    groups: GroupStore,
    store: AttentionStore,
    owner_id: str | None = None,
) -> dict[str, Any]:
    from server.bot_team.rev import ReadMarkStore, unread_for_group

    marks = ReadMarkStore(groups.root)
    group_rows = []
    for group in groups.list():
        messages = groups.messages(group.id)
        open_attention = store.open_count(group_id=group.id)
        status, last_ts = _status_from_messages(messages, open_attention=open_attention)
        read_rev = marks.get(owner_id or "", group.id)
        group_rows.append(
            {
                "group_id": group.id,
                "name": group.name,
                "reply_mode": group.reply_mode or "everyone",
                "member_count": len(group.bot_ids),
                "status": status,
                "open_attention": open_attention,
                "last_active_ts": last_ts,
                "read_rev": read_rev,
                "unread": unread_for_group(messages, open_attention, read_rev),
            }
        )
    open_items = store.list(status=OPEN)
    # Escalations wait on a human's decision: they pin above ordinary
    # questions, in creation order within each kind.
    open_items.sort(
        key=lambda item: (
            0 if item.get("kind") == "escalation" else 1,
            float(item.get("created_ts") or 0.0),
        )
    )
    return {
        "attention": open_items,
        "board": build_board(
            registry, conversations=conversations, store=store, owner_id=owner_id
        ),
        "groups": group_rows,
    }


__all__ = [
    "ANSWERED",
    "DISMISSED",
    "OPEN",
    "AttentionStore",
    "StandingRefusedError",
    "build_board",
    "build_inbox",
]
