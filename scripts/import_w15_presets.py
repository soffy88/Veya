#!/usr/bin/env python3
"""W15 — import role/skill presets from the two MIT upstream repositories.

Reads a shallow clone under ``/tmp/w15-upstream`` (read-only reference, never
committed) and writes the curated, committed preset pack:

* ``config/presets/roles/<id>.md``          — agency-agents role personas
* ``config/presets/skills/<id>/SKILL.md``   — agent-skills engineering skills
  (plus sibling/reference ``*.md`` files; **never** scripts or any other file)

Every generated file starts with a provenance header (HTML comment) that keeps
the upstream source path, commit, license and copyright line, plus a one-line
Chinese summary (``summary_zh``).  Content below the header is the upstream
text verbatim (English).

Usage::

    git clone --depth 1 https://github.com/msitarzewski/agency-agents /tmp/w15-upstream/agency-agents
    git clone --depth 1 https://github.com/addyosmani/agent-skills  /tmp/w15-upstream/agent-skills
    .venv/bin/python scripts/import_w15_presets.py [--upstream /tmp/w15-upstream]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = REPO_ROOT / "config" / "presets"

AGENCY = {
    "repo": "agency-agents",
    "url": "https://github.com/msitarzewski/agency-agents",
    "copyright": "Copyright (c) 2025 AgentLand Contributors",
}
SKILLS = {
    "repo": "agent-skills",
    "url": "https://github.com/addyosmani/agent-skills",
    "copyright": "Copyright (c) 2025 Addy Osmani",
}

# id -> (upstream relative path, category, one-line Chinese summary)
ROLES: dict[str, tuple[str, str, str]] = {
    # engineering
    "backend-architect": ("engineering/engineering-backend-architect.md", "engineering", "后端架构师：可扩展系统、数据库、API 与云基础设施设计。"),
    "frontend-developer": ("engineering/engineering-frontend-developer.md", "engineering", "前端开发：现代 Web 界面、组件化、性能与可访问性。"),
    "senior-developer": ("engineering/engineering-senior-developer.md", "engineering", "资深开发：高质量实现、代码规范与工程判断。"),
    "software-architect": ("engineering/engineering-software-architect.md", "engineering", "软件架构师：系统边界、架构决策与权衡记录。"),
    "code-reviewer": ("engineering/engineering-code-reviewer.md", "engineering", "代码评审：正确性、安全、可维护性的建设性评审。"),
    "minimal-change-engineer": ("engineering/engineering-minimal-change-engineer.md", "engineering", "最小改动工程师：只改必须改的，避免顺手重构。"),
    "devops-automator": ("engineering/engineering-devops-automator.md", "engineering", "DevOps 自动化：CI/CD、基础设施即代码与部署流水线。"),
    "sre": ("engineering/engineering-sre.md", "engineering", "SRE：可靠性、SLO、监控告警与事故响应。"),
    "ai-engineer": ("engineering/engineering-ai-engineer.md", "engineering", "AI 工程师：模型集成、LLM 应用与评测落地。"),
    "technical-writer": ("engineering/engineering-technical-writer.md", "writing", "技术写作：开发者文档、API 参考、README 与教程。"),
    # testing
    "reality-checker": ("testing/testing-reality-checker.md", "engineering", "现实核验：以证据为准验收，拒绝未经证实的“完成”。"),
    "api-tester": ("testing/testing-api-tester.md", "engineering", "API 测试：接口功能、性能与安全测试。"),
    # product
    "product-manager": ("product/product-manager.md", "product", "产品经理：需求、路线图、优先级与跨团队协作。"),
    "sprint-prioritizer": ("product/product-sprint-prioritizer.md", "product", "迭代排期：敏捷规划、优先级排序与资源分配。"),
    "feedback-synthesizer": ("product/product-feedback-synthesizer.md", "product", "反馈归纳：汇总用户反馈并提炼可执行洞察。"),
    # design
    "ui-designer": ("design/design-ui-designer.md", "design", "UI 设计师：视觉设计、组件库与设计系统。"),
    "ux-researcher": ("design/design-ux-researcher.md", "design", "UX 研究员：用户研究、可用性测试与洞察。"),
    # marketing
    "content-creator": ("marketing/marketing-content-creator.md", "marketing", "内容创作：多平台内容策略、编辑日历与文案。"),
    "growth-hacker": ("marketing/marketing-growth-hacker.md", "marketing", "增长黑客：获客实验、转化漏斗与快速迭代。"),
    "seo-specialist": ("marketing/marketing-seo-specialist.md", "marketing", "SEO 专家：技术 SEO、内容优化与自然流量增长。"),
    # operations
    "senior-project-manager": ("project-management/project-manager-senior.md", "operations", "高级项目经理：把规格拆成任务，控制范围与节奏。"),
    "support-responder": ("support/support-support-responder.md", "operations", "客服响应：多渠道客户支持与问题解决。"),
    "infrastructure-maintainer": ("support/support-infrastructure-maintainer.md", "operations", "基础设施运维：系统稳定、性能、安全与成本。"),
    # research
    "research-synthesist": ("research/research-synthesist.md", "research", "研究综合：文献综述、来源评估与证据综合。"),
    "trend-researcher": ("product/product-trend-researcher.md", "research", "趋势研究：市场情报、竞品分析与新兴趋势。"),
    # writing
    "book-co-author": ("marketing/marketing-book-co-author.md", "writing", "图书合著：长文写作、结构规划与作者口吻。"),
}

SKILL_ZH: dict[str, str] = {
    "api-and-interface-design": "API 与接口设计：稳定、清晰、可演进的接口契约。",
    "browser-testing-with-devtools": "用浏览器 DevTools 做真实页面测试与排障。",
    "ci-cd-and-automation": "CI/CD 与自动化：流水线、质量门禁与发布自动化。",
    "code-review-and-quality": "代码评审与质量：多维度评审后再合并。",
    "code-simplification": "代码简化：在不改行为的前提下降低复杂度。",
    "constraint-driven-development": "约束驱动开发：先定硬约束再实现。",
    "context-engineering": "上下文工程：为智能体准备恰当的上下文。",
    "debugging-and-error-recovery": "调试与错误恢复：系统化定位根因并修复。",
    "deprecation-and-migration": "弃用与迁移：安全地下线旧代码与迁移用户。",
    "documentation-and-adrs": "文档与 ADR：记录架构决策与关键文档。",
    "doubt-driven-development": "怀疑驱动开发：主动质疑假设并验证。",
    "frontend-ui-engineering": "前端 UI 工程：生产级界面实现。",
    "git-workflow-and-versioning": "Git 工作流与版本管理：提交、分支与发布规范。",
    "idea-refine": "想法打磨：把模糊想法收敛成可执行方案。",
    "incremental-implementation": "增量实现：小步交付、每步可验证。",
    "interview-me": "访谈澄清：通过提问把需求问清楚。",
    "observability-and-instrumentation": "可观测性与埋点：日志、指标与追踪。",
    "performance-optimization": "性能优化：先测量再优化。",
    "planning-and-task-breakdown": "规划与任务拆解：把工作拆成可验证的小任务。",
    "security-and-hardening": "安全与加固：输入校验、密钥管理与防护。",
    "shipping-and-launch": "发布上线：上线前检查与灰度发布。",
    "source-driven-development": "来源驱动开发：以官方文档/源码为准。",
    "spec-driven-development": "规格驱动开发：先写规格再写代码。",
    "test-driven-development": "测试驱动开发：红-绿-重构循环。",
    "using-agent-skills": "技能使用指南：如何发现并调用这些技能。",
}


def _git_head(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _header(*, kind: str, preset_id: str, src: dict[str, str], commit: str, rel: str, category: str, summary_zh: str) -> str:
    return (
        "<!-- veya-preset\n"
        f"kind: {kind}\n"
        f"id: {preset_id}\n"
        f"category: {category}\n"
        f"source: {src['url']}/blob/{commit}/{rel}\n"
        f"source_path: {src['repo']}/{rel}\n"
        f"license: MIT\n"
        f"copyright: {src['copyright']}\n"
        f"summary_zh: {summary_zh}\n"
        "-->\n"
    )


def _check_license(repo: Path) -> None:
    text = (repo / "LICENSE").read_text(encoding="utf-8", errors="replace")
    if "MIT License" not in text:
        raise SystemExit(f"{repo}: LICENSE is not MIT — refusing to import")


def import_roles(upstream: Path) -> int:
    repo = upstream / AGENCY["repo"]
    _check_license(repo)
    commit = _git_head(repo)
    out_dir = OUT_ROOT / "roles"
    out_dir.mkdir(parents=True, exist_ok=True)
    for preset_id, (rel, category, zh) in ROLES.items():
        body = (repo / rel).read_text(encoding="utf-8")
        head = _header(kind="role", preset_id=preset_id, src=AGENCY, commit=commit, rel=rel, category=category, summary_zh=zh)
        (out_dir / f"{preset_id}.md").write_text(head + body, encoding="utf-8")
    return len(ROLES)


def import_skills(upstream: Path) -> int:
    repo = upstream / SKILLS["repo"]
    _check_license(repo)
    commit = _git_head(repo)
    count = 0
    for skill_dir in sorted((repo / "skills").iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        preset_id = skill_dir.name
        out_dir = OUT_ROOT / "skills" / preset_id
        out_dir.mkdir(parents=True, exist_ok=True)
        zh = SKILL_ZH.get(preset_id, "")
        # Markdown only: SKILL.md and any *.md siblings/references. Scripts and
        # every other file type are deliberately never copied (no executable
        # content enters the preset pack).
        for src in sorted(skill_dir.rglob("*.md")):
            rel_in_skill = src.relative_to(skill_dir)
            rel = f"skills/{preset_id}/{rel_in_skill.as_posix()}"
            dest = out_dir / rel_in_skill
            dest.parent.mkdir(parents=True, exist_ok=True)
            head = _header(kind="skill", preset_id=preset_id, src=SKILLS, commit=commit, rel=rel, category="engineering", summary_zh=zh)
            dest.write_text(head + src.read_text(encoding="utf-8"), encoding="utf-8")
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default="/tmp/w15-upstream")
    args = parser.parse_args(argv)
    upstream = Path(args.upstream)
    roles = import_roles(upstream)
    skills = import_skills(upstream)
    print(f"imported roles={roles} skills={skills} -> {OUT_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
