# 3O-IO-ALLOW — server/project_brain/brief.py: PROJECT_STATE.md and brief.json persistence in .veya-project/
"""W16 Project Brain — Brief model, parser, renderer, and store.

Spec §3:
  PROJECT_STATE.md is the human-readable source of truth for the Brief.
  Fixed sections:
    1. 目标与范围 (Goal & Scope)
    2. 重要文件 (Key Files)
    3. 规则 (Rules: "- when: <cond> → do: <action> [approval: auto|ask]")
    4. 进行中任务与阻塞 (Active Tasks & Blockers; rendered from tasks.json)
    5. 约定 (Conventions; from DECISIONS.md)
  brief.json is the structured mirror generated from / written back to PROJECT_STATE.md.
"""

from __future__ import annotations

import contextlib
import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json
from server.project_store import ProjectStore

_BRIEF_JSON = "brief.json"

_RULE_PATTERN = re.compile(
    r"^(?:[-*]\s*)?when:\s*(?P<when>.+?)\s*(?:→|->)\s*do:\s*(?P<do>.+?)(?:\s*\[?(?:approval:\s*(?P<approval>auto|ask))\]?)?$",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class BriefRule:
    """A rule in section 3: '- when: <cond> → do: <action> [approval: auto|ask]'."""

    when: str
    do: str
    approval: str = "ask"  # auto | ask (default ask, §11: 收尾动作默认全部 ask)

    def __post_init__(self) -> None:
        self.when = self.when.strip()
        self.do = self.do.strip()
        app = str(self.approval).strip().lower()
        self.approval = "auto" if app == "auto" else "ask"

    def to_dict(self) -> dict[str, str]:
        return {"when": self.when, "do": self.do, "approval": self.approval}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BriefRule:
        return cls(
            when=str(data.get("when") or "").strip(),
            do=str(data.get("do") or "").strip(),
            approval=str(data.get("approval") or "ask").strip(),
        )

    def render(self) -> str:
        return f"- when: {self.when} → do: {self.do} [approval: {self.approval}]"


def parse_rule_line(line: str) -> BriefRule | None:
    """Parse one rule line into a BriefRule, or return None if not matching."""
    s = line.strip()
    if not s or s in ("暂无", "None", "-"):
        return None
    match = _RULE_PATTERN.match(s)
    if not match:
        return None
    when_part = match.group("when").strip()
    do_part = match.group("do").strip()
    approval_part = match.group("approval")
    # Clean possible trailing bracket on do_part if regex matched partial
    if do_part.endswith("]") and "[" in do_part and not approval_part:
        # Check if approval was inside do_part
        m_app = re.search(r"\[approval:\s*(auto|ask)\]$", do_part, re.IGNORECASE)
        if m_app:
            approval_part = m_app.group(1)
            do_part = do_part[: m_app.start()].strip()
    approval = approval_part.lower() if approval_part else "ask"
    return BriefRule(when=when_part, do=do_part, approval=approval)


@dataclass
class ProjectBrief:
    """Structured Brief for a project."""

    goal_scope: str = ""
    key_files: list[str] = field(default_factory=list)
    rules: list[BriefRule] = field(default_factory=list)
    active_tasks: str = ""
    conventions: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_scope": self.goal_scope,
            "key_files": list(self.key_files),
            "rules": [r.to_dict() for r in self.rules],
            "active_tasks": self.active_tasks,
            "conventions": self.conventions,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectBrief:
        rules_raw = data.get("rules") or []
        rules: list[BriefRule] = []
        for r in rules_raw:
            if isinstance(r, BriefRule):
                rules.append(r)
            elif isinstance(r, dict):
                rules.append(BriefRule.from_dict(r))
            elif isinstance(r, str):
                parsed = parse_rule_line(r)
                if parsed:
                    rules.append(parsed)
        return cls(
            goal_scope=str(data.get("goal_scope") or "").strip(),
            key_files=[str(f).strip() for f in (data.get("key_files") or []) if str(f).strip()],
            rules=rules,
            active_tasks=str(data.get("active_tasks") or "").strip(),
            conventions=str(data.get("conventions") or "").strip(),
            updated_at=str(data.get("updated_at") or "").strip(),
        )


def _classify_heading(heading: str) -> int:
    """Classify heading into section index 1..5, or 0 if unknown."""
    h = heading.lower().strip()
    if any(k in h for k in ("目标", "范围", "goal", "scope")):
        return 1
    if any(k in h for k in ("文件", "file", "artifacts", "key files")):
        return 2
    if any(k in h for k in ("规则", "rule")):
        return 3
    if any(k in h for k in ("任务", "阻塞", "task", "blocker", "status")):
        return 4
    if any(k in h for k in ("约定", "决策", "convention", "decision", "lesson")):
        return 5
    return 0


def parse_brief(md: str, decisions_md: str = "") -> ProjectBrief:
    """Parse PROJECT_STATE.md into a ProjectBrief, with graceful corruption fallback."""
    if not md or not md.strip():
        return ProjectBrief(
            conventions=decisions_md.strip() if decisions_md else "",
            updated_at=_now_iso(),
        )

    lines = md.splitlines()
    updated_at = ""
    for line in lines[:10]:
        if line.lower().startswith("updated:"):
            updated_at = line.split(":", 1)[1].strip()
            break

    # Extract sections
    sections: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: [], 5: []}
    current_sec = 0
    has_headings = False

    for line in lines:
        stripped = line.strip()
        # Only level-2 headings ("## Heading") define top-level sections; level-3 ("###") are content
        if re.match(r"^##\s+", stripped):
            has_headings = True
            heading_title = re.sub(r"^##\s+", "", stripped).strip()
            current_sec = _classify_heading(heading_title)
            continue
        if current_sec in sections:
            sections[current_sec].append(line)

    # Corrupt fallback: if no headings at all, treat the whole markdown as goal_scope
    if not has_headings:
        return ProjectBrief(
            goal_scope=md.strip(),
            conventions=decisions_md.strip() if decisions_md else "",
            updated_at=updated_at or _now_iso(),
        )

    # 1. Goal & Scope
    sec1_text = "\n".join(sections[1]).strip()
    if sec1_text in ("暂无目标描述。", "暂无", "None"):
        sec1_text = ""

    # 2. Key Files
    key_files: list[str] = []
    for line in sections[2]:
        s = line.strip()
        if not s or s in ("暂无", "None", "-"):
            continue
        cleaned = re.sub(r"^[-*]\s*", "", s).strip()
        if cleaned and cleaned not in ("暂无", "None"):
            key_files.append(cleaned)

    # 3. Rules
    rules: list[BriefRule] = []
    for line in sections[3]:
        r = parse_rule_line(line)
        if r:
            rules.append(r)

    # 4. Active Tasks
    sec4_text = "\n".join(sections[4]).strip()
    if sec4_text in ("暂无进行中任务。", "暂无", "None"):
        sec4_text = ""

    # 5. Conventions
    sec5_text = "\n".join(sections[5]).strip()
    if sec5_text in ("暂无约定。", "暂无", "None"):
        sec5_text = ""
    if not sec5_text and decisions_md:
        sec5_text = decisions_md.strip()

    return ProjectBrief(
        goal_scope=sec1_text,
        key_files=key_files,
        rules=rules,
        active_tasks=sec4_text,
        conventions=sec5_text,
        updated_at=updated_at or _now_iso(),
    )


def render_brief(brief: ProjectBrief) -> str:
    """Render ProjectBrief to standardized PROJECT_STATE.md markdown."""
    ts = brief.updated_at or _now_iso()
    files_str = "\n".join(f"- {f}" for f in brief.key_files) if brief.key_files else "暂无"
    rules_str = "\n".join(r.render() for r in brief.rules) if brief.rules else "暂无"
    tasks_str = brief.active_tasks.strip() or "暂无进行中任务。"
    conventions_str = brief.conventions.strip() or "暂无约定。"
    goal_str = brief.goal_scope.strip() or "暂无目标描述。"

    return (
        f"# Project State\n"
        f"Updated: {ts}\n\n"
        f"## 1. 目标与范围\n"
        f"{goal_str}\n\n"
        f"## 2. 重要文件\n"
        f"{files_str}\n\n"
        f"## 3. 规则\n"
        f"{rules_str}\n\n"
        f"## 4. 进行中任务与阻塞\n"
        f"{tasks_str}\n\n"
        f"## 5. 约定\n"
        f"{conventions_str}\n"
    )


def load_brief(project_root: str | Path) -> ProjectBrief:
    """Load brief from project_root/.veya-project/PROJECT_STATE.md and update brief.json mirror."""
    store = ProjectStore(project_root)
    store.ensure_layout()
    state_md = store.read_state()
    decisions_md = store.read_decisions()
    brief = parse_brief(state_md, decisions_md)
    # Sync to brief.json mirror
    brief_json_path = store.dir / _BRIEF_JSON
    with contextlib.suppress(Exception):
        atomic_write_json(brief_json_path, brief.to_dict())
    return brief


def save_brief(project_root: str | Path, brief: ProjectBrief) -> None:
    """Save brief to PROJECT_STATE.md and mirror to brief.json."""
    store = ProjectStore(project_root)
    store.ensure_layout()
    brief.updated_at = _now_iso()
    md = render_brief(brief)
    store.write_state(md)
    brief_json_path = store.dir / _BRIEF_JSON
    atomic_write_json(brief_json_path, brief.to_dict())


def update_brief_section(
    project_root: str | Path,
    section: str,
    content: Any,
) -> ProjectBrief:
    """Update one section in brief and persist immediately."""
    brief = load_brief(project_root)
    sec = section.strip().lower()
    if sec in ("goal_scope", "goal", "scope", "1", "目标", "范围", "目标与范围"):
        brief.goal_scope = str(content).strip()
    elif sec in ("key_files", "files", "2", "重要文件", "文件"):
        if isinstance(content, list):
            brief.key_files = [str(x).strip() for x in content if str(x).strip()]
        elif isinstance(content, str):
            lines = [line.strip().lstrip("-* ").strip() for line in content.splitlines()]
            brief.key_files = [line for line in lines if line and line not in ("暂无", "None")]
    elif sec in ("rules", "3", "规则"):
        new_rules: list[BriefRule] = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, BriefRule):
                    new_rules.append(item)
                elif isinstance(item, dict):
                    new_rules.append(BriefRule.from_dict(item))
                elif isinstance(item, str):
                    r = parse_rule_line(item)
                    if r:
                        new_rules.append(r)
        elif isinstance(content, str):
            for line in content.splitlines():
                r = parse_rule_line(line)
                if r:
                    new_rules.append(r)
        brief.rules = new_rules
    elif sec in ("active_tasks", "tasks", "4", "进行中任务与阻塞", "任务", "阻塞"):
        brief.active_tasks = str(content).strip()
    elif sec in ("conventions", "decisions", "5", "约定", "决策"):
        brief.conventions = str(content).strip()
    save_brief(project_root, brief)
    return brief
