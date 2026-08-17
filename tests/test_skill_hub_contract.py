"""SkillHub × 技能契约 + 元路由 (装配 3O oskill.skill_contract / select_skill)。"""

from __future__ import annotations

import json
from pathlib import Path

from server.skill_hub import VeyaSkillHub


def _write_skill(
    skills_dir: Path,
    name: str,
    *,
    description: str,
    contract: dict | None = None,
    code: str = "def main(**kwargs):\n    return 'ok'\n",
) -> None:
    pkg = skills_dir / name
    pkg.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "description": description,
        "type": "python",
        "entrypoint": "run.py",
        "parameters": {"type": "object", "properties": {"goal": {"type": "string"}}},
    }
    if contract:
        manifest.update(contract)
    (pkg / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (pkg / "run.py").write_text(code, encoding="utf-8")


def test_contract_fields_parsed_and_stored(tmp_path):
    _write_skill(
        tmp_path,
        "refactor_py",
        description="refactor python code",
        contract={
            "when_to_use": ["function too long"],
            "verification": ["pytest green"],
            "red_flags": ["no tests"],
        },
    )
    hub = VeyaSkillHub(skills_dir=tmp_path)
    c = hub._contracts["refactor_py"]
    assert c["when_to_use"] == ["function too long"]
    assert c["verification"] == ["pytest green"]


def test_skill_without_contract_stays_backward_compatible(tmp_path):
    _write_skill(tmp_path, "plain", description="a plain skill")
    hub = VeyaSkillHub(skills_dir=tmp_path)
    # 契约为空 (全字段空), 行为不变
    assert hub._contracts["plain"] == {
        "when_to_use": [],
        "verification": [],
        "red_flags": [],
        "rationalizations": [],
    }


async def test_list_skills_task_routes_and_returns_contract(tmp_path):
    _write_skill(
        tmp_path,
        "refactor_py",
        description="refactor python code",
        contract={"when_to_use": ["long function"], "verification": ["pytest green"]},
    )
    _write_skill(tmp_path, "weather", description="get the weather forecast")
    hub = VeyaSkillHub(skills_dir=tmp_path)

    out = json.loads(await hub.execute("list_skills", {"task": "refactor this python function"}))
    assert out, "should return ranked skills"
    assert out[0]["name"] == "refactor_py"  # 元路由把最相关的排前
    assert out[0]["verification"] == ["pytest green"]  # 附证据契约


async def test_list_skills_no_task_lists_all(tmp_path):
    _write_skill(tmp_path, "a", description="skill a")
    _write_skill(tmp_path, "b", description="skill b")
    hub = VeyaSkillHub(skills_dir=tmp_path)
    out = json.loads(await hub.execute("list_skills", {}))
    assert {e["name"] for e in out} == {"a", "b"}


def test_invalid_contract_shape_still_loads(tmp_path):
    # verification 声明成空 → 结构告警, 但技能仍挂载 (向后兼容, 不拒载)
    _write_skill(tmp_path, "loose", description="loose skill", contract={"verification": []})
    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert hub.has("loose")
