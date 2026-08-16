"""Veya 统一流水线总装 — 把 Graft(Phase1) / OpenRSI(Phase2) / ReasoningBank(Phase3)
编排成一条流转, 并作为 evolve_solution 主脑工具暴露。

装配即拦截与注入:
  Phase 1 (pre)   Graft 代码依赖地图 + ReasoningBank 历史规则 → System Context Block
  Phase 2 (in)    OpenRSI 多分支 MCTS 演化 (沙盒反馈驱动), 选出沙盒验证过的最优解
  Phase 3 (post)  对比死亡/成功分支归纳经验三元组, 落盘供下次开局检索

铁律: 只装配, 不写入 3O 子库。生成步骤统一走注入的同步 llm(prompt)->str,
生产侧桥接 veya.llm.llm_call (async→sync, 跑在 worker 线程), 测试侧注入确定性桩。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.graft_context import GraftContext, extract_entities
from server.openrsi import EvoResult, default_probes, evolve
from server.reasoning_bank import Experience, ReasoningBank

Llm = Callable[[str], str]


@dataclass
class PipelineResult:
    solved: bool
    best_reward: float
    best_files: dict[str, str]
    context_block: str
    experience: Experience | None
    stats: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        head = (
            "✅ solved" if self.solved else "⚠️ best-effort"
        ) + f" (reward={self.best_reward:.2f})"
        lines = [f"Evolution {head}. stats={self.stats}"]
        if self.experience:
            lines.append(f"Learned rule: {self.experience.to_rule()}")
        return "\n".join(lines)


def run_pipeline(
    task: str,
    workspace_files: dict[str, str],
    target_files: list[str],
    llm: Llm,
    *,
    bank: ReasoningBank | None = None,
    graft: GraftContext | None = None,
    n_branches: int = 8,
    budget: int = 40,
    max_depth: int = 4,
    isolation: str = "netns",
) -> PipelineResult:
    """纯编排 (无 I/O 副作用, 全部注入): Phase 1 → 2 → 3。"""
    graft = graft or GraftContext()
    bank = bank or ReasoningBank()

    # ---- Phase 1: 上下文装配 (Graft 空间维度 + ReasoningBank 时间维度) ----
    graft.sync(workspace_files)
    entities = extract_entities(task)
    code_block = graft.assemble(entities)
    lessons = bank.search_experience(task)
    rule_block = bank.as_rule_block(lessons)
    context_block = "\n\n".join(b for b in (code_block, rule_block) if b)

    # 把上下文块前置进任务描述 — 主脑/算子生成时即"带地图和秘籍开局"
    enriched_task = f"{task}\n\n{context_block}" if context_block else task

    # ---- Phase 2: OpenRSI 多分支演化 (沙盒反馈驱动) ----
    result: EvoResult = evolve(
        enriched_task,
        workspace_files,
        target_files,
        llm,
        n_branches=n_branches,
        budget=budget,
        max_depth=max_depth,
        probes_factory=default_probes,
        isolation=isolation,
    )

    # ---- Phase 3: 反思归纳 + 落盘 ----
    experience = bank.induce(task, result.trajectory, llm, target=target_files[0])

    return PipelineResult(
        solved=result.solved,
        best_reward=result.best_reward,
        best_files=result.best_files,
        context_block=context_block,
        experience=experience,
        stats=result.stats,
    )


# ------------------------------------------------------------------ 主脑工具适配

_NOISE_DIRS = {".git", "venv", "node_modules", "__pycache__", ".veya", "platform"}


def _read_workspace(root: Path, target_file: str, extra_globs: list[str]) -> dict[str, str]:
    """读入目标文件 + 测试文件构成候选跑测的最小 workspace。"""
    files: dict[str, str] = {}
    tp = root / target_file
    if tp.exists():
        files[target_file] = tp.read_text(encoding="utf-8")
    for pat in ["test_*.py", "*_test.py", *extra_globs]:
        for p in root.rglob(pat):
            if any(part in _NOISE_DIRS for part in p.parts):
                continue
            rel = str(p.relative_to(root))
            files[rel] = p.read_text(encoding="utf-8")
    return files


# 输出预算默认区间 (可经 env / 工具参数覆盖)。floor 保证小任务也有推理头寸;
# ceiling 防失控。reasoning 模型 (如 nemotron) 会先花上千 token 思考再吐正文,
# 固定 4096 对整文件重写会截断 —— 故按 prompt 规模自适应。
_DEFAULT_TOK_FLOOR = int(os.environ.get("VEYA_EVOLVE_MAX_TOKENS_FLOOR", "4096"))
_DEFAULT_TOK_CEILING = int(os.environ.get("VEYA_EVOLVE_MAX_TOKENS_CEILING", "32768"))
_REASONING_HEADROOM = int(os.environ.get("VEYA_EVOLVE_REASONING_HEADROOM", "4000"))


def _adaptive_max_tokens(prompt: str, floor: int, ceiling: int) -> int:
    """按 prompt 规模估算输出预算: 单文件重写的产出 ≈ prompt 里的代码量,
    留 1.3× 冗余 + 推理头寸, 再夹到 [floor, ceiling]。
    """
    prompt_tokens = int(len(prompt) / 3.5)  # 中英+代码混合的粗估
    budget = int(prompt_tokens * 1.3) + _REASONING_HEADROOM
    return max(floor, min(budget, ceiling))


# 已知网关预设。opencode(zen) 快模型如 deepseek-v4-flash: 需正确端点, 且网关只认
# 裸 model id (带 opencode-go/ 前缀会 "Model not supported"); 通用 llm_call 不配端点
# 会回落 OpenAI → 401。key 从 OPENCODE_API_KEY env 注入。
_OPENCODE_ENDPOINT = "https://opencode.ai/zen/go/v1"


def _apply_provider_preset(
    model: str | None, provider: str | None, config: dict | None
) -> tuple[str | None, str | None, dict | None, str | None]:
    """对已知网关套用端点/模型/密钥预设。返回 (model, provider, config, endpoint)。

    非预设 provider 原样返回 (endpoint=None)。纯函数, 无网络 (可单测)。
    """
    if provider != "opencode":
        return model, provider, config, None
    norm_model = model.split("/")[-1] if model else model  # opencode-go/x → x (裸 id)
    cfg = dict(config or {})
    providers = dict(cfg.get("providers") or {})
    if "opencode" not in providers and os.environ.get("OPENCODE_API_KEY"):
        providers["opencode"] = {"api_key": os.environ["OPENCODE_API_KEY"]}
        cfg["providers"] = providers
    return norm_model, provider, cfg, _OPENCODE_ENDPOINT


def _make_sync_llm(
    model: str | None,
    provider: str | None,
    config: dict | None,
    *,
    floor: int | None = None,
    ceiling: int | None = None,
) -> Llm:
    """把 async veya.llm.llm_call 包成同步 llm(prompt)->str (供演化核心调用)。

    max_tokens 按每次 prompt 自适应 (见 _adaptive_max_tokens), 不再写死。
    已知网关 (opencode) 自动套端点/裸模型/密钥预设。
    仅在 worker 线程内使用 (asyncio.run 需要无运行中的事件循环)。
    """
    from veya.llm import llm_call

    fl = floor or _DEFAULT_TOK_FLOOR
    ce = ceiling or _DEFAULT_TOK_CEILING
    model, provider, config, endpoint = _apply_provider_preset(model, provider, config)
    extra = {"endpoint": endpoint} if endpoint else {}

    def _llm(prompt: str) -> str:
        resp = asyncio.run(
            llm_call(
                [{"role": "user", "content": prompt}],
                model=model,
                provider=provider,
                config=config,
                max_tokens=_adaptive_max_tokens(prompt, fl, ce),
                **extra,
            )
        )
        return ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    return _llm


async def evolve_solution_tool(
    task: str,
    target_file: str,
    *,
    test_globs: list[str] | None = None,
    workspace_root: str | None = None,
    n_branches: int = 8,
    budget: int = 40,
    max_depth: int = 4,
    model: str | None = None,
    provider: str | None = None,
    config: dict | None = None,
    max_tokens_ceiling: int | None = None,
    max_tokens_floor: int | None = None,
    _llm: Llm | None = None,
    _bank_dir: str | None = None,
) -> str:
    """evolve_solution 主脑工具: 对 target_file 跑完整统一流水线并写回最优解。

    _llm / _bank_dir 为测试注入用 (下划线前缀不进 LLM schema)。
    """
    # 工作区根: 显式参数 > VEYA_WORKSPACE (与 grep/read_file_ast 一致, 而非进程 cwd)。
    # 原来默认 "." → 主脑不传参时落到 veya 项目根, 找不到用户文件 → 静默空转。
    if workspace_root:
        root = Path(workspace_root).resolve()
    else:
        from server.tool_registry import _resolve_workspace_root

        root = _resolve_workspace_root()
    files = _read_workspace(root, target_file, test_globs or [])
    if target_file not in files:
        return f"evolve_solution: 目标文件不存在: {target_file}"
    if not any(k != target_file for k in files):
        return "evolve_solution: 未找到测试文件 (test_*.py), 演化需要沙盒可执行的测试来打分。"

    # provider/model 未显式指定时回落 env 默认 (VEYA_EVOLVE_PROVIDER/MODEL) —
    # 让 evolve_solution 开箱即用一个配好的快模型 (如 opencode/deepseek-v4-flash)。
    provider = provider or os.environ.get("VEYA_EVOLVE_PROVIDER") or None
    model = model or os.environ.get("VEYA_EVOLVE_MODEL") or None
    llm = _llm or _make_sync_llm(
        model, provider, config, floor=max_tokens_floor, ceiling=max_tokens_ceiling
    )
    bank = ReasoningBank(base_dir=_bank_dir) if _bank_dir else ReasoningBank()

    def _blocking() -> PipelineResult:
        return run_pipeline(
            task,
            files,
            [target_file],
            llm,
            bank=bank,
            n_branches=n_branches,
            budget=budget,
            max_depth=max_depth,
        )

    result = await asyncio.to_thread(_blocking)

    # 写回沙盒验证过的最优解 (仅目标文件; 测试文件天然冻结)
    if result.best_files.get(target_file):
        (root / target_file).write_text(result.best_files[target_file], encoding="utf-8")
    return result.summary()


def register(master_tools: Any) -> None:
    """把 evolve_solution 挂到主脑注册表 (由 tool_registry 在装配期调用)。"""
    if master_tools.has("evolve_solution"):
        return
    master_tools.register(
        name="evolve_solution",
        description=(
            "Test-driven evolutionary search (sandbox MCTS). Use ONLY when (1) test_*.py already "
            "exists and (2) the user asked to search/evolve until tests pass. "
            "Ordinary implement/fix/refactor → hicode_run. Do NOT prefer this over hicode_run. "
            "Internally attaches a Graft map + ReasoningBank lessons, rewrites target_file if a "
            "candidate goes green. Requires target_file + at least one test_*.py."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "what the code should do"},
                "target_file": {
                    "type": "string",
                    "description": "path (relative to workspace) the search may rewrite",
                },
                "test_globs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "extra test file globs beyond test_*.py (optional)",
                },
                "n_branches": {
                    "type": "integer",
                    "description": "parallel search width (initial drafts), default 8; "
                    "lower it (2-3) for quick/cheap runs, raise for hard problems",
                },
                "budget": {
                    "type": "integer",
                    "description": "MCTS simulation budget, default 40; search stops early once "
                    "tests pass, so a high budget only costs on genuinely hard tasks",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "max refinement depth per branch "
                    "(draft→debug→improve→…), default 4",
                },
                "max_tokens_ceiling": {
                    "type": "integer",
                    "description": "cap on per-call output tokens; raise for large multi-file "
                    "rewrites to avoid truncation (default adaptive, up to 32768)",
                },
            },
            "required": ["task", "target_file"],
        },
        func=evolve_solution_tool,
        max_result_chars=4000,
    )
