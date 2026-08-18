"""server.skill_opt — skill 文档迭代优化 (microsoft/SkillOpt 范式内化)。

把 skill 文档当"可训练状态": 一个 optimizer LLM 读当前 skill + 打分反馈 → 提议
**有界文本编辑** (小步 add/delete/replace, 不重写) → 在任务集上评估 → **验证门控**
(分不升不接受, 单调改进) → 留优。可选 held-out 任务集反 overfit: train 升但
holdout 退 ⇒ overfit=True (弃用该轮结果)。

与 [[server.openrsi]] 同范式 (改→验证门控→留优 + holdout 反 reward-hacking),
openrsi.evolve 是其**代码版** (extract_code + 沙盒跑测试); 本模块是**文本版**:
skill 评估不走代码沙盒, 由可注入 ``scorer`` 在任务上打分 —— 生产侧把 scorer 接到
goal_run 机械验证 (server/goal_run/verify) 或 LLM-judge 即可。

刻意**不抄** SkillOpt 的完整离线训练框架 (epoch / mini-batch / learning-rate /
webui / sleep CLI): 只内化"验证门控的迭代文本优化 + holdout"这一核心原语。纯函数
式 (无 I/O; LLM 与 scorer 均注入), 可确定性测试。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# 同步 LLM 契约 (对齐 openrsi.Llm): 输入完整 prompt, 输出模型文本。
Llm = Callable[[str], str]
# 评分器: 给定 (skill 文档, 任务列表) → 复合分 (约定 [0,1], 越高越好)。
# 生产接线: 用 skill 作为 system/指令在 tasks 上跑 agent, 复用 goal_run 机械验证打分。
SkillScorer = Callable[[str, list[Any]], float]
# 提取器: optimizer 输出文本 → 干净 skill 文档 (默认剥 code fence)。可注入。
Extractor = Callable[[str], str]

_FENCE = re.compile(r"^```[a-zA-Z]*\n(.*?)```\s*$", re.DOTALL)


def default_extract(text: str) -> str:
    """从 optimizer 输出剥出 skill 文档: 去外层 ```fence```, 否则原样 strip。"""
    text = (text or "").strip()
    m = _FENCE.match(text)
    return (m.group(1) if m else text).strip()


def _optimizer_prompt(skill: str, score: float, worst: str | None) -> str:
    parts = [
        "# ROLE\nYou optimize a reusable natural-language SKILL document that guides an "
        "agent on a family of tasks. Improve it under feedback like a training loop.",
        f"\n# CURRENT SKILL (score={score:.3f})\n{skill}",
    ]
    if worst:
        parts.append(f"\n# WEAKEST OBSERVED CASE (address this)\n{worst[:1500]}")
    parts.append(
        "\n# EDIT RULES\nMake a BOUNDED edit (add/delete/replace a few lines) that raises "
        "the score. Do NOT rewrite wholesale, do not pad. Keep it concise (300-2000 tokens). "
        "Return ONLY the complete new skill document, no commentary."
    )
    return "".join(parts)


@dataclass
class SkillOptResult:
    """skill 迭代优化结果 (留优候选 + 门控轨迹)。"""

    best_skill: str
    best_score: float
    init_score: float
    accepted: int = 0
    rejected: int = 0
    holdout_score: float | None = None  # None = 未做 holdout 校验
    overfit: bool = False  # train 升但 holdout 退 = 过拟合可见任务, 上层应丢弃
    trace: list[dict] = field(default_factory=list)

    @property
    def improved(self) -> bool:
        """相对初稿是否真有提升 (且未 overfit)。"""
        return self.best_score > self.init_score and not self.overfit


def optimize_skill(
    skill_md: str,
    tasks: list[Any],
    scorer: SkillScorer,
    optimizer_llm: Llm,
    *,
    rounds: int = 6,
    holdout_tasks: list[Any] | None = None,
    extractor: Extractor = default_extract,
    worst_case: Callable[[str, list[Any]], str | None] | None = None,
) -> SkillOptResult:
    """验证门控的 skill 文档迭代优化。

    每轮: optimizer 提议有界编辑 → 在 ``tasks`` 上评估 → 分**严格提升**才接受 (否则
    弃该轮, best 不变)。单调改进, 不会因一次坏编辑退化。

    holdout_tasks: withheld 任务集, 搜索期从不喂给 optimizer。选出 best 后校验一次;
        best 在 train 升但在 holdout 比初稿退 ⇒ overfit=True (泛化未成立, 上层丢弃)。
    scorer: (skill, tasks) → [0,1]; 生产接 goal_run/verify 机械验证或 LLM-judge。
    worst_case: 可选, (skill, tasks) → 最差样本文本, 喂给 optimizer 聚焦改进。
    """
    init_score = scorer(skill_md, tasks)
    best, best_score = skill_md, init_score
    result = SkillOptResult(best_skill=best, best_score=best_score, init_score=init_score)

    for r in range(rounds):
        worst = worst_case(best, tasks) if worst_case else None
        proposal = extractor(optimizer_llm(_optimizer_prompt(best, best_score, worst)))
        if not proposal or proposal == best:
            result.rejected += 1
            result.trace.append({"round": r, "action": "reject", "reason": "empty/no-op"})
            continue
        new_score = scorer(proposal, tasks)
        if new_score > best_score:  # 验证门控: 严格提升才接受
            best, best_score = proposal, new_score
            result.accepted += 1
            result.trace.append({"round": r, "action": "accept", "score": new_score})
        else:
            result.rejected += 1
            result.trace.append({"round": r, "action": "reject", "score": new_score})

    result.best_skill, result.best_score = best, best_score

    # holdout 反 overfit: best 在 withheld 任务上退步于初稿 = 只学会了可见任务。
    if holdout_tasks:
        init_holdout = scorer(skill_md, holdout_tasks)
        best_holdout = scorer(best, holdout_tasks)
        result.holdout_score = best_holdout
        if best_score > init_score and best_holdout < init_holdout:
            result.overfit = True

    return result
