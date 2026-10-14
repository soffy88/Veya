# Source: https://github.com/PenglongHuang/chinese-novelist-skill; License: MIT; Copyright (c) 2026 PenglongHuang. Adapted for Veya.
# Source: https://github.com/PenglongHuang/chinese-novelist-skill
# License: MIT; Copyright (c) 2026 PenglongHuang
"""Read-only discovery entry point for the Chinese novelist skill package."""

from __future__ import annotations

from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REFERENCE_FILES = {
    "phase0-initialization": "references/flows/phase0-initialization.md",
    "phase1-layer1-core": "references/flows/phase1-layer1-core.md",
    "phase1-layer2-customize": "references/flows/phase1-layer2-customize.md",
    "phase1-layer3-title": "references/flows/phase1-layer3-title.md",
    "phase2-planning": "references/flows/phase2-planning.md",
    "phase3-writing": "references/flows/phase3-writing.md",
    "phase4-validation": "references/flows/phase4-validation.md",
    "shared-infrastructure": "references/flows/shared-infrastructure.md",
    "chapter-guide": "references/guides/chapter-guide.md",
    "chapter-template": "references/guides/chapter-template.md",
    "character-building": "references/guides/character-building.md",
    "character-template": "references/guides/character-template.md",
    "content-expansion": "references/guides/content-expansion.md",
    "dialogue-writing": "references/guides/dialogue-writing.md",
    "hook-techniques": "references/guides/hook-techniques.md",
    "outline-template": "references/guides/outline-template.md",
    "plot-structures": "references/guides/plot-structures.md",
    "title-guide": "references/guides/title-guide.md",
}


def main(
    action: str = "overview",
    reference: str = "",
    text: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Return bundled guidance or count Chinese Han characters; never writes files."""
    if action == "overview":
        path = ROOT / "SKILL.md"
        return {"ok": True, "action": action, "content": path.read_text(encoding="utf-8")}
    if action == "list_references":
        return {"ok": True, "action": action, "references": sorted(REFERENCE_FILES)}
    if action == "reference":
        relative = REFERENCE_FILES.get(reference)
        if relative is None:
            return {"ok": False, "error": "unknown reference", "references": sorted(REFERENCE_FILES)}
        return {
            "ok": True,
            "action": action,
            "reference": reference,
            "content": (ROOT / relative).read_text(encoding="utf-8"),
        }
    if action == "word_count_text":
        count = sum("\u4e00" <= char <= "\u9fff" for char in text)
        return {"ok": True, "action": action, "han_character_count": count}
    return {
        "ok": False,
        "error": "unknown action",
        "actions": ["overview", "list_references", "reference", "word_count_text"],
    }
