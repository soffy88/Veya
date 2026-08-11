"""
veya/kanban.py — Kanban Board + Inbox + Templates (Layer 4).

Data models and persistence for task management:
- Kanban boards with columns, cards, dependencies
- Inbox message queue for agent notifications
- Project templates (hedge fund, code review, etc.)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Kanban types
# ---------------------------------------------------------------------------


class CardStatus(StrEnum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"


@dataclass
class KanbanCard:
    """A single task card on the kanban board."""

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str = ""
    description: str = ""
    status: CardStatus = CardStatus.BACKLOG
    priority: int = 0  # 0=low, 1=medium, 2=high, 3=critical
    assignee: str = ""  # agent name or "auto"
    depends_on: list[str] = field(default_factory=list)  # card IDs this depends on
    blocked_by: list[str] = field(default_factory=list)   # card IDs blocking this
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "status": self.status.value, "priority": self.priority,
            "assignee": self.assignee, "depends_on": self.depends_on,
            "blocked_by": self.blocked_by, "tags": self.tags,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "completed_at": self.completed_at, "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KanbanCard":
        return cls(
            id=d.get("id", ""), title=d.get("title", ""),
            description=d.get("description", ""),
            status=CardStatus(d.get("status", "backlog")),
            priority=d.get("priority", 0), assignee=d.get("assignee", ""),
            depends_on=d.get("depends_on", []), blocked_by=d.get("blocked_by", []),
            tags=d.get("tags", []), created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            completed_at=d.get("completed_at"), metadata=d.get("metadata", {}),
        )


@dataclass
class KanbanColumn:
    """A column on the kanban board."""

    name: str
    status: CardStatus
    cards: list[KanbanCard] = field(default_factory=list)
    wip_limit: int = 0  # 0 = unlimited


@dataclass
class KanbanBoard:
    """A complete kanban board."""

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = "Default Board"
    columns: list[KanbanColumn] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @classmethod
    def create_default(cls, name: str = "Default Board") -> "KanbanBoard":
        return cls(
            name=name,
            columns=[
                KanbanColumn(name="Backlog", status=CardStatus.BACKLOG),
                KanbanColumn(name="To Do", status=CardStatus.TODO),
                KanbanColumn(name="In Progress", status=CardStatus.IN_PROGRESS, wip_limit=3),
                KanbanColumn(name="Review", status=CardStatus.REVIEW),
                KanbanColumn(name="Done", status=CardStatus.DONE),
                KanbanColumn(name="Blocked", status=CardStatus.BLOCKED),
            ],
        )

    def add_card(self, card: KanbanCard, column_status: CardStatus | None = None):
        target = column_status or card.status
        for col in self.columns:
            if col.status == target:
                col.cards.append(card)
                return

    def move_card(self, card_id: str, to_status: CardStatus) -> bool:
        for col in self.columns:
            for card in list(col.cards):
                if card.id == card_id:
                    col.cards.remove(card)
                    card.status = to_status
                    card.updated_at = time.time()
                    if to_status == CardStatus.DONE:
                        card.completed_at = time.time()
                    self.add_card(card, to_status)
                    return True
        return False

    def get_card(self, card_id: str) -> KanbanCard | None:
        for col in self.columns:
            for card in col.cards:
                if card.id == card_id:
                    return card
        return None

    def get_dependency_graph(self) -> dict[str, list[str]]:
        """Build a dependency graph for all cards."""
        graph: dict[str, list[str]] = {}
        for col in self.columns:
            for card in col.cards:
                graph[card.id] = card.depends_on + card.blocked_by
        return graph

    def get_ready_cards(self) -> list[KanbanCard]:
        """Get cards that are unblocked (all dependencies met)."""
        completed = set()
        for col in self.columns:
            for card in col.cards:
                if card.status == CardStatus.DONE:
                    completed.add(card.id)

        ready = []
        for col in self.columns:
            if col.status in (CardStatus.TODO, CardStatus.BACKLOG):
                for card in col.cards:
                    deps_met = all(d in completed for d in card.depends_on)
                    not_blocked = not any(b not in completed for b in card.blocked_by)
                    if deps_met and not_blocked:
                        ready.append(card)
        return ready

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "columns": [
                {"name": c.name, "status": c.status.value, "wip_limit": c.wip_limit,
                 "cards": [card.to_dict() for card in c.cards]}
                for c in self.columns
            ],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KanbanBoard":
        board = cls(id=d.get("id", ""), name=d.get("name", ""),
                    created_at=d.get("created_at", time.time()))
        for col_data in d.get("columns", []):
            col = KanbanColumn(
                name=col_data["name"],
                status=CardStatus(col_data["status"]),
                wip_limit=col_data.get("wip_limit", 0),
                cards=[KanbanCard.from_dict(c) for c in col_data.get("cards", [])],
            )
            board.columns.append(col)
        return board


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


@dataclass
class InboxMessage:
    """A message in the agent's inbox."""

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    from_platform: str = ""   # "discord", "slack", "feishu", "system", "agent"
    from_user: str = ""       # pseudo-anonymized user ID
    subject: str = ""
    body: str = ""
    priority: int = 0
    read: bool = False
    archived: bool = False
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "from_platform": self.from_platform,
            "from_user": self.from_user, "subject": self.subject,
            "body": self.body, "priority": self.priority,
            "read": self.read, "archived": self.archived,
            "created_at": self.created_at, "metadata": self.metadata,
        }


class Inbox:
    """User inbox for agent messages and notifications."""

    def __init__(self, user_id: str, store_dir: Path | None = None):
        self.user_id = user_id
        self._dir = store_dir or Path.home() / ".veya" / "inbox"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._messages: list[InboxMessage] = []
        self._load()

    def _path(self) -> Path:
        safe = self.user_id.replace("/", "_").replace(":", "_")
        return self._dir / f"{safe}.json"

    def _load(self):
        path = self._path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self._messages = [InboxMessage(**m) for m in data]
            except Exception:
                self._messages = []

    def _save(self):
        self._path().write_text(
            json.dumps([m.__dict__ for m in self._messages], default=str, indent=2)
        )

    def add(self, *, from_platform: str = "system", from_user: str = "",
            subject: str = "", body: str = "", priority: int = 0) -> InboxMessage:
        msg = InboxMessage(
            from_platform=from_platform, from_user=from_user,
            subject=subject, body=body, priority=priority,
        )
        self._messages.append(msg)
        self._save()
        return msg

    def list(self, *, unread_only: bool = False, limit: int = 50) -> list[InboxMessage]:
        msgs = [m for m in self._messages if not m.archived]
        if unread_only:
            msgs = [m for m in msgs if not m.read]
        return sorted(msgs, key=lambda m: (-m.priority, -m.created_at))[:limit]

    def mark_read(self, msg_id: str):
        for m in self._messages:
            if m.id == msg_id:
                m.read = True
        self._save()

    def archive(self, msg_id: str):
        for m in self._messages:
            if m.id == msg_id:
                m.archived = True
        self._save()

    def count_unread(self) -> int:
        return sum(1 for m in self._messages if not m.read and not m.archived)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


TEMPLATES: dict[str, dict] = {
    "hedge-fund": {
        "name": "Hedge Fund Analysis",
        "description": "Quantitative hedge fund research & trading pipeline",
        "kanban_columns": ["Research", "Signal Dev", "Backtest", "Risk Review", "Live"],
        "skills": ["bootstrap_sharpe", "walk_forward_optimization", "regime_change_detector",
                   "tail_risk_analyzer", "scenario_stress_test"],
        "tools": ["git", "terminal", "filesystem"],
        "system_prompt": "You are a quantitative hedge fund analyst. Focus on risk-adjusted returns, regime detection, and factor attribution.",
        "default_config": {"model": "claude-sonnet-4-6", "budget_usd": 100.0, "mode": "build"},
    },
    "code-review": {
        "name": "Code Review Pipeline",
        "description": "Automated code review with test generation",
        "kanban_columns": ["Queued", "Reviewing", "Testing", "Reported"],
        "skills": ["code_review", "static_analysis", "test_generation"],
        "tools": ["git", "terminal", "filesystem", "ripgrep"],
        "system_prompt": "You are a senior code reviewer. Analyze code for bugs, security issues, and style violations. Generate tests for found issues.",
        "default_config": {"model": "claude-sonnet-4-6", "budget_usd": 10.0, "mode": "plan"},
    },
    "web-scraper": {
        "name": "Web Scraping Agent",
        "description": "Autonomous web scraping and data extraction",
        "kanban_columns": ["URL Queue", "Scraping", "Extracting", "Validating", "Exported"],
        "skills": ["browser_automation", "data_extraction", "schema_validation"],
        "tools": ["browser", "filesystem", "terminal"],
        "system_prompt": "You are a web scraping expert. Navigate websites, extract structured data, and validate against schemas.",
        "default_config": {"model": "gpt-4o", "budget_usd": 5.0, "mode": "build", "headless": True},
    },
    "voice-assistant": {
        "name": "Voice Assistant",
        "description": "Multi-modal voice agent with vision",
        "kanban_columns": ["Incoming", "Transcribing", "Processing", "Responding"],
        "skills": ["speech_to_text", "text_to_speech", "analyze_image", "turn_detection"],
        "tools": ["audio", "vision", "terminal"],
        "system_prompt": "You are a helpful voice assistant that can see and hear. Respond naturally in conversation.",
        "default_config": {"model": "gpt-4o", "stt_provider": "openai", "tts_provider": "openai"},
    },
    "ci-cd": {
        "name": "CI/CD Pipeline Agent",
        "description": "Automated CI/CD pipeline management",
        "kanban_columns": ["Build", "Test", "Lint", "Deploy", "Monitor"],
        "skills": ["run_tests", "lint_check", "deploy", "monitor_logs"],
        "tools": ["terminal", "git", "docker"],
        "system_prompt": "You are a DevOps engineer managing CI/CD pipelines. Run builds, tests, and deployments.",
        "default_config": {"model": "claude-sonnet-4-6", "budget_usd": 20.0},
    },
    "research": {
        "name": "Deep Research Agent",
        "description": "Branching deep research with citations",
        "kanban_columns": ["Query", "Researching", "Drafting", "Reviewing", "Published"],
        "skills": ["deep_research_tree", "web_search", "citation_tracing"],
        "tools": ["browser", "terminal", "filesystem"],
        "system_prompt": "You are a research analyst. Conduct thorough research with citations and produce structured reports.",
        "default_config": {"model": "claude-sonnet-4-6", "max_depth": 3, "max_branches": 5},
    },
}


def list_templates() -> list[dict[str, str]]:
    """List all available project templates."""
    return [
        {"id": tid, "name": t["name"], "description": t["description"]}
        for tid, t in TEMPLATES.items()
    ]


def get_template(template_id: str) -> dict | None:
    """Get a template by ID."""
    return TEMPLATES.get(template_id)


def apply_template(template_id: str, *, name: str = "", output_dir: Path | None = None) -> dict[str, Any]:
    """Apply a template — create kanban board + config + prompt.

    Returns dict with board, config, system_prompt.
    """
    template = get_template(template_id)
    if template is None:
        return {"error": f"Unknown template: {template_id}"}

    board = KanbanBoard.create_default(name or template["name"])
    return {
        "template_id": template_id,
        "name": template["name"],
        "kanban": board.to_dict(),
        "skills": template["skills"],
        "tools": template["tools"],
        "system_prompt": template["system_prompt"],
        "config": template["default_config"],
    }
