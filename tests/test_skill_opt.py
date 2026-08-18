"""server.skill_opt 测试 — 验证门控的 skill 文档迭代优化 + holdout 反 overfit。

补 microsoft/SkillOpt 调研对比出的空档: skill 文档的迭代优化闭环 (改→门控→留优)。
确定性 stub scorer/optimizer, 不触真实 LLM/任务。
"""

from __future__ import annotations

from server.skill_opt import default_extract, optimize_skill


def _step_scorer(skill: str, tasks: list) -> float:
    """train 打分: skill 里 STEP 越多越好 (模拟"更结构化的指令更强"), 封顶 1.0。"""
    return min(1.0, skill.count("STEP") / 3.0)


def _dual_scorer(skill: str, tasks: list) -> float:
    """按任务集区分: holdout 看是否保留 GENERAL 泛化指导; train 看 STEP 数。"""
    if tasks == ["holdout"]:
        return 1.0 if "GENERAL" in skill else 0.0
    return min(1.0, skill.count("STEP") / 3.0)


def _optimizer(*versions: str):
    """依次吐出预设候选, 用尽后固定返回最后一个。"""
    seq = list(versions)
    it = iter(seq)

    def llm(_prompt: str) -> str:
        return next(it, seq[-1])

    return llm


# ── 提取器 ──────────────────────────────────────────────────────────
def test_default_extract_strips_fence():
    assert default_extract("```markdown\nHELLO\n```") == "HELLO"
    assert default_extract("```\nX\nY\n```") == "X\nY"
    assert default_extract("  plain text  ") == "plain text"


# ── 验证门控三态 ─────────────────────────────────────────────────────
def test_accepts_improving_edits():
    r = optimize_skill(
        "draft",
        ["t"],
        _step_scorer,
        _optimizer("STEP 1", "STEP 1\nSTEP 2", "STEP 1\nSTEP 2\nSTEP 3"),
        rounds=3,
    )
    assert r.accepted == 3 and r.rejected == 0
    assert r.best_score == 1.0 and r.init_score == 0.0
    assert "STEP 3" in r.best_skill
    assert r.improved is True


def test_rejects_regressing_edits_keeps_best():
    r = optimize_skill(
        "STEP a\nSTEP b\nSTEP c",  # 初稿已满分
        ["t"],
        _step_scorer,
        _optimizer("worse", "also worse"),
        rounds=3,
    )
    assert r.accepted == 0 and r.rejected == 3
    assert r.best_score == 1.0
    assert r.best_skill == "STEP a\nSTEP b\nSTEP c"  # 留优: 坏编辑不污染 best


def test_score_monotonic_only_best_kept():
    # good 被接受后, 后续更差候选被拒 → best_score 单调不降。
    r = optimize_skill(
        "draft",
        ["t"],
        _step_scorer,
        _optimizer("STEP 1\nSTEP 2\nSTEP 3", "bad", "STEP 1"),
        rounds=3,
    )
    assert r.accepted == 1 and r.rejected == 2
    assert r.best_score == 1.0


def test_noop_proposal_rejected():
    r = optimize_skill("SAME", ["t"], _step_scorer, _optimizer("SAME"), rounds=2)
    assert r.accepted == 0 and r.rejected == 2


# ── holdout 反 overfit ──────────────────────────────────────────────
def test_holdout_catches_overfit():
    """加 STEP 提升 train, 却删掉 GENERAL 泛化指导 → holdout 退步 = overfit。"""
    r = optimize_skill(
        "GENERAL",
        ["train"],
        _dual_scorer,
        _optimizer("STEP 1\nSTEP 2\nSTEP 3"),  # 删了 GENERAL
        rounds=1,
        holdout_tasks=["holdout"],
    )
    assert r.overfit is True
    assert r.improved is False  # overfit ⇒ 不算真提升


def test_holdout_passes_genuine_improvement():
    """既加 STEP 又保留 GENERAL → train 升、holdout 不退 = 真泛化。"""
    r = optimize_skill(
        "GENERAL",
        ["train"],
        _dual_scorer,
        _optimizer("GENERAL\nSTEP 1\nSTEP 2\nSTEP 3"),
        rounds=1,
        holdout_tasks=["holdout"],
    )
    assert r.overfit is False
    assert r.improved is True
    assert r.holdout_score == 1.0


def test_no_holdout_no_overfit():
    r = optimize_skill(
        "draft",
        ["t"],
        _step_scorer,
        _optimizer("STEP 1\nSTEP 2\nSTEP 3"),
        rounds=1,
    )
    assert r.overfit is False and r.holdout_score is None
    assert r.improved is True
