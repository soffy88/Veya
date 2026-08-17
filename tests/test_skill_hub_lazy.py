"""SkillHub 渐进加载 (#2): SKILL.md body 命中才拉 + 常驻目录 cap。"""

from __future__ import annotations

import json
from pathlib import Path

from server.skill_hub import VeyaSkillHub


def _write_skill(skills_dir: Path, name: str, *, description: str, skill_md: str | None = None):
    pkg = skills_dir / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": description,
                "type": "python",
                "entrypoint": "run.py",
                "parameters": {"type": "object", "properties": {"goal": {"type": "string"}}},
            }
        ),
        encoding="utf-8",
    )
    (pkg / "run.py").write_text("def main(**kwargs):\n    return 'ok'\n", encoding="utf-8")
    if skill_md is not None:
        (pkg / "SKILL.md").write_text(skill_md, encoding="utf-8")


_SKILL_MD = """---
name: refactor_py
description: refactor python code
tags: [python, refactor]
---
# Refactor guide
Step 1: locate the long function.
Step 2: extract cohesive helpers.
"""


def test_has_body_flag(tmp_path):
    _write_skill(tmp_path, "with_md", description="d", skill_md=_SKILL_MD)
    _write_skill(tmp_path, "no_md", description="d")
    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert hub._skills["with_md"]["has_body"] is True
    assert hub._skills["no_md"]["has_body"] is False


def test_body_absent_from_static_catalog(tmp_path):
    _write_skill(tmp_path, "refactor_py", description="refactor python code", skill_md=_SKILL_MD)
    hub = VeyaSkillHub(skills_dir=tmp_path)
    run_skill = next(s for s in hub.get_all_schemas() if s["function"]["name"] == "run_skill")
    # 常驻目录不含 SKILL.md body (省 token) —— body 只在路由命中时拉
    assert "Step 1" not in run_skill["function"]["description"]


async def test_body_pulled_only_on_routed_match(tmp_path):
    _write_skill(tmp_path, "refactor_py", description="refactor python code", skill_md=_SKILL_MD)
    _write_skill(tmp_path, "weather", description="get the weather")
    hub = VeyaSkillHub(skills_dir=tmp_path)

    out = json.loads(await hub.execute("list_skills", {"task": "refactor this python function"}))
    top = out[0]
    assert top["name"] == "refactor_py"
    assert "body" in top and "Step 1" in top["body"]  # 命中才拉 body


def test_catalog_capped_when_many_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_SKILL_CATALOG_CAP", "3")
    for i in range(6):
        _write_skill(tmp_path, f"skill_{i}", description=f"desc {i}")
    hub = VeyaSkillHub(skills_dir=tmp_path)
    desc = next(s for s in hub.get_all_schemas() if s["function"]["name"] == "run_skill")[
        "function"
    ]["description"]
    assert "+3 more skills" in desc
    assert "list_skills(task=" in desc
