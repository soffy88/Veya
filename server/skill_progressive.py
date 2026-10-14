# 3O-IO-ALLOW — server/skill_progressive.py
# 理由：只读加载预设技能文件（config/presets/skills/**/SKILL.md）；不写盘、不起进程。
# 同形先例：server/role_skill_presets.py 读 config/presets/**.md。
"""server/skill_progressive — Clean-room progressive skill disclosure.

Flag: ``VEYA_SKILL_PROGRESSIVE`` (default off).

Instead of dumping full SKILL.md contents of all available skills into the bot
prompt (wasting thousands of context tokens), progressive disclosure places only
the skill ID, name, and a one-line summary into an `<available_skills>` block.
When the model determines it needs a skill to complete a task, it loads the full
instructions on demand via the read-only ``read_skill(skill_id)`` tool.

Reuses existing W15 presets (``config/presets/skills/**/SKILL.md``),
Skills V2 / SkillSpec (``server.capability_model``), and ``server.skill_hub``.
No second registry or authority is introduced.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from server.feature_flags import enabled

FLAG_NAME = "VEYA_SKILL_PROGRESSIVE"
ALT_FLAG_NAME = "VEYA_SKILL_PROGRESSIVE_DISCLOSURE"


def is_skill_progressive_enabled() -> bool:
    """Return True if progressive skill disclosure is enabled."""
    try:
        if enabled(FLAG_NAME):
            return True
    except KeyError:
        pass
    alt = os.environ.get(ALT_FLAG_NAME, "").strip().lower()
    return alt in ("1", "true", "yes", "on")


def _clean_skill_id(skill_id: str) -> str:
    sid = str(skill_id or "").strip()
    if sid.startswith("preset:"):
        return sid[len("preset:") :]
    return sid


def get_skill_info(skill_id: str) -> dict[str, str]:
    """Retrieve skill metadata and body from presets, Skills V2, or skill_hub.

    Returns dict with keys: id, name, summary, content.
    Raises KeyError if the skill cannot be resolved.
    """
    raw_id = str(skill_id or "").strip()
    if not raw_id:
        raise KeyError("skill_id is required")
    clean_id = _clean_skill_id(raw_id)

    # 1. Try W15 presets
    try:
        from server import role_skill_presets as presets

        preset = presets.get_skill(clean_id)
        return {
            "id": raw_id,
            "name": preset.name,
            "summary": preset.summary_zh or preset.description or preset.name,
            "content": preset.body,
        }
    except Exception:
        pass

    # 2. Try Skills V2 / capability model
    try:
        from server.capability_model import skill_registry

        spec = skill_registry.get(clean_id)
        if spec is not None:
            doc = (
                getattr(spec, "doc", "")
                or getattr(spec, "body", "")
                or getattr(spec, "description", "")
            )
            return {
                "id": raw_id,
                "name": getattr(spec, "name", clean_id) or clean_id,
                "summary": getattr(spec, "description", "") or getattr(spec, "name", clean_id),
                "content": doc,
            }
    except Exception:
        pass

    # 3. Try skill_hub
    try:
        from server.skill_hub import skill_hub as default_hub

        if default_hub.has(clean_id):
            desc = default_hub.describe(clean_id)
            src = default_hub.skill_source_path(clean_id)
            body = (
                Path(src).read_text(encoding="utf-8", errors="replace")
                if src and Path(src).is_file()
                else desc
            )
            return {
                "id": raw_id,
                "name": clean_id,
                "summary": desc or clean_id,
                "content": body,
            }
    except Exception:
        pass

    raise KeyError(f"Skill {raw_id!r} not found in presets or capability registry")


def read_skill(skill_id: str) -> str:
    """Tool callable: load full SKILL.md body for an available skill."""
    try:
        info = get_skill_info(skill_id)
        return f"# Skill: {info['name']} ({info['id']})\n\n{info['content']}"
    except KeyError:
        return f"Error: Skill {skill_id!r} not found in presets or skill registry."
    except Exception as exc:
        return f"Error reading skill {skill_id!r}: {exc}"


def render_skills_block(skill_ids: list[str], progressive: bool = True) -> str:
    """Render a skill block for objective / system prompt.

    When progressive=True, emits only metadata summaries in <available_skills>.
    When progressive=False, dumps full SKILL.md bodies for all skills.
    """
    valid_infos: list[dict[str, str]] = []
    for sid in skill_ids or []:
        try:
            valid_infos.append(get_skill_info(sid))
        except KeyError:
            clean = _clean_skill_id(sid)
            valid_infos.append(
                {
                    "id": sid,
                    "name": clean,
                    "summary": clean,
                    "content": f"# {clean}\n\nSkill instructions placeholder.",
                }
            )

    if not valid_infos:
        return ""

    if progressive:
        lines = [
            "<available_skills>",
            "When you need detailed guidance, procedures, or examples for a skill, call `read_skill(skill_id)` to load its full instructions on demand.",
        ]
        for info in valid_infos:
            lines.append(f"- {info['id']} ({info['name']}): {info['summary']}")
        lines.append("</available_skills>")
        return "\n".join(lines)
    else:
        sections = ["## Available Skills (Full Instructions)"]
        for info in valid_infos:
            sections.append(f"### Skill: {info['name']} ({info['id']})\n\n{info['content']}")
        return "\n\n".join(sections)


def measure_prompt_tokens(prompt: str) -> int:
    """Estimate token count of a prompt using canonical Veya CJK/Latin heuristics."""
    try:
        from veya.oskill.pure.context_compress import estimate_tokens

        return estimate_tokens(prompt)
    except Exception:
        # Fallback heuristic: 1 CJK char = 1 token, other 4 chars = 1 token
        cjk = sum(1 for ch in prompt if "\u4e00" <= ch <= "\u9fff")
        other = len(prompt) - cjk
        return max(1, cjk + (other + 3) // 4)


def compare_skills_tokens(skill_ids: list[str], base_prompt: str = "") -> dict[str, Any]:
    """Measure prompt token counts before and after progressive skill disclosure."""
    full_block = render_skills_block(skill_ids, progressive=False)
    prog_block = render_skills_block(skill_ids, progressive=True)

    prompt_before = f"{base_prompt}\n\n{full_block}".strip() if base_prompt else full_block
    prompt_after = f"{base_prompt}\n\n{prog_block}".strip() if base_prompt else prog_block

    tokens_before = measure_prompt_tokens(prompt_before)
    tokens_after = measure_prompt_tokens(prompt_after)
    saved = max(0, tokens_before - tokens_after)
    reduction = round((saved / max(1, tokens_before)) * 100, 1)

    return {
        "skill_count": len(skill_ids),
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "tokens_saved": saved,
        "reduction_percentage": reduction,
    }
