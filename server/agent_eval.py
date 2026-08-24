"""server.agent_eval — 独立 Eval 夹具: 把 `oskill.eval_suite`(纯统计原语)接到
真实 `MasterCoordinator.chat_stream()` 运行结果上(对标"Pi"清单 P2, 见 memory
project_veya_pi_gap_audit)。

此前 `eval_suite.py` 零 I/O、主链零处引用，从未接到真实 agent 运行结果——本
模块补上这条线：真实调用 `chat_stream()`(I/O 在这里发生，不在 oskill 里)，
用规则打分器把结果转成 [0,1] 分数，再交给 `oskill.eval_suite.run_suite` 走
真正的统计路径(mean/median/paired_t_test/cohens_d 等一律复用原语，不重新
实现——单一权威源约定, 见 `veya/platform/__init__.py` 文件头)。

用法(独立 CLI, 不进 pytest):
    python scripts/run_agent_eval.py
    python scripts/run_agent_eval.py --save baseline.json
    python scripts/run_agent_eval.py --compare-against baseline.json
"""

from __future__ import annotations

import statistics
from typing import Any

from veya.platform import oskill as _load_oskill

_load_oskill()
from oskill.eval_suite import EvalCase, EvalRun, compare_runs, run_suite  # noqa: E402

__all__ = [
    "AGENT_EVAL_CASES",
    "EvalCase",
    "EvalRun",
    "category_breakdown",
    "compare_runs",
    "cost_per_case",
    "eval_run_to_dict",
    "run_agent_eval_suite",
    "unnecessary_tool_rate",
]

# 用例覆盖三种代表性场景: 直接回答 / 工具成功 / 工具失败后恢复
# (跟 tests/test_master_tools.py 的既有断言场景保持一致, 不额外发明新契约)。
#
# 2026-08-23 新增 5 条(用户诉求: 更智能/更准确的回复, 别动不动就调工具, 见
# docs/dev/rfc-05-cognitive-policy.md)——直接取自用户给的 spec 文档 §38.1-38.5
# 的原始场景例句, 不是另外发明的用例。§38.6(worker claim 语义) 不在这里测:
# 那是"结果怎么被对待"的语义问题, 不是"调不调工具"的次数问题, 这个打分器的
# max/min_tool_calls + tool 断言机制回答不了那类问题。
# meta.category 按 docs/VEYA_10_OF_10_PLAN.md §16 的分类打标(2026-08-24, rfc-09)。
# 这是诚实分类既有用例, 不是新写用例——8 条里 7 条落在 tool_selection, 只有
# 1 条 recovery、1 条 delegation, coding/research/long_task/safety/context
# 五个类目目前零覆盖。故意不为了"看起来分布均匀"去凑新用例, 缺口就是缺口,
# 记在 rfc-09 里, 不用假用例填。
AGENT_EVAL_CASES: list[EvalCase] = [
    EvalCase(
        id="direct_answer",
        input="你好,你是谁?",
        expected={"max_tool_calls": 0},
        meta={"category": "tool_selection"},
    ),
    EvalCase(
        id="tool_success",
        input="列出当前目录文件",
        expected={"tool": "list_files", "tool_status": "success"},
        meta={"category": "tool_selection"},
    ),
    EvalCase(
        id="tool_failure_recovery",
        input="看看 missing_file_xyz.py 里有什么",
        expected={"tool": "read_file_ast"},
        meta={"category": "recovery"},
    ),
    # §38.1 Stable Knowledge: 稳定知识类问题不该调任何工具。
    EvalCase(
        id="stable_knowledge_no_tools",
        input="什么是闭包？",
        expected={"max_tool_calls": 0},
        meta={"category": "tool_selection"},
    ),
    # §38.2 Complex Reasoning: 问题"难"不是调工具的理由, 该直接推理。
    EvalCase(
        id="complex_reasoning_no_tools",
        input="从认识论、语言哲学和认知科学三个角度分析 self-talk。",
        expected={"max_tool_calls": 0},
        meta={"category": "tool_selection"},
    ),
    # §38.3 Actual Repository State: 涉及仓库真实状态, 该先查再答, 不能凭猜测。
    EvalCase(
        id="repo_state_needs_inspection",
        input="Veya 当前 Python 版本是多少？",
        expected={"min_tool_calls": 1},
        meta={"category": "tool_selection"},
    ),
    # §38.4 Runtime Feasibility: 需要验证运行时事实时, 该用 sandbox 做最小探测。
    EvalCase(
        id="runtime_feasibility_needs_probe",
        input="当前环境里 pydantic 能 import 吗？",
        expected={"tool": "run_in_sandbox"},
        meta={"category": "tool_selection"},
    ),
    # §38.5 Code Modification: 改代码+跑测试该派工 hicode_run, 不是在聊天里手写patch。
    EvalCase(
        id="code_modification_needs_delegation",
        input="修复 auth.py 并跑测试。",
        expected={"tool": "hicode_run"},
        meta={"category": "delegation"},
    ),
]


def _score_result(case: EvalCase, result: dict[str, Any]) -> float:
    """规则打分: status!=success 直接 0 分; 否则底分 0.5 + 命中 expected 条件加分。

    刻意用简单规则而非 LLM-judge——先把"真实结果流进统计管线"这条线接通,
    评分器本身是可插拔的(签名跟 `Scorer = Callable[[EvalCase], float]` 一致),
    以后要换 LLM-judge 只需换这个函数, 不动 run_agent_eval_suite。
    """
    if result.get("status") != "success":
        return 0.0
    expected = case.expected or {}
    if not expected:
        return 1.0
    score = 0.5
    tool_calls = result.get("tool_calls") or []
    if "max_tool_calls" in expected:
        score += 0.5 if len(tool_calls) <= expected["max_tool_calls"] else 0.0
    if "min_tool_calls" in expected:
        score += 0.5 if len(tool_calls) >= expected["min_tool_calls"] else 0.0
    if "tool" in expected:
        want_status = expected.get("tool_status")
        matched = any(tc.get("tool") == expected["tool"] for tc in tool_calls)
        if want_status is not None:
            matched = any(
                tc.get("tool") == expected["tool"] and tc.get("status") == want_status
                for tc in tool_calls
            )
        score += 0.5 if matched else 0.0
    return max(0.0, min(1.0, score))


async def run_agent_eval_suite(
    coord: Any,
    cases: list[EvalCase] | None = None,
    *,
    suite_name: str = "agent_eval",
    max_rounds: int = 4,
) -> EvalRun:
    """真实跑一遍每个用例的 `chat_stream()`, 再用 `run_suite` 打分/汇总。

    每个用例独立 session_id(`eval-<case.id>`), 互不污染历史。单个用例异常
    (超时/网络错误)只影响该用例记 0 分, 不拖垮整个 suite。
    """
    cases = cases if cases is not None else AGENT_EVAL_CASES
    real_results: dict[str, dict[str, Any]] = {}
    for case in cases:
        try:
            real_results[case.id] = await coord.chat_stream(
                case.input, session_id=f"eval-{case.id}", max_rounds=max_rounds
            )
        except Exception as exc:
            real_results[case.id] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    def scorer(case: EvalCase) -> float:
        return _score_result(case, real_results[case.id])

    run = run_suite(cases, scorer, suite_name=suite_name)
    for case in cases:
        run.details.setdefault(case.id, {})
        run.details[case.id]["agent_result"] = real_results[case.id]
    return run


def category_breakdown(run: EvalRun, cases: list[EvalCase]) -> dict[str, dict[str, Any]]:
    """docs/VEYA_10_OF_10_PLAN.md §16 核心指标(2026-08-24, rfc-09)——按
    `EvalCase.meta["category"]` 分组均分。用真实跑分数据算, 不是新造指标口径。
    """
    by_cat: dict[str, list[float]] = {}
    for case in cases:
        cat = case.meta.get("category", "uncategorized")
        if case.id in run.scores:
            by_cat.setdefault(cat, []).append(run.scores[case.id])
    return {
        cat: {"n": len(scores), "mean": statistics.fmean(scores)}
        for cat, scores in sorted(by_cat.items())
    }


def unnecessary_tool_rate(run: EvalRun, cases: list[EvalCase]) -> float | None:
    """docs/VEYA_10_OF_10_PLAN.md §16 "Unnecessary Tool Rate"。

    只统计 expected 里写了 max_tool_calls=0(该场景本该零工具调用)的用例中,
    真实调用了 >0 个工具的比例——不是全量用例的笼统统计, 那样会跟"该调用工具
    但调多了"的场景混在一起, 口径不干净。没有任何 max_tool_calls=0 用例时返回
    None(不伪造一个 0.0, 那看起来像"全部通过"而不是"没数据")。
    """
    relevant = [c for c in cases if (c.expected or {}).get("max_tool_calls") == 0]
    if not relevant:
        return None
    triggered = 0
    for case in relevant:
        agent_result = run.details.get(case.id, {}).get("agent_result", {})
        tool_calls = agent_result.get("tool_calls") or []
        if len(tool_calls) > 0:
            triggered += 1
    return triggered / len(relevant)


def cost_per_case(run: EvalRun) -> dict[str, float]:
    """docs/VEYA_10_OF_10_PLAN.md §16 "Cost / Successful Task"——从 chat_stream()
    结果里已经带的 cost_usd 读, 不新增采集链路。缺失字段的用例不进这份表
    (不伪造 0.0 成本)。"""
    costs: dict[str, float] = {}
    for case_id, detail in run.details.items():
        cost = detail.get("agent_result", {}).get("cost_usd")
        if isinstance(cost, int | float):
            costs[case_id] = float(cost)
    return costs


def eval_run_to_dict(run: EvalRun, cases: list[EvalCase] | None = None) -> dict[str, Any]:
    """EvalRun → JSON 可序列化 dict(供 --save / --compare-against 落盘用)。

    cases 传入时附带 §16 派生指标(category_breakdown/unnecessary_tool_rate/
    cost_per_case); 不传(如 eval_run_from_dict 还原出的历史基线, 已经丢了
    meta)时只输出 summary, 不假装能算出用不存在的分类数据。
    """
    payload: dict[str, Any] = {
        "suite_name": run.suite_name,
        "scores": run.scores,
        "details": run.details,
        "summary": run.summary(),
    }
    if cases is not None:
        payload["category_breakdown"] = category_breakdown(run, cases)
        payload["unnecessary_tool_rate"] = unnecessary_tool_rate(run, cases)
        payload["cost_per_case"] = cost_per_case(run)
    return payload


def eval_run_from_dict(data: dict[str, Any]) -> EvalRun:
    """`eval_run_to_dict` 的逆操作, 从落盘 JSON 还原 EvalRun(供 compare_runs 用)。"""
    run = EvalRun(suite_name=data["suite_name"])
    run.scores = dict(data["scores"])
    run.details = dict(data.get("details", {}))
    return run
