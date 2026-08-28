"""Project guide loading for the Veya Agent harness.

Guides are read-only context.  Every durable rule must point back to a real
source path and line; this prevents an LLM suggestion from silently becoming a
permanent workspace policy.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .models import AntiPattern, GuideCommands, GuideConflict, GuideRule, ProjectGuide

GUIDE_FILENAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    ".veya/GUIDES.md",
    ".veya/project_rules.md",
)

_COMMAND_KEYS = {
    "build": "build",
    "test": "test",
    "tests": "test",
    "lint": "lint",
    "typecheck": "typecheck",
    "type-check": "typecheck",
    "format": "format",
    "formatter": "format",
}
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_FIELD_LINE = re.compile(
    r"^\s*(build|test|tests|lint|typecheck|type-check|format)\s*:\s*(.+)$", re.I
)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BULLET = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|\[[ xX]\]\s+)?(.*)$")
_NEGATIVE = re.compile(r"(?i)\b(?:never|don't|do not|must not|avoid|forbid|禁止|不要|不得|严禁)\b")
_POSITIVE = re.compile(r"(?i)\b(?:always|must|should|use|require|总是|必须|应当|要求)\b")
_COMMAND_WORDS = re.compile(r"(?i)\b(?:pytest|ruff|mypy|tsc|npm|pnpm|yarn|bun|cargo|go|make)\b")


def _workspace_root(value: str | Path | object) -> tuple[Path, str]:
    root_value = getattr(value, "root_path", value)
    candidate = Path(str(root_value)).expanduser().resolve()
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError(f"workspace path is not a directory: {candidate}")
    for ancestor in (candidate, *candidate.parents):
        if (ancestor / ".git").exists():
            candidate = ancestor
            break
    workspace_id = str(getattr(value, "id", "")) or (
        "workspace-" + hashlib.sha256(str(candidate).encode("utf-8")).hexdigest()[:12]
    )
    return candidate, workspace_id


def guide_paths(root: str | Path) -> list[Path]:
    project_root = Path(root).expanduser().resolve()
    return [
        project_root / relative
        for relative in GUIDE_FILENAMES
        if (project_root / relative).is_file()
    ]


def _clean_line(line: str) -> str:
    match = _BULLET.match(line)
    return match.group(1).strip() if match else line.strip()


def _category(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized or "general"


def _command_key(value: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9-]+", "", value.lower())
    for alias, key in _COMMAND_KEYS.items():
        if normalized == re.sub(r"[^a-z0-9-]+", "", alias):
            return key
    return None


def _command_value(line: str, section: str | None) -> tuple[str, str] | None:
    cleaned_line = _clean_line(line)
    field = _FIELD_LINE.match(cleaned_line)
    if field:
        key = _command_key(field.group(1))
        if key:
            return key, field.group(2).strip().strip("`")
    key = _command_key(section or "")
    if not key:
        return None
    values = _INLINE_CODE.findall(line)
    if values:
        return key, values[0].strip()
    cleaned = cleaned_line.strip("`").strip()
    if not cleaned or cleaned.startswith("#"):
        return None
    # A command section may contain one command per line.  Keep prose out of
    # the command list unless it begins with a conventional executable.
    if _COMMAND_WORDS.search(cleaned) or re.match(r"^[A-Za-z0-9_.-]+(?:\s|$)", cleaned):
        return key, cleaned
    return None


def _rule_priority(text: str) -> int:
    if _NEGATIVE.search(text) or re.search(r"(?i)\b(?:must|shall|required|必须|不得)\b", text):
        return 100
    if re.search(r"(?i)\b(?:should|建议|应当)\b", text):
        return 70
    return 50


def _rule_id(source_path: Path, line_number: int, text: str) -> str:
    raw = f"{source_path}:{line_number}:{text}".encode()
    return "guide-rule-" + hashlib.sha256(raw).hexdigest()[:16]


def _parse_guide(path: Path, workspace_id: str) -> ProjectGuide:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"cannot read guide {path}: {exc}") from exc

    commands = GuideCommands()
    rules: list[GuideRule] = []
    anti_patterns: list[AntiPattern] = []
    current_category = "general"
    current_command: str | None = None
    in_fence = False

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        heading = _HEADING.match(raw_line)
        if heading:
            title = heading.group(1).strip()
            current_category = _category(title)
            current_command = _command_key(title)
            continue
        if not line or line.startswith("<!--") or (line.startswith("|") and line.endswith("|")):
            continue

        command_value = _command_value(raw_line, current_command)
        if command_value:
            key, value = command_value
            values = getattr(commands, key)
            if value not in values:
                values.append(value)
            if in_fence or current_command or _FIELD_LINE.match(raw_line):
                continue

        if in_fence:
            continue
        rule_text = _clean_line(raw_line)
        if not rule_text or rule_text.startswith("---") or re.fullmatch(r"[-=*_ ]+", rule_text):
            continue
        rule = GuideRule(
            id=_rule_id(path, line_number, rule_text),
            text=rule_text,
            category=current_category,
            source_path=str(path),
            source_line=line_number,
            priority=_rule_priority(rule_text),
            verifiable=bool(_INLINE_CODE.search(rule_text) or _COMMAND_WORDS.search(rule_text)),
        )
        rules.append(rule)
        if current_category in {
            "anti_patterns",
            "antipatterns",
            "do_not",
            "dont",
        } or _NEGATIVE.search(rule_text):
            anti_patterns.append(
                AntiPattern(
                    id="anti-pattern-" + rule.id.removeprefix("guide-rule-"),
                    text=rule_text,
                    source_path=str(path),
                    source_line=line_number,
                    category=current_category,
                )
            )

    return ProjectGuide(
        workspace_id=workspace_id,
        source_path=str(path),
        rules=rules,
        commands=commands,
        anti_patterns=anti_patterns,
        last_loaded_at=datetime.now(UTC),
    )


def load_guides(workspace: str | Path | object) -> list[ProjectGuide]:
    """Load supported root-level project guides without persisting context."""
    root, workspace_id = _workspace_root(workspace)
    return [_parse_guide(path, workspace_id) for path in guide_paths(root)]


def guide_conflicts(guides: Iterable[ProjectGuide]) -> list[GuideConflict]:
    rules = [rule for guide in guides for rule in guide.rules]
    conflicts: list[GuideConflict] = []
    for index, left in enumerate(rules):
        for right in rules[index + 1 :]:
            if (
                left.category != right.category
                and left.category != "general"
                and right.category != "general"
            ):
                continue
            left_negative = bool(_NEGATIVE.search(left.text))
            right_negative = bool(_NEGATIVE.search(right.text))
            if left_negative == right_negative:
                continue
            left_subject = _NEGATIVE.sub("", left.text).lower()
            right_subject = _NEGATIVE.sub("", right.text).lower()
            left_tokens = set(re.findall(r"[a-z0-9_]+", left_subject))
            right_tokens = set(re.findall(r"[a-z0-9_]+", right_subject))
            common = left_tokens & right_tokens
            if len(common) < 2:
                continue
            conflicts.append(
                GuideConflict(
                    left_rule_id=left.id,
                    right_rule_id=right.id,
                    message="guide rules have opposing instructions: " + ", ".join(sorted(common)),
                    source_paths=[left.source_path, right.source_path],
                )
            )
    return conflicts


def guide_commands(guides: Iterable[ProjectGuide]) -> GuideCommands:
    result = GuideCommands()
    for guide in guides:
        for key, values in guide.commands.all().items():
            target = getattr(result, key)
            for value in values:
                if value not in target:
                    target.append(value)
    return result


def search_guides(workspace: str | Path | object, query: str) -> list[dict[str, object]]:
    needle = query.strip().lower()
    if not needle:
        return []
    results: list[dict[str, object]] = []
    for guide in load_guides(workspace):
        for rule in guide.rules:
            if needle in rule.text.lower() or needle in rule.category.lower():
                results.append(rule.to_dict())
        for anti_pattern in guide.anti_patterns:
            if needle in anti_pattern.text.lower():
                results.append(anti_pattern.to_dict())
    return results


def show_guide(workspace: str | Path | object, source_path: str | None = None) -> dict[str, object]:
    guides = load_guides(workspace)
    if source_path:
        requested = Path(source_path).expanduser().resolve()
        guides = [guide for guide in guides if Path(guide.source_path).resolve() == requested]
    return {
        "guides": [guide.to_dict() for guide in guides],
        "conflicts": [conflict.to_dict() for conflict in guide_conflicts(guides)],
        "commands": guide_commands(guides).to_dict(),
    }


__all__ = [
    "GUIDE_FILENAMES",
    "guide_commands",
    "guide_conflicts",
    "guide_paths",
    "load_guides",
    "search_guides",
    "show_guide",
]
