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

from typing import Any

from veya.platform import oskill as _load_oskill

_load_oskill()
from oskill.eval_suite import EvalCase, EvalRun, compare_runs, run_suite  # noqa: E402

__all__ = [
    "EvalCase",
    "EvalRun",
    "compare_runs",
    "AGENT_EVAL_CASES",
    "run_agent_eval_suite",
    "eval_run_to_dict",
]

# 用例覆盖三种代表性场景: 直接回答 / 工具成功 / 工具失败后恢复
# (跟 tests/test_master_tools.py 的既有断言场景保持一致, 不额外发明新契约)。
AGENT_EVAL_CASES: list[EvalCase] = [
    EvalCase(
        id="direct_answer",
        input="你好,你是谁?",
        expected={"max_tool_calls": 0},
    ),
    EvalCase(
        id="tool_success",
        input="列出当前目录文件",
        expected={"tool": "list_files", "tool_status": "success"},
    ),
    EvalCase(
        id="tool_failure_recovery",
        input="看看 missing_file_xyz.py 里有什么",
        expected={"tool": "read_file_ast"},
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


def eval_run_to_dict(run: EvalRun) -> dict[str, Any]:
    """EvalRun → JSON 可序列化 dict(供 --save / --compare-against 落盘用)。"""
    return {
        "suite_name": run.suite_name,
        "scores": run.scores,
        "details": run.details,
        "summary": run.summary(),
    }


def eval_run_from_dict(data: dict[str, Any]) -> EvalRun:
    """`eval_run_to_dict` 的逆操作, 从落盘 JSON 还原 EvalRun(供 compare_runs 用)。"""
    run = EvalRun(suite_name=data["suite_name"])
    run.scores = dict(data["scores"])
    run.details = dict(data.get("details", {}))
    return run
