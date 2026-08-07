#!/usr/bin/env python3
"""导入 ECC 领域 Agent (agents/*.md) → skill_hub 技能包 (零侵入热载)。

ECC 的 agents/<name>.md (YAML frontmatter + 领域提示词) 是"提示词资产":
转为本仓库 skill_hub 技能包 (manifest.json + run.py), LLM 按 description
自动分派 (skill_hub schema 喂 LLM)。

用法:
  git clone --depth 1 https://github.com/affaan-m/ECC /tmp/ECC
  python scripts/import_ecc_agents.py /tmp/ECC [--out ~/.veya/skills] [--limit 5]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, str]:
    """极简 YAML frontmatter 解析 (key: value)。"""
    m = FRONTMATTER_RE.match(text)
    meta: dict[str, str] = {}
    if not m:
        return meta
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


def build_skill_package(agent_md: Path, out_dir: Path) -> dict[str, str]:
    """单个 agent.md → 技能包 (manifest.json + run.py)。"""
    raw = agent_md.read_text(encoding="utf-8")
    meta = parse_frontmatter(raw)
    name = meta.get("name", agent_md.stem)
    description = meta.get("description", f"ECC 领域 Agent: {name}")
    tools = meta.get("tools", "")
    model = meta.get("model", "")

    # 去掉 frontmatter 后的正文 (领域提示词)
    body = FRONTMATTER_RE.sub("", raw).strip()

    skill_name = f"ecc_{re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')}"
    pkg = out_dir / skill_name
    pkg.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": skill_name,
        "description": f"[ECC 领域] {description} (tools: {tools})",
        "type": "python",
        "entrypoint": "run.py",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "要交给该领域 Agent 的任务"},
                "context": {"type": "string", "description": "相关代码/上下文"},
            },
            "required": ["goal"],
        },
    }
    (pkg / "manifest.json").write_text(
        __import__("json").dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8")

    run_py = f'''"""ECC 领域 Agent 技能包: {name} (自动导入, 只读资产)。

提示词资产来自 ECC agents/{agent_md.name} — 本技能生成领域审查指令,
由上层 (主脑/编排) 消费; 技能本身零 LLM 调用。
"""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = """{body[:3000]}"""


def main(goal: str, context: str = "", **_: Any) -> dict[str, Any]:
    """返回领域 Agent 指令 (system prompt + 任务)。"""
    return {{
        "ok": True,
        "skill": "{skill_name}",
        "domain_agent": "{name}",
        "model_hint": "{model}",
        "goal": goal,
        "instruction": f"{{SYSTEM_PROMPT}}\\n\\n任务: {{goal}}"
                       + (f"\\n\\n上下文: {{context[:2000]}}" if context else ""),
    }}
'''
    (pkg / "run.py").write_text(run_py, encoding="utf-8")
    return {"name": skill_name, "description": description, "model": model}


def main_cli() -> int:
    ap = argparse.ArgumentParser(description="导入 ECC 领域 Agent → skill_hub 技能包")
    ap.add_argument("ecc_repo", help="ECC 仓库路径 (含 agents/ 目录)")
    ap.add_argument("--out", default=str(Path.home() / ".veya" / "skills"))
    ap.add_argument("--limit", type=int, default=0, help="导入数量限制 (0=全部)")
    args = ap.parse_args()

    agents_dir = Path(args.ecc_repo) / "agents"
    if not agents_dir.is_dir():
        print(f"✗ 未找到 {agents_dir} (先 clone ECC)")
        return 1
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(agents_dir.glob("*.md"))
    if args.limit:
        files = files[:args.limit]

    imported = 0
    skipped = 0
    for f in files:
        try:
            info = build_skill_package(f, out_dir)
            print(f"  ✓ {info['name']} — {info['description'][:60]}")
            imported += 1
        except Exception as e:
            print(f"  ✗ {f.name}: {e}")
            skipped += 1

    print(f"\n导入完成: {imported} 个领域 Agent → {out_dir} (skill_hub 热载即用)")
    print("分派: LLM 按 description 自动选择领域技能 (schema 已喂 tools)")
    return 0 if skipped == 0 else 1


if __name__ == "__main__":
    sys.exit(main_cli())
