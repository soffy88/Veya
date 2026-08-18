"""server.paper_skill_scorer 测试 — knowledge skill 机械+LLM 复合评分。"""

from __future__ import annotations

from server.paper_skill_scorer import _llm_judge, make_scorer, mechanical_score
from server.skill_opt import optimize_skill

_CLEAN = (
    "---\n"
    "name: paper-x\n"
    'description: "分析某方法的收敛性与并行加速"\n'
    "---\n\n"
    "# X\n\n## 解决什么问题\n高维采样效率不足。\n\n## 何时使用\n- 贝叶斯逆问题采样\n"
)


def test_clean_skill_full_score():
    score, problems = mechanical_score(_CLEAN)
    assert score == 1.0 and problems == []


def test_missing_frontmatter_zero():
    score, problems = mechanical_score("# 没有 frontmatter\n正文")
    assert score == 0.0 and "缺 YAML frontmatter" in problems


def test_template_leak_penalized():
    bad = _CLEAN.replace('"分析某方法的收敛性与并行加速"', '"分析..."[:120]')
    score, problems = mechanical_score(bad)
    assert score < 1.0
    assert any("模板切片泄漏" in p for p in problems)


def test_todo_placeholder_penalized():
    bad = _CLEAN + "\n## 关系\n- (主题内边待补)\n"
    score, problems = mechanical_score(bad)
    assert score < 1.0 and any("待补" in p for p in problems)


def test_short_body_penalized():
    score, problems = mechanical_score('---\nname: x\ndescription: "d"\n---\n\n短')
    assert score < 1.0 and any("正文过短" in p for p in problems)


def test_missing_ref_attachment(tmp_path):
    md = _CLEAN + "\n查询时加载 `glossary.md` 与 `ku_index.md`。\n"
    (tmp_path / "glossary.md").write_text("term", encoding="utf-8")  # ku_index.md 缺失
    score, problems = mechanical_score(md, skill_dir=tmp_path)
    assert any("ku_index.md" in p for p in problems)
    assert not any("glossary.md" in p for p in problems)


def test_llm_judge_parses_number():
    assert _llm_judge(lambda p: "0.8", "skill", []) == 0.8
    assert _llm_judge(lambda p: "SCORE: 0.42 (good)", "skill", []) == 0.42
    assert _llm_judge(lambda p: "no number here", "skill", []) == 0.5  # 兜底中性
    assert _llm_judge(lambda p: "1.5", "skill", []) == 1.0  # clamp


def test_make_scorer_weights_mech_and_llm():
    # 机械满分(1.0) + LLM 给 0.5, mech_weight=0.4 → 0.4*1 + 0.6*0.5 = 0.7
    scorer = make_scorer(lambda p: "0.5", mech_weight=0.4)
    assert abs(scorer(_CLEAN, []) - 0.7) < 1e-9


def test_scorer_drives_optimize_skill():
    """真 scorer 接入 optimize_skill: 修好模板泄漏的候选应被门控接受。"""
    leaked = _CLEAN.replace('"分析某方法的收敛性与并行加速"', '"分析..."[:120]')
    fixed = _CLEAN
    scorer = make_scorer(lambda p: "0.5", mech_weight=1.0)  # 纯机械, 确定性
    r = optimize_skill(leaked, ["t"], scorer, lambda p: fixed, rounds=1)
    assert r.accepted == 1
    assert r.best_score > r.init_score  # 修好泄漏 → 机械分回到 1.0
    assert "[:120]" not in r.best_skill
