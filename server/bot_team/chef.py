"""Chef: the bot that proposes a team, and the proposal ledger (Lorca P1).

Chef is an ordinary bot with one extra scripted behaviour: it may propose a
team as a structured JSON block. It has no privileges — it cannot create a bot,
cannot touch another bot's conversation and cannot run anything but a normal
turn. Creating the proposed bots requires an explicit human
``POST /api/v1/bots/proposals/{id}/accept``.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json, default_bots_root
from server.goal_run.bot_identity import BotRegistry, BotSpec

CHEF_BOT_ID = "chef"
CHEF_NAME = "Chef"

#: A team stays small; a proposal asking for more is refused.
MAX_PROPOSAL_BOTS = 6

_PROPOSALS_FILE = "chef_proposals.json"

#: ```veya-bots ... ``` (the documented form) or ```json ... ```.
_BLOCK_RE = re.compile(
    r"```(?:veya-bots|json)\s*\n(?P<body>.*?)```",
    re.DOTALL,
)

CHEF_INSTRUCTIONS = """You are Chef, the lead of a team of one-job Veya bots.

Your job is to understand what the user works on, then propose a small team of
bots where every bot has exactly one job. You never create a bot yourself and
you never run work for another bot: you ask, propose, and route.

How to work:
1. If you do not yet know what the user works on, ask one short question about
   it and stop. Do not propose a team before you know the work.
2. When you know the work, propose 2-4 bots. Each bot must have one job you can
   state in one sentence, a lowercase snake_case bot_id, and short instructions
   that tell it how to do that one job.
3. Always emit the proposal as exactly one fenced block so the user can accept
   it with one click:

```veya-bots
{"bots": [{"bot_id": "research", "name": "Researcher", "description": "Finds and summarises sources", "instructions": "..."}]}
```

4. Never invent a team before the user told you what they do, and never create
   more than 4 bots without being asked.
5. After a proposal, tell the user which bot to talk to for which job, and that
   they can accept the proposal to create the team.

You may also route work: when the user asks for something that belongs to a bot
that exists, say which bot should do it and what to ask that bot."""


def chef_spec(owner_id: str = "") -> BotSpec:
    """The Chef BotSpec. Registered like any other bot, on first list."""

    return BotSpec(
        bot_id=CHEF_BOT_ID,
        owner_id=owner_id,
        name=CHEF_NAME,
        description="Understands what the user works on and proposes a small team of one-job bots.",
        instructions=CHEF_INSTRUCTIONS,
        runner_id="local",
    )


def ensure_chef_seeded(registry: BotRegistry, *, owner_id: str = "") -> BotSpec | None:
    """Register Chef once. Existing bots are never modified.

    Returns the Chef spec, or ``None`` when it already existed (idempotent:
    a first GET /api/v1/bots seeds it, later calls are a no-op).
    """

    existing = registry.get(CHEF_BOT_ID)
    if existing is not None:
        return None
    spec = chef_spec(owner_id)
    registry.register(spec)
    return spec


def _normalise_proposed_bot(raw: Any) -> dict[str, str] | None:
    """Validate one proposed bot. Anything invalid is dropped, not repaired."""

    if not isinstance(raw, dict):
        return None
    bot_id = str(raw.get("bot_id") or "").strip()
    if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", bot_id):
        return None

    def _text(key: str, limit: int) -> str:
        return str(raw.get(key) or "").strip()[:limit]

    name = _text("name", 120) or bot_id
    description = _text("description", 400)
    instructions = _text("instructions", 4000)
    if not description and not instructions:
        # A one-job bot must say what its job is.
        return None
    return {
        "bot_id": bot_id,
        "name": name,
        "description": description,
        "instructions": instructions,
        "provider": _text("provider", 128),
        "model": _text("model", 256),
    }


def extract_proposal(text: str) -> dict[str, Any] | None:
    """Read Chef's structured proposal out of a reply, if it contains one.

    Returns ``{"proposal": ..., "bots": [...]}`` or ``None``. The reply text is
    data, never instructions: only the documented block shape is honoured.
    """

    for match in _BLOCK_RE.finditer(str(text or "")):
        try:
            parsed = json.loads(match.group("body"))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        raw_bots = parsed.get("bots")
        if not isinstance(raw_bots, list):
            continue
        bots = [item for item in (_normalise_proposed_bot(bot) for bot in raw_bots) if item]
        if not bots or len(bots) > MAX_PROPOSAL_BOTS:
            continue
        return {"proposal": parsed, "bots": bots[:MAX_PROPOSAL_BOTS]}
    return None


class ProposalStore:
    """Proposals Chef emitted, awaiting an explicit human accept."""

    def __init__(self, root: str | Path | None = None):
        # ``root`` is the bots root (the same one ConversationStore uses), never
        # the proposals file itself, so both spellings agree on one path.
        base = Path(root) if root is not None else default_bots_root()
        self.path = base / _PROPOSALS_FILE

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}

    def _save(self, items: dict[str, dict[str, Any]]) -> None:
        atomic_write_json(self.path, items)

    def record(
        self,
        text: str,
        *,
        task_id: str | None = None,
        owner_id: str = "",
    ) -> dict[str, Any] | None:
        """Store the proposal contained in ``text``; returns it, or ``None``."""

        found = extract_proposal(text)
        if found is None:
            return None
        items = self._load()
        proposal = {
            "id": f"prop_{uuid.uuid4().hex[:16]}",
            "created_by": CHEF_BOT_ID,
            "created_at": time.time(),
            "task_id": task_id,
            "owner_id": str(owner_id or ""),
            "bots": found["bots"],
            "proposal": found["proposal"],
            "status": "pending",
        }
        items[proposal["id"]] = proposal
        self._save(items)
        return proposal

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        return self._load().get(str(proposal_id))

    def list(self) -> list[dict[str, Any]]:
        return sorted(self._load().values(), key=lambda item: float(item.get("created_at") or 0))

    def accept(
        self, proposal_id: str, registry: BotRegistry, *, owner_id: str = ""
    ) -> dict[str, Any]:
        """Create the proposed bots. Bots that already exist are skipped."""

        proposal = self.get(proposal_id)
        if proposal is None:
            raise KeyError(f"proposal {proposal_id!r} not found")
        if proposal.get("status") == "accepted":
            raise ValueError(f"proposal {proposal_id!r} was already accepted")
        created: list[str] = []
        skipped: list[str] = []
        for bot in proposal.get("bots") or []:
            bot_id = str(bot.get("bot_id") or "")
            if registry.get(bot_id) is not None:
                skipped.append(bot_id)
                continue
            registry.register(
                BotSpec(
                    bot_id=bot_id,
                    owner_id=str(owner_id or proposal.get("owner_id") or ""),
                    name=str(bot.get("name") or bot_id),
                    description=str(bot.get("description") or ""),
                    instructions=str(bot.get("instructions") or ""),
                    provider=str(bot.get("provider") or ""),
                    model=str(bot.get("model") or ""),
                    runner_id="local",
                )
            )
            created.append(bot_id)
        items = self._load()
        items[str(proposal_id)]["status"] = "accepted"
        items[str(proposal_id)]["accepted_at"] = time.time()
        items[str(proposal_id)]["accepted_by"] = str(owner_id or "")
        items[str(proposal_id)]["created_bot_ids"] = created
        self._save(items)
        return {
            "proposal_id": str(proposal_id),
            "status": "accepted",
            "created": created,
            "skipped": skipped,
        }


def proposal_store(root: str | Path | None = None) -> ProposalStore:
    return ProposalStore(root)


__all__ = [
    "CHEF_BOT_ID",
    "CHEF_INSTRUCTIONS",
    "CHEF_NAME",
    "MAX_PROPOSAL_BOTS",
    "ProposalStore",
    "chef_spec",
    "ensure_chef_seeded",
    "extract_proposal",
    "proposal_store",
]
