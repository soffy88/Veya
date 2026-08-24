"""主脑级自动上下文注入 (统一流水线 Phase 1 的 master 接线)。

每轮请求前, 静默为主脑装配"空间维度(Graft 代码依赖地图) + 时间维度(ReasoningBank
历史规则)"的 System Context Block。与 evolve_solution 的 Phase 3 共用持久记忆库
(~/.veya/memory), 形成闭环: 演化归纳的经验下一轮开局即被检索注入。

安全约束 (与 _inject_memory 同): 空 → 完全无操作 (零行为变化); 任何异常都被上层
suppress, 绝不拖垮对话。工作区扫描按 mtime 缓存, 稳态几乎零成本。
"""

from __future__ import annotations

import os
from pathlib import Path

from server.graft_context import GraftContext, extract_entities
from server.reasoning_bank import ReasoningBank

# 注入块的稳定标记 (主脑历史里按此前缀幂等刷新, 每轮先清旧块再插新块)。
MARK = "<!-- veya-auto-context -->"

_NOISE = {
    ".git",
    "venv",
    ".venv",
    "node_modules",
    "__pycache__",
    ".veya",
    "platform",
    "site",
    "dist",
    "build",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}

_graft: GraftContext | None = None
_bank: ReasoningBank | None = None
_mtime_cache: dict[str, tuple[float, str]] = {}
_explain_cache: "GraftExplainCache | None" = None


def enabled() -> bool:
    """每轮预注入开关。默认关闭 (模型按需调 assemble_code_context / 编码工具内附带)。

    VEYA_GRAFT_CONTEXT=1 恢复旧的每轮 system 预注入 (不推荐, 会带偏非编码任务)。
    """
    return os.environ.get("VEYA_GRAFT_CONTEXT", "0").lower() in ("1", "true", "yes", "on")


def _get_graft() -> GraftContext:
    global _graft
    if _graft is None:
        _graft = GraftContext()
    return _graft


def _get_bank() -> ReasoningBank:
    global _bank
    if _bank is None:
        _bank = ReasoningBank()  # 默认持久库 ~/.veya/memory, 与 evolve_solution 共用
    return _bank


def _get_explain_cache() -> "GraftExplainCache":
    global _explain_cache
    if _explain_cache is None:
        from server.graft_explain import GraftExplainCache

        _explain_cache = GraftExplainCache()
    return _explain_cache


def _workspace_root() -> Path:
    from server.tool_registry import _resolve_workspace_root

    return _resolve_workspace_root()


def _scan_py(root: Path, cap: int = 250) -> dict[str, str]:
    """有界扫描工作区 .py; 按 mtime 缓存, 未变文件不重读 (稳态近零成本)。"""
    out: dict[str, str] = {}
    for p in root.rglob("*.py"):
        if any(part in _NOISE for part in p.parts):
            continue
        key = str(p)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        cached = _mtime_cache.get(key)
        if cached and cached[0] == mtime:
            text = cached[1]
        else:
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            _mtime_cache[key] = (mtime, text)
        out[str(p.relative_to(root))] = text
        if len(out) >= cap:
            break
    return out


def build_block(query: str, *, force: bool = False) -> str:
    """装配代码地图 + 历史规则; 无实质内容时返回空串。

    force=False: 仅当 VEYA_GRAFT_CONTEXT=1 时装配 (每轮预注入路径)。
    force=True: 工具 / hicode 按需调用, 忽略预注入开关。

    纯同步 (调用方在 to_thread 里跑)。任何子步骤失败都退化为不贡献该部分, 不抛。
    """
    if not force and not enabled():
        return ""
    parts: list[str] = []

    # 时间维度: 历史避坑规则 (持久库, 即使无代码上下文也有价值)
    try:
        lessons = _get_bank().search_experience(query)
        rule_block = _get_bank().as_rule_block(lessons)
        if rule_block:
            parts.append(rule_block)
    except Exception:  # 记忆故障绝不拖垮注入
        pass

    # 空间维度: 代码依赖地图 (仅当 prompt 含可定位实体且工作区有匹配符号)
    try:
        entities = extract_entities(query)
        if entities:
            files = _scan_py(_workspace_root())
            if files:
                g = _get_graft()
                g.sync(files)
                if any(g.find(e) for e in entities):
                    parts.append(g.assemble(entities))
    except Exception:  # 代码图故障绝不拖垮注入
        pass

    if not parts:
        return ""
    return MARK + "\n" + "\n\n".join(parts)


def attach_to_task(task: str) -> str:
    """编码工具内附带: 有地图/规则则拼到任务书后, 否则原样返回。"""
    block = build_block(task, force=True)
    if not block:
        return task
    return f"{task}\n\n{block}"


async def assemble_code_context(query: str) -> str:
    """主脑工具: 按需装配 Graft 代码依赖地图 + 讲解层 + ReasoningBank 历史规则。

    讲解层(server.graft_explain, nanonets/graft "资深工程师讲解" 的内化)是
    LLM 生成、按内容哈希缓存的锦上添花——结构化地图(build_block, 零 LLM)才是
    可靠的主体, 讲解层失败/无匹配时静默跳过, 不影响这个工具本身能不能用。
    """
    block = build_block(query, force=True)
    narrative = await _assemble_narrative(query)
    parts = [p for p in (block, narrative) if p]
    if not parts:
        return (
            "No code-map or past-lesson match for this query "
            "(no matching symbols in the workspace, empty ReasoningBank)."
        )
    return "\n\n".join(parts)


async def _assemble_narrative(query: str) -> str:
    """讲解层: 给命中的模块生成"这部分是干什么的"大白话解释, 补 build_block
    结构化部分(定义位置/调用方/被调方)之外的"为什么"。任何环节失败/无匹配都
    优雅退化为空串, 不抛异常。
    """
    try:
        entities = extract_entities(query)
        if not entities:
            return ""
        files = _scan_py(_workspace_root())
        if not files:
            return ""
        g = _get_graft()
        g.sync(files)
        hits_by_module: dict[str, list[str]] = {}
        for entity in entities:
            for hit in g.find(entity):
                hits_by_module.setdefault(hit.module, []).append(entity)
        if not hits_by_module:
            return ""
    except Exception:
        return ""

    import asyncio

    from server.graft_explain import explain_module

    cache = _get_explain_cache()

    async def _one(module: str) -> tuple[str, str]:
        source = files.get(module, "")
        if not source:
            return module, ""
        try:
            text = await explain_module(
                module=module, source=source, symbol_names=hits_by_module[module], cache=cache
            )
        except Exception:
            text = ""
        return module, text

    results = await asyncio.gather(*(_one(m) for m in hits_by_module))
    lines = ["## CODE EXPLANATION (senior-engineer-style, cached)"]
    found = False
    for module, text in results:
        if text:
            found = True
            lines.append(f"\n### `{module}`\n{text}")
    return "\n".join(lines) if found else ""


def register(master_tools: object) -> None:
    """把 assemble_code_context 挂到主脑注册表 (装配期调用, 幂等)。"""
    from server.tool_registry import SideEffect  # 延迟导入: tool_registry 装配期反向调用本模块

    has = getattr(master_tools, "has", None)
    reg = getattr(master_tools, "register", None)
    if not callable(has) or not callable(reg) or has("assemble_code_context"):
        return
    reg(
        name="assemble_code_context",
        description=(
            "Build a Graft code-dependency map + ReasoningBank lessons for THIS workspace. "
            "Call BEFORE hicode_run / evolve_solution when the task touches existing code "
            "(refactor, fix, understand callers/callees, avoid a past pitfall). "
            "Pass the user request (or the symbol/file names) as query. "
            "Does NOT write files. Empty match is normal for conceptual / non-code questions "
            "— then answer directly or use hicode_run without a map."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "user request or symbol/file names to locate",
                }
            },
            "required": ["query"],
        },
        func=assemble_code_context,
        max_result_chars=12000,
        side_effect=SideEffect.PURE_READ,
    )
