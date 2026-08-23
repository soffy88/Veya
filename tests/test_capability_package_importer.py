"""server.capability_package_importer 测试(VAOM P6 Generic Capability Package
Importer, PR-30, 见 docs/dev/rfc-01-vaom.md)。用构造的 fixture 目录验证解析
Impeccable 风格目录格式(2.0 文档 §6.2), 不碰真实 ~/.veya 文件。"""

from __future__ import annotations

import pytest

from server.capability_model import (
    CapabilityRegistry,
    KnowledgeRegistry,
    SkillRegistry,
    _JsonRegistryStore,
)
from server.capability_package_importer import import_capability_package


def _isolated_registries(tmp_path, monkeypatch):
    store = _JsonRegistryStore(storage_path=tmp_path / "registry.json")
    cap = CapabilityRegistry(store)
    skill = SkillRegistry(store)
    knowledge = KnowledgeRegistry(store)
    monkeypatch.setattr("server.capability_package_importer.capability_registry", cap)
    monkeypatch.setattr("server.capability_package_importer.skill_registry", skill)
    monkeypatch.setattr("server.capability_package_importer.knowledge_registry", knowledge)
    return cap, skill, knowledge


def _write_package(root, *, with_skills=True, with_knowledge=True, with_extras=True):
    root.mkdir(parents=True, exist_ok=True)
    (root / "CAPABILITY.yaml").write_text(
        "id: frontend-design\n"
        "domain: frontend\n"
        "description: critique/polish/harden frontend code\n"
        "can_do:\n  - review CSS\n  - accessibility audit\n"
        "cannot_do:\n  - write backend code\n"
        "risk_level: low\n",
        encoding="utf-8",
    )
    if with_skills:
        skills_dir = root / "skills"
        skills_dir.mkdir()
        (skills_dir / "critique.yaml").write_text(
            "instructions: review a diff against design heuristics\n"
            "applicable_when:\n  - CSS or component diff\n",
            encoding="utf-8",
        )
        (skills_dir / "polish.yaml").write_text(
            "description: tighten spacing/typography\n",  # 无 instructions, 走 fallback
            encoding="utf-8",
        )
    if with_knowledge:
        knowledge_dir = root / "knowledge"
        knowledge_dir.mkdir()
        (knowledge_dir / "typography.md").write_text("# Typography\n...", encoding="utf-8")
        (knowledge_dir / "color.md").write_text("# Color\n...", encoding="utf-8")
    if with_extras:
        (root / "evaluators").mkdir()
        (root / "evaluators" / "deterministic.py").write_text("# stub", encoding="utf-8")
        (root / "benchmarks").mkdir()
        (root / "benchmarks" / "suite.yaml").write_text("cases: []", encoding="utf-8")
        (root / "adapters").mkdir()
        (root / "adapters" / "cli.py").write_text("# stub", encoding="utf-8")
    return root


def test_import_full_package(tmp_path, monkeypatch):
    cap, skill, knowledge = _isolated_registries(tmp_path, monkeypatch)
    pkg_dir = _write_package(tmp_path / "frontend-design")

    package = import_capability_package(pkg_dir)

    assert package.package_id == "frontend-design"
    assert package.skill_ids == ["frontend-design.critique", "frontend-design.polish"]
    assert package.knowledge_ids == ["frontend-design.color", "frontend-design.typography"]
    assert len(package.evaluator_refs) == 1
    assert len(package.benchmark_refs) == 1
    assert len(package.adapter_refs) == 1

    cap_spec = cap.get("frontend-design")
    assert cap_spec is not None
    assert cap_spec.domain == "frontend"
    assert cap_spec.can_do == ["review CSS", "accessibility audit"]
    assert cap_spec.status == "candidate"

    critique = skill.get_version("frontend-design.critique")
    assert critique.instructions == "review a diff against design heuristics"
    polish = skill.get_version("frontend-design.polish")
    assert polish.instructions == "tighten spacing/typography"  # description fallback

    typography = knowledge.search("typography")
    assert len(typography) == 1
    assert typography[0].title == "typography"


def test_import_missing_manifest_raises(tmp_path, monkeypatch):
    _isolated_registries(tmp_path, monkeypatch)
    empty_dir = tmp_path / "not-a-package"
    empty_dir.mkdir()

    with pytest.raises(ValueError, match=r"CAPABILITY\.yaml"):
        import_capability_package(empty_dir)


def test_import_minimal_package_no_skills_or_knowledge(tmp_path, monkeypatch):
    _isolated_registries(tmp_path, monkeypatch)
    pkg_dir = _write_package(
        tmp_path / "minimal", with_skills=False, with_knowledge=False, with_extras=False
    )

    package = import_capability_package(pkg_dir)

    assert package.skill_ids == []
    assert package.knowledge_ids == []
    assert package.evaluator_refs == []
    assert package.benchmark_refs == []
    assert package.adapter_refs == []


def test_import_package_id_defaults_to_dir_name_when_missing(tmp_path, monkeypatch):
    _isolated_registries(tmp_path, monkeypatch)
    pkg_dir = tmp_path / "my-package"
    pkg_dir.mkdir()
    (pkg_dir / "CAPABILITY.yaml").write_text("description: no id field\n", encoding="utf-8")

    package = import_capability_package(pkg_dir)

    assert package.package_id == "my-package"
