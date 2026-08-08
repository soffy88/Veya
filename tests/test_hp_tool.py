"""optimize_parameters 工具 (Agentic HPO 装配层) 测试。

真实沙箱 objective + stub sampler (隔离 LLM 网络); 覆盖 wire/schema、
端到端循环、校验兜底、持久化。
"""
import json

import pytest

from server.tool_registry import ToolExecutionError, master_tools


def _space_json():
    return json.dumps({
        "threshold": {"type": "float", "low": 0.05, "high": 0.95,
                      "context": "decision threshold"},
        "budget": {"type": "int", "low": 10, "high": 200, "log": True,
                   "context": "compute budget"},
    })


def _objective():
    return (
        "import math\n"
        "score = -abs(params['threshold'] - 0.5) - abs(params['budget'] - 100) / 100\n"
        "print(score)\n"
    )


@pytest.fixture
def stub_sampler(monkeypatch):
    """把 LLM sampler 换成可编程 stub (proposal 序列)。"""
    from server import hp_optimizer

    state = {"calls": 0}

    def make(backend):
        def sampler(prompt):
            state["calls"] += 1
            if state["calls"] == 1:
                return '{"threshold": 0.6, "budget": 100}'
            return '{"threshold": 0.4, "budget": 120}'

        return sampler

    monkeypatch.setattr(hp_optimizer, "_llm_sampler", make)
    return state


# ── wire / schema ───────────────────────────────────────────────────────


def test_wire_registers_optimize_parameters():
    from server import hp_optimizer

    if not any(s["function"]["name"] == "optimize_parameters"
               for s in master_tools._schemas):
        hp_optimizer.wire_master_tools()
    schema = next(s["function"] for s in master_tools._schemas
                  if s["function"]["name"] == "optimize_parameters")
    props = schema["parameters"]["properties"]
    assert {"space_json", "objective_python"} <= set(props)
    assert schema["parameters"]["required"] == ["space_json", "objective_python"]


# ── 端到端 ──────────────────────────────────────────────────────────────


async def test_optimize_end_to_end_stub_sampler(stub_sampler):
    from server import hp_optimizer

    result = json.loads(await hp_optimizer._tool_optimize_parameters(
        space_json=_space_json(),
        objective_python=_objective(),
        direction="maximize",
        n_trials=2,
        context="tune a classifier",
    ))
    assert result["ok"] is True
    assert result["n_trials"] == 2
    assert result["completed"] == 2
    assert result["failed"] == 0
    # stub 提议: (0.6,100) score=-0.1; (0.4,120) score=-0.3 → best = 0.6
    assert result["best"]["params"]["threshold"] == 0.6
    assert result["best"]["value"] == pytest.approx(-0.1, abs=1e-6)
    assert result["storage"].endswith(".json")
    assert stub_sampler["calls"] == 2


async def test_optimize_minimize_direction(stub_sampler):
    from server import hp_optimizer

    result = json.loads(await hp_optimizer._tool_optimize_parameters(
        space_json=_space_json(),
        objective_python=_objective(),
        direction="minimize",
        n_trials=2,
    ))
    assert result["direction"] == "minimize"
    # minimize: best = 较小 score = (0.4,120) → -0.3
    assert result["best"]["value"] == pytest.approx(-0.3, abs=1e-6)


async def test_optimize_persists_study(tmp_path, stub_sampler):
    from server import hp_optimizer

    storage = str(tmp_path / "hp" / "study.json")
    result = json.loads(await hp_optimizer._tool_optimize_parameters(
        space_json=_space_json(),
        objective_python=_objective(),
        direction="maximize",
        n_trials=2,
        storage=storage,
    ))
    from veya.platform import oprim as load_oprim

    loaded = load_oprim().HPStudy.load(result["storage"])
    assert len(loaded.trials) == 2
    assert loaded.best_trial.params["threshold"] == 0.6


# ── 校验兜底 ────────────────────────────────────────────────────────────


async def test_invalid_space_raises():
    from server import hp_optimizer

    with pytest.raises(Exception):
        await hp_optimizer._tool_optimize_parameters(
            space_json='{"x": {"type": "unknown"}}',
            objective_python="print(1.0)",
            n_trials=1,
        )


async def test_objective_without_print_raises():
    from server import hp_optimizer

    with pytest.raises(ToolExecutionError):
        await hp_optimizer._tool_optimize_parameters(
            space_json=_space_json(),
            objective_python="x = 1 + 1",
            n_trials=1,
        )


async def test_objective_crash_records_failed_trial(stub_sampler):
    from server import hp_optimizer

    bad = "raise RuntimeError('boom')\nprint(0.0)\n"
    result = json.loads(await hp_optimizer._tool_optimize_parameters(
        space_json=_space_json(),
        objective_python=bad,
        direction="maximize",
        n_trials=2,
    ))
    assert result["failed"] == 2
    assert all(t["state"] == "failed" for t in result["trials"])
    assert result["best"] is None


async def test_sampler_reply_invalid_falls_back_random(stub_sampler, monkeypatch):
    from server import hp_optimizer

    def make_bad(backend):
        return lambda prompt: "not json at all"

    monkeypatch.setattr(hp_optimizer, "_llm_sampler", make_bad)
    result = json.loads(await hp_optimizer._tool_optimize_parameters(
        space_json=_space_json(),
        objective_python=_objective(),
        direction="maximize",
        n_trials=3,
    ))
    # 兜底随机采样 → 仍然完成 (值在空间内)
    assert result["completed"] == 3
    for t in result["trials"]:
        assert 0.05 <= t["params"]["threshold"] <= 0.95
        assert 10 <= t["params"]["budget"] <= 200
