"""HPO 参数优化工具 (Agentic HPO) — 3O 原语 _hp_search 的 veya 装配层。

``optimize_parameters`` master 工具: 主脑给出参数空间 + objective 代码 →
HPStudy + AgentSampler (LLM sampler 注入) → 测量迭代 → best 配置。

装配决策 (主仓只装配, 机制在 oprim._hp_search):
- sampler 默认 veya1.2 别名路由 (GMI MiniMax M3 + OpenRouter 兜底 + frontier),
  可切 ``sampler="opencode-go"`` 走 key 直连端点;
- objective 在 3O 沙箱执行 (network_blocked, 纯计算); 代码约定: 使用
  ``params`` 字典, 最后一行 ``print(float)`` 输出目标值 (越大越好方向由
  direction 决定);
- study JSON 持久化到临时目录 (可审计、跨调用恢复)。

同步 API 设计: 工具在 to_thread worker 线程内跑 HPStudy.optimize,
sampler/objective 内部的 asyncio.run 各自起新事件循环 — 不在主循环嵌套。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

from server.tool_registry import ToolExecutionError, master_tools
from veya.llm import llm_call as _llm_call

_OPENCODE_ENDPOINT = "https://opencode.ai/zen/go/v1"


def _llm_sampler(backend: str) -> object:
    """prompt -> reply 同步包装 (worker 线程内 asyncio.run 新 loop)。"""

    def sampler(prompt: str) -> str:
        if backend == "opencode-go":
            result = asyncio.run(
                _llm_call(
                    [{"role": "user", "content": prompt}],
                    provider="opencode-go",
                    model="deepseek-v4-flash",
                    endpoint=_OPENCODE_ENDPOINT,
                )
            )
        else:  # veya1.2 别名路由 (默认)
            result = asyncio.run(_llm_call([{"role": "user", "content": prompt}], model="veya1.2"))
        content = result["choices"][0]["message"]["content"]
        return str(content)

    return sampler


def _build_objective(objective_python: str) -> object:
    """objective 代码 → 同步 callable (params -> float)。

    注入 ``params`` 字典; 解析 stdout 最后一行 float 为目标值。
    3O 沙箱执行: 网络封锁 / 内存时间限制。
    """
    from veya.sandbox import SandboxConfig, create_safe_executor

    prefix = "import json\nparams = json.loads(r'''__VEYA_HP_PARAMS__''')\n"

    def objective(params: dict) -> float:
        code = prefix.replace("__VEYA_HP_PARAMS__", json.dumps(params)) + objective_python
        config = SandboxConfig(
            time_limit=60.0,
            memory_limit=1024 * 1024 * 1024,
            network_blocked=True,
            audit_enabled=True,
            env_extra={
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
            },
        )
        executor = create_safe_executor(config)

        async def _run():
            async with executor:
                result = await executor.run_script(code)
            return result

        result = asyncio.run(_run())
        if result.get("exit_code") != 0:
            raise RuntimeError(
                f"objective exit={result.get('exit_code')}: {result.get('stderr', '')[:400]}"
            )
        stdout = str(result.get("stdout", ""))
        lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
        if not lines:
            raise RuntimeError("objective printed nothing (expect final print(float))")
        try:
            return float(lines[-1])
        except ValueError:
            raise RuntimeError(f"objective last line not a float: {lines[-1]!r}") from None

    return objective


def _load_oprim():
    """惰性加载主库 oprim (veya.platform 注入 sys.path)。"""
    from veya.platform import load as _load

    return _load("oprim")


def _parse_space(space_json: str) -> object:
    return _load_oprim().space_from_json(space_json)


def _parse_arguments(
    space_json: str,
    objective_python: str,
    direction: str,
    n_trials: int,
    context: str,
    sampler: str,
    storage: str | None,
) -> dict:
    oprim = _load_oprim()
    space = _parse_space(space_json)
    if direction not in ("maximize", "minimize"):
        raise ToolExecutionError(f"direction must be maximize|minimize, got {direction!r}")
    if not objective_python or "print(" not in objective_python:
        raise ToolExecutionError(
            "objective_python must print the scalar objective as its last line, "
            "e.g. `print(params['threshold'])`"
        )
    study_kwargs: dict = {
        "direction": direction,
        "storage": storage,
    }
    return {
        "space": space,
        "objective": _build_objective(objective_python),
        "n_trials": int(n_trials),
        "sampler": oprim.AgentSampler(_llm_sampler(sampler), context=context),
        "study_kwargs": study_kwargs,
    }


async def _tool_optimize_parameters(
    space_json: str,
    objective_python: str,
    direction: str = "maximize",
    n_trials: int = 5,
    context: str = "",
    sampler: str = "veya1.2",
    storage: str | None = None,
) -> str:
    """运行一轮 Agentic HPO: 语义化提议 + 沙箱测量 + 迭代 → best 配置。"""
    if storage is None:
        hp_dir = Path(tempfile.gettempdir()) / "veya_hp"
        hp_dir.mkdir(parents=True, exist_ok=True)
        storage = str(hp_dir / f"study_{int(time.time() * 1000)}.json")

    oprim = _load_oprim()
    args = _parse_arguments(
        space_json, objective_python, direction, n_trials, context, sampler, storage
    )
    study = oprim.HPStudy(seed=0, **args["study_kwargs"])
    study.space = args["space"]

    # 同步 optimize 在 worker 线程跑; sampler/objective 内部各自 asyncio.run
    await asyncio.to_thread(study.optimize, args["objective"], args["n_trials"], args["sampler"])

    best = study.best_trial
    summary = {
        "ok": True,
        "direction": study.direction,
        "n_trials": len(study.trials),
        "completed": len(study.completed),
        "failed": sum(1 for t in study.trials if t.state == "failed"),
        "best": (
            {"trial": best.number, "params": best.params, "value": best.value}
            if best is not None
            else None
        ),
        "trials": [
            {"number": t.number, "params": t.params, "value": t.value, "state": t.state}
            for t in study.trials
        ],
        "note": study.note or args["sampler"].note,
        "storage": str(study.save(storage)),
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


def wire_master_tools() -> int:
    """注册 optimize_parameters 进主脑工具面 (幂等)。"""
    master_tools.register(
        name="optimize_parameters",
        description=(
            "Run an agent-guided hyperparameter/parameter optimization loop: give a "
            "parameter space and an objective snippet, and it iteratively proposes "
            "configurations (semantic, history-driven), evaluates each in the 3O "
            "sandbox, and returns the best configuration plus the full trial trail. "
            "Use when the user wants to tune system parameters against a measurable "
            "scalar objective (model training, inference routing, strategy thresholds, "
            "simulation inputs)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "space_json": {
                    "type": "string",
                    "description": (
                        'Parameter space as JSON object: {"<name>": {"type": '
                        '"float|int|categorical", ...}}. float: low/high/log/context; '
                        "int: low/high/log/context; categorical: choices/context. "
                        'Example: {"threshold": {"type": "float", "low": 0.05, '
                        '"high": 0.95, "context": "decision threshold"}}'
                    ),
                },
                "objective_python": {
                    "type": "string",
                    "description": (
                        "Python snippet computing the scalar objective. It receives a "
                        "`params` dict; print the scalar as the LAST line, e.g. "
                        "`print(accuracy(params))`."
                    ),
                },
                "direction": {
                    "type": "string",
                    "description": "maximize (default) or minimize the objective",
                },
                "n_trials": {
                    "type": "integer",
                    "description": "number of trials to run (default 5)",
                },
                "context": {
                    "type": "string",
                    "description": "semantic description of what is being tuned (helps the sampler)",
                },
                "sampler": {
                    "type": "string",
                    "description": "veya1.2 (default, GMI MiniMax M3 + OpenRouter fallback) | opencode-go (key-direct)",
                },
                "storage": {
                    "type": "string",
                    "description": "optional JSON study path to persist/resume",
                },
            },
            "required": ["space_json", "objective_python"],
        },
        func=_tool_optimize_parameters,
        max_result_chars=12000,
    )
    return 1
