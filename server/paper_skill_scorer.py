"""server.paper_skill_scorer — knowledge/paper skill 的机械 + LLM 复合评分器。

给 [[server.skill_opt]] 的 optimize_skill 提供真实 scorer: 评一份从论文/概念蒸馏
出的 knowledge skill (~/.agents/paper-skills-archive/ 那 2000+ 个) 好不好, 输出
[0,1] 复合分驱动迭代优化。

- **机械分** (纯静态, 无 LLM, 可判定): frontmatter 合法 / description 无模板切片
  泄漏 (生成器 bug `"..."[:120]`) / 无"待补"占位 / 正文非空 / 引用的 *.md 附件存在。
- **LLM 分** (注入 llm 判内容质量): 概括是否忠实自洽、"何时使用"是否可操作、有无
  幻觉、description 与正文是否一致 (中文 description 截断这类语义问题机械判不准,
  交给 LLM)。

复合 = mech_weight·机械 + (1-mech_weight)·LLM。llm 注入 → 可确定性测试 (stub)。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

_FM = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_TEMPLATE_LEAK = re.compile(r"\[:\d+\]")  # description "..."[:120] 模板切片泄漏进 YAML
_REF_MD = re.compile(r"`([a-z0-9_]+\.md)`")  # 正文里 `glossary.md` 式附件引用

# 各缺陷扣分 (从 1.0 起扣, clamp≥0)。未列出的问题按 _DEFAULT_PENALTY。
_PENALTY: dict[str, float] = {
    "缺 YAML frontmatter": 1.0,
    "frontmatter 缺 name": 0.3,
    "frontmatter 缺 description": 0.4,
    "description 模板切片泄漏 (如 [:120])": 0.25,
    "含 '待补' 未完成占位": 0.2,
    "正文过短/空": 0.5,
}
_DEFAULT_PENALTY = 0.15  # 引用附件缺失等


def mechanical_score(
    skill_md: str, *, skill_dir: str | Path | None = None
) -> tuple[float, list[str]]:
    """纯静态结构检查, 返回 (分 [0,1], 问题列表)。不调 LLM。

    skill_dir 给定时校验正文引用的 ``*.md`` 附件是否真实存在。
    """
    problems: list[str] = []
    fm = _FM.match(skill_md or "")
    if not fm:
        return 0.0, ["缺 YAML frontmatter"]
    front = fm.group(1)
    body = skill_md[fm.end() :]

    if not re.search(r"^name:\s*\S", front, re.MULTILINE):
        problems.append("frontmatter 缺 name")
    dm = re.search(r"^description:\s*(.+)$", front, re.MULTILINE)
    if not dm or not dm.group(1).strip().strip('"'):
        problems.append("frontmatter 缺 description")
    elif _TEMPLATE_LEAK.search(front):
        problems.append("description 模板切片泄漏 (如 [:120])")
    if "待补" in skill_md:
        problems.append("含 '待补' 未完成占位")
    if len(body.strip()) < 40:
        problems.append("正文过短/空")
    if skill_dir is not None:
        d = Path(skill_dir)
        for ref in sorted(set(_REF_MD.findall(skill_md))):
            if not (d / ref).is_file():
                problems.append(f"引用附件缺失: {ref}")

    score = 1.0
    for p in problems:
        score -= _PENALTY.get(p, _DEFAULT_PENALTY)
    return max(0.0, score), problems


_JUDGE_PROMPT = """You grade a KNOWLEDGE SKILL distilled from a research paper or concept.
Score it 0.0-1.0 on: faithful & self-consistent summary; actionable "when to use";
no hallucinated claims; the frontmatter description matches the body (flag truncated
or mismatched descriptions).{tasks_clause}

# SKILL
{skill}

Reply with ONLY a number between 0.0 and 1.0."""


def _llm_judge(llm: Callable[[str], str], skill_md: str, tasks: list[Any]) -> float:
    """LLM 按 rubric 给内容质量打 [0,1]。解析不出数字 → 中性 0.5。"""
    tasks_clause = ""
    if tasks:
        tasks_clause = "\nAlso weigh whether it can support these queries: " + "; ".join(
            str(t) for t in tasks[:5]
        )
    out = llm(_JUDGE_PROMPT.format(skill=skill_md[:6000], tasks_clause=tasks_clause))
    m = re.search(r"\d*\.?\d+", out or "")
    if not m:
        return 0.5
    return max(0.0, min(1.0, float(m.group(0))))


def make_scorer(
    llm: Callable[[str], str],
    *,
    skill_dir: str | Path | None = None,
    mech_weight: float = 0.4,
) -> Callable[[str, list[Any]], float]:
    """构造 optimize_skill 用的 scorer: mech_weight·机械 + (1-mech_weight)·LLM。

    skill_dir: 被优化 skill 的目录 (校验附件引用)。mech_weight∈[0,1] 调机械/内容配比。
    """
    mech_weight = max(0.0, min(1.0, mech_weight))

    def scorer(skill_md: str, tasks: list[Any]) -> float:
        mech, _ = mechanical_score(skill_md, skill_dir=skill_dir)
        judged = _llm_judge(llm, skill_md, tasks)
        return mech_weight * mech + (1.0 - mech_weight) * judged

    return scorer
