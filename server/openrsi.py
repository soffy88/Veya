"""OpenRSI 演化核心 — Veya 统一流水线 Phase 2 (装配层, 不写入 3O 子库)。

把"单次盲写代码"换成"带沙盒反馈的多分支演化搜索"。本模块只组装 oprim 主库
的真实能力, 生成步骤(evo 算子)由注入的 LLM 完成:

  - oprim.MCTS / best_path         PUCT 树搜索 (LLM 先验驱动)
  - oprim.SandboxPool / LocalSandbox 隔离执行, size=N 即 N 条并行分支
  - oprim.run_probes + 探针         稠密奖励 (测试通过率 * diff 大小 * 语法门)

设计要点:
1. step()/reward() 按状态指纹记忆化 —— MCTS 在选择下降时会重复调用 step,
   不缓存则每次多花一次 LLM 调用且破坏可复现性。
2. LLM 以同步 ``llm(prompt) -> str`` 注入: 生产侧由 evolve_solution 主脑工具桥接
   ``veya.llm.llm_call`` (async→sync), 测试侧注入确定性桩, 核心保持 hermetic。
3. crossover 需要两个兄弟状态, 无法用单状态 WorldModel Protocol 表达 ——
   作为搜索后的融合步在 evolve() 里处理 (取 top-2 叶子融合再评一次)。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from veya.platform import load as _load_3o

_oprim = _load_3o("oprim")

MCTS = _oprim.MCTS
best_path = _oprim.best_path
SandboxPool = _oprim.SandboxPool
CommandProbe = _oprim.CommandProbe
DiffSizeProbe = _oprim.DiffSizeProbe
py_syntax_gate = _oprim.py_syntax_gate
unittest_ratio = _oprim.unittest_ratio
run_probes = _oprim.run_probes

# LLM 生成契约: 同步, 输入完整 prompt, 输出模型文本 (内含代码块)。
Llm = Callable[[str], str]
# 探针工厂: 给定 (workspace 全量文件, 目标文件列表) 产出该候选的评分探针序列。
ProbesFactory = Callable[[dict[str, str], list[str]], Sequence[Any]]


# ---------------------------------------------------------------- 状态与算子


@dataclass(frozen=True)
class EvoState:
    """一个候选解 = workspace 全量文件的不可变快照 + 谱系元信息。"""

    files: tuple[tuple[str, str], ...]  # 已排序的 (relpath, content), 保证可哈希/可复现
    depth: int = 0
    op: str = "root"  # 产生本状态的算子
    feedback: str = ""  # 上一步沙盒 stderr (供 debug 算子用)

    @property
    def file_map(self) -> dict[str, str]:
        return dict(self.files)

    @staticmethod
    def of(
        file_map: dict[str, str], *, depth: int = 0, op: str = "root", feedback: str = ""
    ) -> EvoState:
        return EvoState(tuple(sorted(file_map.items())), depth, op, feedback)


def _digest(files: tuple[tuple[str, str], ...]) -> str:
    h = hashlib.sha256()
    for path, content in files:
        h.update(path.encode())
        h.update(b"\0")
        h.update(content.encode())
        h.update(b"\0")
    return h.hexdigest()[:16]


_CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


def extract_code(text: str) -> str:
    """从 LLM 输出里抽第一个代码块; 无围栏则原样返回 (去首尾空白)。"""
    m = _CODE_BLOCK.search(text)
    return (m.group(1) if m else text).strip() + "\n"


# 四个 evo 算子的 prompt 模板。draft=盲写, improve=重构瘦身, debug=按报错修,
# crossover=融合两版之长 (crossover 在 evolve() 里单独驱动)。
_OP_INSTRUCTION = {
    "draft": "Write a first working implementation. Focus on passing the tests.",
    "improve": "The code passes but is not clean. Refactor for minimal, correct code "
    "without breaking any test.",
    "debug": "The code FAILED. Read the failure output and fix the root cause. "
    "Change as little as possible.",
}


def _op_prompt(op: str, task: str, target: str, current: str, feedback: str) -> str:
    parts = [
        f"# TASK\n{task}",
        f"\n# OPERATOR: {op}\n{_OP_INSTRUCTION.get(op, _OP_INSTRUCTION['draft'])}",
        f"\n# FILE: {target}\n```python\n{current}```",
    ]
    if feedback:
        parts.append(f"\n# SANDBOX FEEDBACK (last run)\n```\n{feedback[:2000]}\n```")
    parts.append(f"\nReturn the complete new content of `{target}` in one python code block.")
    return "".join(parts)


# ---------------------------------------------------------------- WorldModel


@dataclass
class EvoResult:
    best_files: dict[str, str]
    best_reward: float
    solved: bool
    trajectory: list[dict[str, Any]]  # 每个被评估状态一条 (供 Phase 3 归纳)
    stats: dict[str, Any]
    overfit: bool = False  # 过 train 却挂 holdout = 硬编码/过拟合测试, 上层应丢弃
    holdout_reward: float | None = None  # holdout 集复合分 (None = 未做 holdout 校验)


class EvoWorldModel:
    """oprim.WorldModel 的 veya 实现: 动作=evo 算子, step=LLM 生成, reward=沙盒探针。"""

    def __init__(
        self,
        task: str,
        base: EvoState,
        target_files: list[str],
        llm: Llm,
        pool: Any,
        probes_factory: ProbesFactory,
        *,
        n_branches: int = 4,
        max_depth: int = 3,
    ):
        self.task = task
        self.base = base
        self.targets = target_files
        self.llm = llm
        self.pool = pool
        self.probes_factory = probes_factory
        self.n_branches = n_branches
        self.max_depth = max_depth
        self._step_cache: dict[tuple[str, Any], EvoState] = {}
        self._reward_cache: dict[str, float] = {}  # 复合奖励 ∈[0,1], 供 MCTS 排序
        self._solved_cache: dict[str, bool] = {}  # 测试是否全绿, 供 terminal/solved 判定
        self._feedback: dict[str, str] = {}
        self.trajectory: list[dict[str, Any]] = []
        self.llm_calls = 0

    # --- WorldModel Protocol ---------------------------------------------
    def key(self, state: EvoState):
        return _digest(state.files)

    def actions(self, state: EvoState):
        if state.depth == 0:
            # 根节点: N 条并行初始草稿 (evo_draft × N), 等先验
            return [(("draft", i), 1.0 / self.n_branches) for i in range(self.n_branches)]
        if self._solved_cache.get(self.key(state), False):
            return []  # 测试已全绿, 无需再动
        # 未过测试 → 优先 debug; 过了但复合分不满(diff 大/部分通过) → improve
        return [(("debug", 0), 0.7), (("improve", 0), 0.3)]

    def step(self, state: EvoState, action) -> EvoState:
        ck = (self.key(state), action)
        if ck in self._step_cache:
            return self._step_cache[ck]
        op = action[0]
        target = self.targets[0]
        current = state.file_map.get(target, "")
        feedback = self._feedback.get(self.key(state), "") if op == "debug" else ""
        prompt = _op_prompt(op, self.task, target, current, feedback)
        self.llm_calls += 1
        new_content = extract_code(self.llm(prompt))
        new_files = state.file_map
        new_files[target] = new_content
        child = EvoState.of(new_files, depth=state.depth + 1, op=op)
        self._step_cache[ck] = child
        return child

    def terminal(self, state: EvoState) -> bool:
        # 终止 = 测试全绿 (而非复合分满分; 复合分含 diff 惩罚, 正确解也常 <1.0)
        return self._solved_cache.get(self.key(state), False) or state.depth >= self.max_depth

    def reward(self, state: EvoState) -> float:
        k = self.key(state)
        if k in self._reward_cache:
            return self._reward_cache[k]
        value, solved, feedback = self._evaluate(state)
        self._reward_cache[k] = value
        self._solved_cache[k] = solved
        self._feedback[k] = feedback
        self.trajectory.append(
            {
                "key": k,
                "op": state.op,
                "depth": state.depth,
                "reward": value,
                "solved": solved,
                "files": state.file_map,
                "feedback": feedback,
            }
        )
        return value

    # --- 沙盒评估 --------------------------------------------------------
    def _evaluate(self, state: EvoState) -> tuple[float, bool, str]:
        """返回 (复合奖励, 测试是否全绿, 反馈)。

        探针基线取 self.base (原始文件) 而非候选自身 —— 否则 DiffSizeProbe 的
        diff 恒为 0、diff 惩罚失效。"solved" 由 tests 探针满分判定 (无 tests 探针
        时回落复合分满分), 与复合分解耦: 复合分含 diff 惩罚, 正确解也常 <1.0。
        """
        import os

        sb = self.pool.acquire()
        try:
            for rel, content in state.files:
                path = os.path.join(sb.workspace, rel)
                os.makedirs(os.path.dirname(path) or sb.workspace, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
            probes = self.probes_factory(self.base.file_map, self.targets)
            rew = run_probes(probes, sb)
            solved = self._is_solved(rew)
            feedback = "" if solved else self._collect_feedback(sb, rew)
            return rew.value, solved, feedback
        finally:
            self.pool.release(sb)

    @staticmethod
    def _is_solved(rew: Any) -> bool:
        """测试全绿判定: tests 探针满分且未被 gate 否决; 无 tests 探针则回落复合分。"""
        if rew.gated:
            return False
        tests = [p for p in rew.probes if p.name == "tests"]
        if tests:
            return all(p.score >= 1.0 for p in tests)
        return rew.value >= 1.0

    @staticmethod
    def _collect_feedback(sb: Any, reward: Any) -> str:
        # 复跑 unittest 抓 stderr 给 debug 算子; 便宜且确定性
        r = sb.run(
            ["python3", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"], timeout_s=60
        )
        detail = reward.breakdown
        return f"[{detail}]\n{(r.stderr or r.stdout)[-2000:]}"


# ---------------------------------------------------------------- 顶层入口


def default_probes(workspace_files: dict[str, str], target_files: list[str]) -> list[Any]:
    """默认稠密奖励: 语法门(否决) + unittest 通过率(权重3) + diff 大小(权重1)。"""
    baseline = {t: workspace_files.get(t, "") for t in target_files}
    tests = CommandProbe(
        "tests",
        ["python3", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"],
        weight=3.0,
        gate=False,
        scorer=unittest_ratio,
        timeout_s=60,
    )
    return [
        py_syntax_gate(target_files),
        tests,
        DiffSizeProbe(baseline_files=baseline, weight=1.0, tau=40.0),
    ]


def evolve(
    task: str,
    workspace_files: dict[str, str],
    target_files: list[str],
    llm: Llm,
    *,
    n_branches: int = 8,
    budget: int = 40,
    max_depth: int = 4,
    probes_factory: ProbesFactory = default_probes,
    isolation: str = "netns",
    holdout_files: dict[str, str] | None = None,
) -> EvoResult:
    """OpenRSI 多分支演化搜索。返回沙盒验证过的最优候选 + 完整轨迹。

    workspace_files: 候选跑测所需的全部文件 (实现 + 测试 + 夹具)。
    target_files:    允许算子改写的文件 (测试文件不在其中 = 天然冻结)。
    holdout_files:   withheld 测试 (path→content, 须 test_*.py 命名)。搜索期从不进沙盒 ——
                     算子看不到、无法硬编码; 仅在选出 best 后叠加跑一次校验。best 过 train
                     却挂 holdout ⇒ EvoResult.overfit=True (reward-hacking 守卫)。
    """
    base = EvoState.of(workspace_files)
    pool = SandboxPool(size=n_branches, isolation=isolation).prewarm()
    try:
        model = EvoWorldModel(
            task,
            base,
            target_files,
            llm,
            pool,
            probes_factory,
            n_branches=n_branches,
            max_depth=max_depth,
        )
        mcts = MCTS(model, max_depth=max_depth)
        model.reward(base)  # 记录基线分, 便于对比
        # 增量搜索 + 早停: 一旦有候选测试全绿即停 (树/置换表跨调用复用)。
        # 这让 budget/n_branches 成为"难任务才用得到的上限"而非固定开销 ——
        # 简单任务几步即解, 难任务才真正铺开宽度/深度。
        for _ in range(budget):
            mcts.search(base, budget=1)
            if any(model._solved_cache.values()):
                break

        # 最优 = 先看测试是否全绿, 再按复合分 (含 diff 惩罚) 排序: 通关且最干净者胜
        def _rank(entry: dict[str, Any]) -> tuple[bool, float]:
            return (bool(entry.get("solved")), entry["reward"])

        best_entry = max(model.trajectory, key=_rank)

        # crossover: 若最优未通关且存在另一条较优独立分支, 融合两者再评一次
        if not best_entry.get("solved"):
            crossed = _try_crossover(model, task, target_files, llm)
            if crossed and _rank(crossed) > _rank(best_entry):
                best_entry = crossed

        # holdout 反作弊: train 上通关的 best, 用 withheld 测试再验一次 —— 过 train 却挂
        # holdout = 算子硬编码/过拟合了可见测试, 标记 overfit 供上层丢弃 (泛化未验证)。
        overfit = False
        holdout_reward: float | None = None
        if holdout_files and best_entry.get("solved"):
            holdout_reward, passed = _run_holdout(
                best_entry["files"], holdout_files, target_files, base, probes_factory, pool
            )
            overfit = not passed

        return EvoResult(
            best_files=best_entry["files"],
            best_reward=best_entry["reward"],
            solved=bool(best_entry.get("solved")),
            trajectory=model.trajectory,
            stats={
                "llm_calls": model.llm_calls,
                "simulations": mcts.stats.simulations,
                "expansions": mcts.stats.expansions,
                "transposition_hits": mcts.stats.transposition_hits,
                "evaluated": len(model._reward_cache),
                "overfit": overfit,
            },
            overfit=overfit,
            holdout_reward=holdout_reward,
        )
    finally:
        pool.shutdown()


def _run_holdout(
    best_files: dict[str, str],
    holdout_files: dict[str, str],
    target_files: list[str],
    base: EvoState,
    probes_factory: ProbesFactory,
    pool: Any,
) -> tuple[float, bool]:
    """把 withheld 测试叠加到 best 候选上跑一次: 返回 (holdout 复合分, 测试是否全绿)。

    holdout 测试在搜索期从不进沙盒 —— 算子看不到、无法硬编码, 故这次全绿=真泛化。
    train 测试仍一并跑 (叠加而非替换), 组合通过率 <1.0 即暴露 holdout 上的失败。
    """
    import os

    merged = dict(best_files)
    merged.update(holdout_files)  # 叠加 withheld 测试 (须 test_*.py 命名方能被 discover)
    sb = pool.acquire()
    try:
        for rel, content in merged.items():
            path = os.path.join(sb.workspace, rel)
            os.makedirs(os.path.dirname(path) or sb.workspace, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        probes = probes_factory(base.file_map, target_files)
        rew = run_probes(probes, sb)
        tests = [p for p in rew.probes if p.name == "tests"]
        passed = (not rew.gated) and (
            all(p.score >= 1.0 for p in tests) if tests else rew.value >= 1.0
        )
        return rew.value, passed
    finally:
        pool.release(sb)


def _try_crossover(
    model: EvoWorldModel, task: str, target_files: list[str], llm: Llm
) -> dict[str, Any] | None:
    """取奖励最高的两条独立候选, 让 LLM 融合双方之长, 沙盒再评一次。

    返回融合候选的轨迹条目 (含 solved/reward), 无可融合时 None。
    """
    ranked = sorted(
        (t for t in model.trajectory if t["op"] != "root"), key=lambda t: t["reward"], reverse=True
    )
    if len(ranked) < 2:
        return None
    a, b = ranked[0], ranked[1]
    target = target_files[0]
    prompt = (
        f"# TASK\n{task}\n\n# OPERATOR: crossover\n"
        "Two candidate implementations each got partway. Merge their strengths into "
        "one correct implementation that passes all tests.\n"
        f"\n# CANDIDATE A (reward={a['reward']:.2f})\n```python\n{a['files'].get(target, '')}```"
        f"\n# CANDIDATE B (reward={b['reward']:.2f})\n```python\n{b['files'].get(target, '')}```"
        f"\nReturn the complete merged content of `{target}` in one python code block."
    )
    model.llm_calls += 1
    merged = a["files"].copy()
    merged[target] = extract_code(llm(prompt))
    state = EvoState.of(merged, depth=1, op="crossover")
    model.reward(state)
    return next(t for t in model.trajectory if t["key"] == model.key(state))
