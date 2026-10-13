"""W18 — DeepEval 适配层测试(离线可跑, 用假裁判模型, 零成本零网络)。

分两部分:

- **Part A**(在共享 .venv 里跑, deepeval 未安装): 验证优雅降级 +
  `agent_eval` 集成 + W12 supervisor 报告的附加字段(flag 关时不改变结构)。
- **Part B**(需 deepeval 已装, 用隔离 venv `.w18venv` 跑): 验证适配层真实
  机制——假裁判走通指标、坏裁判/超时降级为 unknown(不 panic)。

Part B 在 deepeval 缺席时整体 skip, 所以 CI 主套件里 Part A 照跑。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server import deepeval_adapter as A

_HAS_DEEPEVAL = A.available()


def _json_for_schema(schema: object) -> str:
    """按 DeepEval 传进来的 pydantic schema 造一份**合法** JSON(含 $ref 解析)。"""

    def resolve(node: dict, defs: dict) -> object:
        if "$ref" in node:
            ref = node["$ref"].split("/")[-1]
            return resolve(defs.get(ref, {}), defs)
        if "anyOf" in node:
            # 取第一个非 null 分支, 没有就回 null
            for branch in node["anyOf"]:
                if branch.get("type") != "null":
                    return resolve(branch, defs)
            return None
        t = node.get("type")
        if t == "array":
            return [resolve(node.get("items", {}), defs)]
        if t == "object":
            return {k: resolve(v, defs) for k, v in (node.get("properties") or {}).items()}
        if t == "string":
            if "enum" in node:
                return node["enum"][0]
            return "yes"
        if t in ("number", "integer"):
            return 1
        if t == "boolean":
            return True
        return "yes"

    if schema is None:
        return "ok"
    js = A._schema_json(schema) or {}
    return json.dumps(resolve(js, js.get("$defs", {})))


def _make_judge(*, mode: str = "good", timeout_s: float | None = None):
    """假裁判: mode=good 按 schema 造合法 JSON; mode=broken 返回非 JSON;
    mode=slow 故意超时。"""
    JudgeCls = A._build_judge_class()

    async def _llm(messages, **kwargs):
        if mode == "slow":
            await asyncio.sleep(5)
        if mode == "broken":
            return {"choices": [{"message": {"content": "not json at all"}}]}
        return {"choices": [{"message": {"content": "x"}}]}

    class _Judge(JudgeCls):
        async def a_generate(self, prompt, schema=None):
            if mode == "broken":
                return "not json at all"
            if mode == "slow":
                return await super().a_generate(prompt, schema)
            return _json_for_schema(schema)

    return _Judge(llm_fn=_llm, timeout_s=timeout_s)


class _Case:
    input = "2+3等于几？只回答数字。"


# ---------------------------------------------------------------------------
# Part A — 共享 .venv (deepeval 未装): 优雅降级 + 集成
# ---------------------------------------------------------------------------


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("VEYA_DEEPEVAL", raising=False)
    assert A.enabled() is False
    monkeypatch.setenv("VEYA_DEEPEVAL", "1")
    assert A.enabled() is True


def test_score_no_op_when_deepeval_absent():
    """未装 deepeval 时 score_agent_result 不报错, 返回 enabled=False。"""
    if _HAS_DEEPEVAL:
        pytest.skip("deepeval installed — covered by Part B")
    block = A.score_agent_result(_Case(), {"reply": "5", "tool_calls": []})
    assert block == {"enabled": False, "reason": "deepeval not installed"}


def test_supervisor_finish_score_none_when_flag_off(monkeypatch):
    """flag 关(默认)→ None, supervisor 行为与今天一致。"""
    monkeypatch.delenv("VEYA_DEEPEVAL", raising=False)
    assert A.supervisor_finish_score("目标", "证据") is None


def test_agent_eval_deepeval_breakdown_ignores_unknown():
    """deepeval_breakdown 只统计 score 非 None 的用例, unknown 不进分母。"""
    from oskill.eval_suite import EvalRun

    from server.agent_eval import deepeval_breakdown

    run = EvalRun(
        suite_name="s",
        scores={"a": 1.0, "b": 1.0},
        details={
            "a": {"deepeval": {"enabled": True, "metrics": {"task_completion": {"score": 0.8}}}},
            "b": {"deepeval": {"enabled": True, "metrics": {"task_completion": {"score": None}}}},
        },
    )
    assert deepeval_breakdown(run) == {"task_completion": {"n": 1, "mean": 0.8}}


def test_agent_eval_breakdown_empty_when_no_scores():
    from oskill.eval_suite import EvalRun

    from server.agent_eval import deepeval_breakdown

    run = EvalRun(suite_name="s", scores={"a": 1.0}, details={"a": {}})
    assert deepeval_breakdown(run) == {}


def test_report_deepeval_none_when_flag_off(tmp_path, monkeypatch):
    """W12 报告在 flag 关时 deepeval 字段为 None——结构不变。"""
    monkeypatch.delenv("VEYA_DEEPEVAL", raising=False)
    from server.supervisor.reporting import _deepeval_finish_check

    assert _deepeval_finish_check(True, "目标", "证据") is None
    assert _deepeval_finish_check(False, "目标", "证据") is None  # 未完成也不查


@pytest.mark.asyncio
async def test_run_suite_use_deepeval_additive_and_score_neutral(tmp_path, monkeypatch):
    """端到端接线: use_deepeval=True 时 details 记 deepeval 段, 但规则分
    run.scores 与不开时**完全一致**(DeepEval 分只附加, 不改统计/基线)。"""
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("VEYA_DEEPEVAL", raising=False)

    from server.agent_eval import EvalCase, run_agent_eval_suite
    from server.coordinator_master import MasterCoordinator

    async def fake_llm(messages, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "你好!"}}], "usage": {}}

    cases = [EvalCase(id="hello", input="你好,你是谁?", expected={"max_tool_calls": 0})]
    coord = MasterCoordinator(llm_fn=fake_llm, max_rounds=2)

    plain = await run_agent_eval_suite(coord, cases=cases, suite_name="plain")
    with_dp = await run_agent_eval_suite(coord, cases=cases, suite_name="dp", use_deepeval=True)

    # 规则分不受 DeepEval 影响
    assert with_dp.scores == plain.scores == {"hello": 1.0}
    # deepeval 段被填充; 未装则优雅降级为 enabled=False
    assert "deepeval" in with_dp.details["hello"]
    if not _HAS_DEEPEVAL:
        assert with_dp.details["hello"]["deepeval"]["enabled"] is False
    # 不开时 details 里没有 deepeval 键(结构不变)
    assert "deepeval" not in plain.details["hello"]


# ---------------------------------------------------------------------------
# Part B — 需 deepeval 已装(隔离 venv .w18venv): 适配层真实机制
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_DEEPEVAL, reason="deepeval 未安装(需 .w18venv)")
def test_available_true():
    assert A.available() is True


@pytest.mark.skipif(not _HAS_DEEPEVAL, reason="deepeval 未安装(需 .w18venv)")
def test_good_judge_scores_task_completion():
    """合法假裁判 → 真实分数进 metrics, 不是 None。"""
    block = A.score_agent_result(
        _Case(),
        {"reply": "5", "tool_calls": []},
        metric_names=("task_completion",),
        judge=_make_judge(mode="good"),
    )
    assert block["enabled"] is True
    entry = block["metrics"]["task_completion"]
    assert entry["error"] is None
    assert entry["score"] is not None and 0.0 <= entry["score"] <= 1.0


@pytest.mark.skipif(not _HAS_DEEPEVAL, reason="deepeval 未安装(需 .w18venv)")
def test_broken_judge_degrades_to_unknown():
    """坏裁判(非 JSON)→ score None + error, 不 panic。"""
    block = A.score_agent_result(
        _Case(),
        {"reply": "5", "tool_calls": []},
        metric_names=("task_completion",),
        judge=_make_judge(mode="broken"),
    )
    entry = block["metrics"]["task_completion"]
    assert entry["score"] is None
    assert entry["error"]  # 有错误说明, 标 unknown


@pytest.mark.skipif(not _HAS_DEEPEVAL, reason="deepeval 未安装(需 .w18venv)")
def test_timeout_degrades_to_unknown():
    """裁判超时 → score None + error, 绝不阻塞。"""
    block = A.score_agent_result(
        _Case(),
        {"reply": "5", "tool_calls": []},
        metric_names=("task_completion",),
        judge=_make_judge(mode="slow", timeout_s=0.1),
    )
    entry = block["metrics"]["task_completion"]
    assert entry["score"] is None
    assert entry["error"]


@pytest.mark.skipif(not _HAS_DEEPEVAL, reason="deepeval 未安装(需 .w18venv)")
def test_tool_correctness_without_expected_tools_is_unknown():
    """无 expected_tools → 指标报缺参, 降级 unknown(不 panic 整个 suite)。"""
    block = A.score_agent_result(
        _Case(),
        {"reply": "5", "tool_calls": []},
        metric_names=("tool_correctness",),
        judge=_make_judge(mode="good"),
    )
    entry = block["metrics"]["tool_correctness"]
    assert entry["score"] is None
    assert entry["error"]


@pytest.mark.skipif(not _HAS_DEEPEVAL, reason="deepeval 未安装(需 .w18venv)")
def test_supervisor_finish_score_runs_when_flag_on(monkeypatch):
    monkeypatch.setenv("VEYA_DEEPEVAL", "1")
    block = A.supervisor_finish_score("目标：2+3", "证据：答案是 5", judge=_make_judge(mode="good"))
    assert block is not None
    assert block["enabled"] is True
