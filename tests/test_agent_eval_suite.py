"""独立 Eval 夹具接线证明(对标"Pi"清单 P2, 见 memory project_veya_pi_gap_audit)。

之前 `oskill.eval_suite` 是零 I/O 纯统计原语, 主链零处引用, 从未接到真实
agent 运行结果。这里注入确定性 `fake_llm`(CI 可重复、零成本)真实跑
`MasterCoordinator.chat_stream()`, 证明 `server/agent_eval.py`
这条线接通了——分数来自真实工具调用/回答, 不是预置的假分数。
"""

from __future__ import annotations

import json

import pytest
from oskill.eval_suite import EvalRun

from server.agent_eval import (
    AGENT_EVAL_CASES,
    EvalCase,
    _score_result,
    category_breakdown,
    compare_runs,
    cost_per_case,
    run_agent_eval_suite,
    unnecessary_tool_rate,
)
from server.coordinator_master import MasterCoordinator


def _text_response(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {}}


def _tool_response(name: str, args: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            }
        ],
        "usage": {},
    }


async def _good_llm(messages, **kwargs):
    """按最近一条消息内容路由, 覆盖 AGENT_EVAL_CASES 全部场景(含 2026-08-23 新增
    的 5 个 §38.1-38.5 场景), 全部成功收尾。"""
    last = messages[-1]["content"] if messages else ""
    if "[Tool list_files" in last:
        return _text_response("目录文件已列出。")
    if "[Tool read_file_ast" in last:
        return _text_response("文件不存在, 已改用其他方式确认。")
    if "[Tool run_in_sandbox" in last:
        return _text_response("import 成功, pydantic 可用。")
    if "[Tool hicode_run" in last:
        return _text_response("已修复 auth.py 并跑通测试。")
    if "列出当前目录文件" in last:
        return _tool_response("list_files", {"path": "."})
    if "missing_file_xyz.py" in last:
        return _tool_response("read_file_ast", {"filepath": "missing_file_xyz.py"})
    if "Python 版本" in last:
        return _tool_response("read_file_ast", {"filepath": "pyproject.toml"})
    if "pydantic" in last:
        return _tool_response("run_in_sandbox", {"code": "import pydantic"})
    if "auth.py" in last:
        return _tool_response("hicode_run", {"instruction": "fix auth.py and run tests"})
    # 剩余场景(闭包定义/复杂哲学推理)按 §38.1/38.2 预期直接回答, 不调工具。
    return _text_response("你好! 我是 Veya 主脑。")


async def _bad_llm(messages, **kwargs):
    """ "退化候选": 从不调工具, 只会道歉——用于验证 compare_runs 真能测出回归。"""
    return _text_response("抱歉, 我暂时无法处理这个请求。")


def test_score_result_min_tool_calls():
    """2026-08-23 新增(§38.3 类场景需要断言"至少调过一次工具")。"""
    case = EvalCase(id="c", input="x", expected={"min_tool_calls": 1})

    below = _score_result(case, {"status": "success", "tool_calls": []})
    at_least = _score_result(case, {"status": "success", "tool_calls": [{"tool": "grep"}]})

    assert below == 0.5  # 底分, 未命中 min_tool_calls 不加分
    assert at_least == 1.0


def test_score_result_max_tool_calls_still_works():
    case = EvalCase(id="c", input="x", expected={"max_tool_calls": 0})

    zero_calls = _score_result(case, {"status": "success", "tool_calls": []})
    one_call = _score_result(case, {"status": "success", "tool_calls": [{"tool": "grep"}]})

    assert zero_calls == 1.0
    assert one_call == 0.5


def test_category_breakdown_groups_by_meta():
    """rfc-09: 按 EvalCase.meta['category'] 分组, 未跑到的用例(不在 run.scores
    里)不进统计——不伪造缺失分数。"""
    cases = [
        EvalCase(id="a", input="x", meta={"category": "tool_selection"}),
        EvalCase(id="b", input="x", meta={"category": "tool_selection"}),
        EvalCase(id="c", input="x", meta={"category": "recovery"}),
        EvalCase(id="d", input="x"),  # 没打标 → uncategorized
    ]
    run = EvalRun(suite_name="s", scores={"a": 1.0, "b": 0.5, "c": 0.0})  # d 没跑到

    breakdown = category_breakdown(run, cases)

    assert breakdown == {
        "recovery": {"n": 1, "mean": 0.0},
        "tool_selection": {"n": 2, "mean": 0.75},
    }
    assert "uncategorized" not in breakdown


def test_unnecessary_tool_rate_only_counts_zero_tool_cases():
    cases = [
        EvalCase(id="zero1", input="x", expected={"max_tool_calls": 0}),
        EvalCase(id="zero2", input="x", expected={"max_tool_calls": 0}),
        EvalCase(id="needs_tool", input="x", expected={"min_tool_calls": 1}),
    ]
    run = EvalRun(
        suite_name="s",
        scores={"zero1": 0.5, "zero2": 1.0, "needs_tool": 1.0},
        details={
            "zero1": {"agent_result": {"tool_calls": [{"tool": "grep"}]}},  # 违规: 本不该调
            "zero2": {"agent_result": {"tool_calls": []}},
            "needs_tool": {"agent_result": {"tool_calls": [{"tool": "read_file_ast"}]}},
        },
    )

    rate = unnecessary_tool_rate(run, cases)

    assert rate == 0.5  # zero1/zero2 里一个违规, needs_tool 不计入分母


def test_unnecessary_tool_rate_none_when_no_relevant_cases():
    cases = [EvalCase(id="a", input="x", expected={"min_tool_calls": 1})]
    run = EvalRun(suite_name="s", scores={"a": 1.0})

    assert unnecessary_tool_rate(run, cases) is None


def test_cost_per_case_skips_missing_field():
    run = EvalRun(
        suite_name="s",
        scores={"a": 1.0, "b": 1.0},
        details={
            "a": {"agent_result": {"cost_usd": 0.002}},
            "b": {"agent_result": {}},  # 没有 cost_usd, 不伪造 0.0
        },
    )

    assert cost_per_case(run) == {"a": 0.002}


@pytest.mark.asyncio
async def test_agent_eval_suite_runs_real_chat_stream_and_scores(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    coord = MasterCoordinator(llm_fn=_good_llm, max_rounds=3)

    run = await run_agent_eval_suite(coord)

    assert run.suite_name == "agent_eval"
    assert set(run.scores) == {c.id for c in AGENT_EVAL_CASES}
    # 分数来自真实 chat_stream 结果, 不是预置假分数
    for case in AGENT_EVAL_CASES:
        agent_result = run.details[case.id]["agent_result"]
        assert agent_result["status"] == "success"
    assert run.scores["tool_success"] == 1.0  # list_files 命中, 满分
    summary = run.summary()
    assert summary["n"] == len(AGENT_EVAL_CASES)
    assert summary["mean"] > 0.5


@pytest.mark.asyncio
async def test_agent_eval_suite_compare_runs_detects_regression(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    baseline_coord = MasterCoordinator(llm_fn=_good_llm, max_rounds=3)
    candidate_coord = MasterCoordinator(llm_fn=_bad_llm, max_rounds=3)

    baseline = await run_agent_eval_suite(baseline_coord, suite_name="baseline")
    candidate = await run_agent_eval_suite(candidate_coord, suite_name="candidate")

    report = compare_runs(baseline, candidate)

    assert report.candidate_wins is False
    assert report.t_test["mean_diff"] < 0  # 退化候选分数更低
    assert candidate.summary()["mean"] < baseline.summary()["mean"]


@pytest.mark.asyncio
async def test_agent_eval_suite_isolates_case_exceptions(tmp_path, monkeypatch):
    """单个用例的 chat_stream 抛异常, 只影响该用例记 0 分, 不拖垮其余用例。"""
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))

    async def flaky_llm(messages, **kwargs):
        last = messages[-1]["content"] if messages else ""
        if "列出当前目录文件" in last:
            raise RuntimeError("网络超时(模拟)")
        return await _good_llm(messages, **kwargs)

    coord = MasterCoordinator(llm_fn=flaky_llm, max_rounds=3)
    run = await run_agent_eval_suite(coord)

    assert run.scores["tool_success"] == 0.0
    assert run.details["tool_success"]["agent_result"]["status"] != "success"
    assert run.scores["direct_answer"] > 0.0  # 其余用例不受影响


@pytest.mark.asyncio
async def test_agent_eval_suite_accepts_custom_cases(tmp_path, monkeypatch):
    """接受自定义用例集(不是只能跑写死的 AGENT_EVAL_CASES), 校验可扩展性。"""
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    coord = MasterCoordinator(llm_fn=_good_llm, max_rounds=3)
    custom_cases = [EvalCase(id="hello_only", input="你好,你是谁?", expected={"max_tool_calls": 0})]

    run = await run_agent_eval_suite(coord, cases=custom_cases, suite_name="custom")

    assert run.suite_name == "custom"
    assert set(run.scores) == {"hello_only"}
