"""只读导出 Veya 当前的 prompt / 技能 / 工具说明为 harness2 组件树。

Phase 0 的目标是"快照"，不是"回流"：本模块只读取 Veya 的现有数据，绝不修改
``server/coordinator_master.py`` / ``server/tool_registry.py`` / 主链路行为。

输出分两层：

* ``snapshot/``：完整只读快照（全部 prompt、161 工具说明、107 技能清单、记忆）。
* ``base/``：harness2 OpenCode surface 可用的精简 harness。完整快照通常远超
  surface 的 24k always-on 上限，所以 base 只保留摘要 + 指向 snapshot 的指针；
  这是 Phase 0 的明确取舍，Phase 1 需要决定"如何把完整 Veya 工具面映射进
  harness2 surface"。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from server.harness._harness2 import import_harness2_core


@dataclass(frozen=True)
class VeyaSources:
    """导出所需的最小只读快照；测试可注入，避免依赖完整 Veya 运行时。"""

    system_prompt: str
    host_sop: str
    tool_schemas: list[dict] = field(default_factory=list)
    skill_manifests: list[dict] = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    hook_notes: str = ""


def _render_tools(schemas: list[dict], manifests: list[dict]) -> str:
    lines = ["# VEYA TOOL GUIDANCE", "", "The following tools are registered in Veya.", ""]
    for schema in sorted(schemas, key=lambda s: s.get("function", {}).get("name", "")):
        fn = schema.get("function") or {}
        name = fn.get("name", "")
        desc = str(fn.get("description") or "").strip()
        params = fn.get("parameters") or {}
        required = params.get("required") or []
        props = sorted((params.get("properties") or {}).keys())
        lines.append(f"## `{name}`")
        if desc:
            lines.append(desc)
        if props:
            suffix = f" (required: {', '.join(required)})" if required else ""
            lines.append(f"Parameters: {', '.join(props)}{suffix}")
        lines.append("")
    lines.extend(["# VEYA SKILLS INVENTORY", "", "Read-only snapshot of installed skills.", ""])
    for manifest in sorted(manifests, key=lambda m: str(m.get("name", ""))):
        name = str(manifest.get("name") or "")
        desc = str(manifest.get("description") or "").strip()
        lines.append(f"- `{name}`: {desc}" if desc else f"- `{name}`")
    return "\n".join(lines).rstrip() + "\n"


def _render_memory(memory: dict) -> str:
    prefs = memory.get("preferences") or []
    lines = ["# VEYA LONG-TERM MEMORY", "", "Read-only snapshot of global preferences.", ""]
    if not prefs:
        lines.append("(empty)")
    for pref in prefs:
        rule = str(pref.get("rule") or "").strip()
        context = str(pref.get("context") or "General")
        lines.append(f"- [{context}] {rule}")
    return "\n".join(lines).rstrip() + "\n"


def build_harness_tree(sources: VeyaSources) -> dict[str, str]:
    """把只读快照渲染成 harness2 OpenCode surface 的精简组件树。"""
    system_prompt = sources.system_prompt.rstrip()
    host_sop = sources.host_sop.strip()
    # harness2 OpenCode surface 的 always-on 上限是 24,000 字符；Veya 的完整
    # system prompt + 161 工具说明 + 107 技能清单远超该上限。base 只放摘要与
    # 指针，完整内容见 snapshot/。
    systemprompt = (
        "# VEYA BASE HARNESS (PHASE 0 SUMMARY)\n\n"
        "This is a surface-sized summary of the Veya runtime harness. The complete\n"
        "read-only snapshot lives in `snapshot/` next to this file.\n\n"
        "## System prompt summary\n\n"
        + _summarize(system_prompt, 6000)
        + "\n\n## Host SOP\n\n"
        + _summarize(host_sop, 3000)
        + "\n"
    )
    guardrails = (
        "# VEYA GUARDRAILS\n\n"
        "Read-only snapshot of the host hook chain. The executable policy remains in Veya;\n"
        "this file exists so a harness2 proposer can see the guardrail surface.\n\n"
        + (sources.hook_notes.strip() + "\n" if sources.hook_notes.strip() else "")
    )
    return {
        "systemprompt.md": systemprompt,
        "guardrails.md": guardrails,
        "LongTermMEMORY.md": _summarize(_render_memory(sources.memory), 3000),
        # 技能清单并入 tool guidance：两者都在 surface 内，避免出现
        # surface 之外的 `skills_inventory.md` 被 harness2 判为 rejected。
        "tool_descriptions/tool_guidance.md": _summarize(
            _render_tools(sources.tool_schemas, sources.skill_manifests), 9000
        ),
    }


def _summarize(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text.rstrip() + "\n"
    return (
        text[:limit].rstrip()
        + f"\n\n... [truncated {len(text) - limit} chars; full snapshot in snapshot/]\n"
    )


def build_snapshot_tree(sources: VeyaSources) -> dict[str, str]:
    """完整只读快照（不要求符合 harness2 surface 上限）。"""
    system_prompt = sources.system_prompt.rstrip()
    host_sop = sources.host_sop.strip()
    return {
        "systemprompt.md": system_prompt + ("\n\n" + host_sop if host_sop else "") + "\n",
        "guardrails.md": (
            "# VEYA GUARDRAILS\n\n"
            + (sources.hook_notes.strip() + "\n" if sources.hook_notes.strip() else "")
        ),
        "LongTermMEMORY.md": _render_memory(sources.memory),
        "tool_descriptions/tool_guidance.md": _render_tools(
            sources.tool_schemas, sources.skill_manifests
        ),
    }


def read_veya_sources() -> VeyaSources:
    """读取 Veya 当前的真实数据（只读；不写任何文件）。

    注意：``server.coordinator_master`` 在 import 时会构造模块级
    ``master_coordinator`` 单例（会接线 vault/guard/remote 工具）。导出器需要
    的是 prompt 常量与工具 schema，因此这里仍然 import 它——但只在显式的
    export/演示入口调用，不在 Veya 服务进程的常规 import 路径上。
    """
    from server.coordinator_master import (
        _HOST_SOP_APPEND,
        MASTER_SYSTEM_PROMPT,
        _slim_master_prompt,
    )
    from server.tool_registry import master_tools

    skills_dir = Path.home() / ".veya" / "skills"
    manifests: list[dict] = []
    if skills_dir.is_dir():
        for manifest_path in sorted(skills_dir.glob("*/manifest.json")):
            try:
                manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue

    memory_path = Path.home() / ".veya" / "global_memory.json"
    memory: dict = {"preferences": []}
    if memory_path.is_file():
        try:
            loaded = json.loads(memory_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                memory = loaded
        except (OSError, json.JSONDecodeError):
            pass

    hook_notes = _read_hook_notes(Path(__file__).resolve().parents[2] / "hooks")
    return VeyaSources(
        # 真实 MasterAgent 的 system_prompt 是 slim 后的主库 prompt + 宿主 SOP；
        # 导出时保留这一组合，避免把未 slim 的原始常量误当成运行时提示。
        system_prompt=_slim_master_prompt(MASTER_SYSTEM_PROMPT),
        host_sop=_HOST_SOP_APPEND,
        tool_schemas=master_tools.get_all_schemas(),
        skill_manifests=manifests,
        memory=memory,
        hook_notes=hook_notes,
    )


def _read_hook_notes(hooks_root: Path) -> str:
    """从 hooks 源码读取只读摘要，不 import / 不执行任何 hook。"""
    lines = ["## Hook chain (source summary)"]
    builtin = hooks_root / "builtin"
    if builtin.is_dir():
        for path in sorted(builtin.glob("*.py")):
            if path.name == "__init__.py":
                continue
            try:
                doc = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            summary = ""
            for line in doc[:20]:
                text = line.strip().strip('"')
                if text and not text.startswith(("from ", "import ", "#!")):
                    summary = text
                    break
            lines.append(f"- `{path.stem}`: {summary or '(no docstring)'}")
    return "\n".join(lines) + "\n"


def export_base_harness(
    out_dir: Path | str,
    *,
    sources: VeyaSources | None = None,
) -> Path:
    """把 Veya 快照写成 harness2 surface 目录树，返回输出目录。

    ``out_dir`` 是 surface 合规的 base；完整快照写到其同级 ``snapshot/``。
    """
    core = import_harness2_core()
    from harness2.substrate.opencode import OPENCODE_SURFACE  # type: ignore

    resolved = sources or read_veya_sources()
    tree = build_harness_tree(resolved)
    out = Path(out_dir).expanduser()
    # 这是显式的快照输出目录：先清空，确保旧版本遗留的 surface 外文件
    # 不会污染 harness2 的可编辑树。
    if out.exists():
        shutil.rmtree(out)
    core.tree.write_tree(OPENCODE_SURFACE, tree, out, stubs=False)
    snapshot = out.parent / "snapshot"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    core.tree.write_tree(OPENCODE_SURFACE, build_snapshot_tree(resolved), snapshot, stubs=False)
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path.home() / ".veya" / "harness" / "base",
        help="output directory (default: ~/.veya/harness/base)",
    )
    args = parser.parse_args()
    result = export_base_harness(args.out)
    print(result)
