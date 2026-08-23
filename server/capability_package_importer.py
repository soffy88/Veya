"""server.capability_package_importer — Generic Capability Package Importer（P6，PR-30）。

对标 2.0 文档 §6.2「Capability Package 标准」（Impeccable 风格的目录格式，见
`docs/Veya_Evolvable_Agent_Runtime_Architecture_v2.0.docx`）：

    <package>/
    ├── CAPABILITY.yaml   # 能力元数据(id/domain/description/can_do/…)
    ├── skills/*.yaml     # 每个技能一份声明式定义
    ├── knowledge/*.md    # 领域知识文档
    ├── commands/         # (仅记录目录存在, 不解析内容)
    ├── evaluators/       # 确定性/领域评估器脚本或配置(仅记录路径引用)
    ├── benchmarks/       # 评测套件(仅记录路径引用)
    └── adapters/         # 外部系统适配器(仅记录路径引用)

范围边界（2026-08-23，见 docs/VEYA_3.0_GAP_AUDIT.md §6）：

- 这是一个**格式已知、可测试**的解析器——Impeccable 的目录结构在 2.0 文档里
  写得很具体，本模块解析的是这个真实存在的规范，不是凭空发明。测试用构造的
  fixture 目录验证（跟写一个 JSON 解析器、拿样例 JSON 测试是同一类工程实践）。
- **不包含 PR-26/27/28（UHP/Memvid/DeerFlow/LongHorizon Adapter）**——那几个
  需要真实协议规范/真实系统实例才能写出有意义的适配代码，本仓库/本环境里
  一个都没有。写"适配器"类但连接不到任何真实系统，是伪造能力，不是保守，
  拒绝这么做是质量底线，不是等客户上门。真要接哪一个，需要用户提供该系统的
  真实规范/仓库/可达实例作为输入。
- **PR-29（Agency/Cookbook Importer）已经存在，不是本模块新建的**——
  `scripts/convert_agency_skills.py` 早就是这个角色（agency-agents md → veya
  skill），转换产物落进 `~/.veya/skills/`，`server/capability_model.py::
  sync_skills_from_hub()`（P2）已经把这些技能桥接进 SkillRegistry。本模块不
  重复这条路径。
- `commands/`/`evaluators/`/`benchmarks/`/`adapters/` 目录内容异构（Python 脚本、
  yaml 配置都可能有），只记录文件路径引用（`CapabilityPackage.evaluator_refs`
  等），不解析内容——解析需要先知道每种 evaluator 的具体格式，不该在导入器
  这层假装能读懂它们。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from server.capability_model import (
    CapabilityPackage,
    CapabilitySpec,
    KnowledgePack,
    SkillSpec,
    capability_registry,
    knowledge_registry,
    skill_registry,
)


def _list_refs(dir_path: Path) -> list[str]:
    if not dir_path.is_dir():
        return []
    return [str(p) for p in sorted(dir_path.iterdir()) if p.is_file()]


def import_capability_package(package_dir: str | Path) -> CapabilityPackage:
    """解析并注册一个 Impeccable 风格的 Capability Package 目录。

    返回聚合后的 CapabilityPackage(引用刚注册的 skill_ids/knowledge_ids)。
    目录缺 CAPABILITY.yaml 直接抛 ValueError——那是这个格式唯一的硬性入口
    文件, 缺了就不是一个合法的 package, 不该静默跳过或猜测。
    """
    package_dir = Path(package_dir)
    manifest_path = package_dir / "CAPABILITY.yaml"
    if not manifest_path.is_file():
        raise ValueError(f"not a capability package (missing CAPABILITY.yaml): {package_dir}")

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    package_id = str(manifest.get("id") or package_dir.name)
    domain = str(manifest.get("domain") or "")

    skill_ids = _import_skills(package_dir / "skills", package_id)
    knowledge_ids = _import_knowledge(package_dir / "knowledge", package_id, domain)
    evaluator_refs = _list_refs(package_dir / "evaluators")
    benchmark_refs = _list_refs(package_dir / "benchmarks")
    adapter_refs = _list_refs(package_dir / "adapters")

    capability_registry.register_candidate(
        CapabilitySpec(
            capability_id=package_id,
            domain=domain,
            description=str(manifest.get("description") or ""),
            can_do=list(manifest.get("can_do") or []),
            cannot_do=list(manifest.get("cannot_do") or []),
            risk_level=str(manifest.get("risk_level") or "unknown"),
            skills=skill_ids,
            knowledge_packs=knowledge_ids,
            evaluators=evaluator_refs,
            provenance=f"capability_package_importer:{package_dir}",
        )
    )

    return CapabilityPackage(
        package_id=package_id,
        capability_ids=[package_id],
        skill_ids=skill_ids,
        knowledge_ids=knowledge_ids,
        evaluator_refs=evaluator_refs,
        benchmark_refs=benchmark_refs,
        adapter_refs=adapter_refs,
    )


def _import_skills(skills_dir: Path, package_id: str) -> list[str]:
    if not skills_dir.is_dir():
        return []
    skill_ids = []
    for skill_file in sorted(skills_dir.glob("*.yaml")):
        data = yaml.safe_load(skill_file.read_text(encoding="utf-8")) or {}
        skill_id = f"{package_id}.{skill_file.stem}"
        skill_registry.register_candidate(
            SkillSpec(
                skill_id=skill_id,
                instructions=str(data.get("instructions") or data.get("description") or ""),
                applicable_when=list(data.get("applicable_when") or []),
                not_applicable_when=list(data.get("not_applicable_when") or []),
                provenance=f"capability_package_importer:{skill_file}",
            )
        )
        skill_ids.append(skill_id)
    return skill_ids


def _import_knowledge(knowledge_dir: Path, package_id: str, domain: str) -> list[str]:
    if not knowledge_dir.is_dir():
        return []
    knowledge_ids = []
    for md_file in sorted(knowledge_dir.glob("*.md")):
        knowledge_id = f"{package_id}.{md_file.stem}"
        knowledge_registry.import_pack(
            KnowledgePack(
                knowledge_id=knowledge_id,
                title=md_file.stem,
                domain=domain,
                content_refs=[str(md_file)],
                source=str(md_file),
                provenance=f"capability_package_importer:{md_file}",
            )
        )
        knowledge_ids.append(knowledge_id)
    return knowledge_ids
