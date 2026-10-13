"""server.deepeval_adapter — 把 DeepEval 接成 Veya 现有评测的**可选打分后端**。

不建第二套评测框架。Veya 已有 `oskill.eval_suite`(纯统计原语) +
`server/agent_eval.py`(真实 `chat_stream` → 规则打分 → 统计) +
`scripts/run_agent_eval.py`(CLI)。本模块只补两样东西:

1. `VeyaJudgeModel` — 把 Veya 现有 LLM router(`veya.llm.llm_call`, 免费池)
   包成 DeepEval 的 `DeepEvalBaseLLM`, 让 DeepEval 的指标用**免费**裁判模型,
   不加付费 key。
2. `score_agent_result()` — 对**已有的** agent 输出(不重跑 agent)跑选定指标,
   把分数记进 dict。规则分(`_score_result`)仍是 `run.scores` 的权威; DeepEval
   分只**附加**在 `run.details[case.id]["deepeval"]`, 不改动统计/基线对比。

可选依赖: `deepeval` 未安装时 `available()` 为 False, 所有调用 no-op 优雅降级,
现有测试与主链零感知。裁判不可用/超时 → 该指标 `score=None` 标 `unknown`,
不记 0 分(0 分会污染均值), 绝不阻塞主流程。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

__all__ = [
    "DEFAULT_METRICS",
    "available",
    "enabled",
    "score_agent_result",
    "supervisor_finish_score",
]

#: flag: 开了才在 W12 supervisor 完成验证时跑 DeepEval(默认关)。
_DEEPEVAL_ENV = "VEYA_DEEPEVAL"
#: 单次裁判调用超时(秒), 可覆盖。
_TIMEOUT_ENV = "VEYA_DEEPEVAL_TIMEOUT"
_DEFAULT_TIMEOUT_S = 30.0

#: 默认跑的指标名(与 §2 选型一致)。全部对**已有输出**打分, 不重跑 agent。
DEFAULT_METRICS: tuple[str, ...] = (
    "answer_relevancy",
    "task_completion",
    "tool_correctness",
    "faithfulness",
)


def enabled() -> bool:
    """W12 supervisor 侧是否启用 DeepEval(flag VEYA_DEEPEVAL, 默认关)。"""
    return os.environ.get(_DEEPEVAL_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def available() -> bool:
    """DeepEval 可选依赖是否已安装。未装 → 一切 no-op 优雅降级。"""
    try:
        import deepeval  # noqa: F401
    except Exception:
        return False
    return True


def _timeout_s() -> float:
    raw = os.environ.get(_TIMEOUT_ENV)
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def _run_in_thread(coro: Any) -> Any:
    """在一个新线程里跑协程(新 loop)。用于同步上下文, 即便调用方已有运行中的
    loop 也安全——asyncio.run 不能嵌套进运行中的 loop。"""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _judge_model_base() -> type:
    """返回 DeepEvalBaseLLM 基类(延迟导入, 未装时抛 ImportError 由调用方兜住)。"""
    from deepeval.models import DeepEvalBaseLLM

    return DeepEvalBaseLLM


def _build_judge_class() -> type:
    base = _judge_model_base()

    class VeyaJudgeModel(base):  # type: ignore[misc, valid-type]
        """DeepEval 裁判模型, 底层走 Veya 免费 LLM router。

        `llm_fn` 可注入(测试用假裁判); 缺省用 `veya.llm.llm_call`。
        每次调用包超时; 异常向上抛, 由指标层捕获标 unknown(不 panic suite)。
        """

        def __init__(
            self,
            *,
            llm_fn: Any = None,
            provider: str = "veya1.2",
            model: str | None = None,
            timeout_s: float | None = None,
        ) -> None:
            self._llm_fn = llm_fn
            self._provider = provider
            self._model = model
            self._timeout_s = timeout_s if timeout_s is not None else _timeout_s()
            super().__init__()

        # -- DeepEval contract -------------------------------------------------
        def load_model(self) -> Any:
            return self

        def get_model_name(self) -> str:
            return f"veya-judge:{self._provider}"

        def generate(self, prompt: str, schema: Any = None) -> str:
            # 同步入口少用(主用 a_generate); 一律在新线程里跑, 避免与调用方
            # 已在运行的 event loop 冲突(asyncio.run 不能在运行中的 loop 里调)。
            return _run_in_thread(self.a_generate(prompt, schema))

        async def a_generate(self, prompt: str, schema: Any = None) -> str:
            messages = self._messages(prompt, schema)
            resp = await asyncio.wait_for(self._call(messages), timeout=self._timeout_s)
            return self._extract(resp)

        # -- internals ---------------------------------------------------------
        async def _call(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
            if self._llm_fn is not None:
                return await self._llm_fn(messages, provider=self._provider)
            from veya.llm import llm_call

            kwargs: dict[str, Any] = {"provider": self._provider}
            if self._model:
                kwargs["model"] = self._model
            return await llm_call(messages, **kwargs)

        def _messages(self, prompt: str, schema: Any) -> list[dict[str, Any]]:
            system = "You are a strict evaluation judge. Answer with JSON only."
            if schema is not None:
                system += " The JSON must match this schema: " + json.dumps(
                    _schema_json(schema), ensure_ascii=False
                )
            return [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]

        @staticmethod
        def _extract(resp: dict[str, Any]) -> str:
            choices = resp.get("choices") or [{}]
            msg = choices[0].get("message") or {}
            return str(msg.get("content") or "")

    return VeyaJudgeModel


def _schema_json(schema: Any) -> Any:
    """把 DeepEval 传进来的 pydantic schema 转成可读 JSON schema 描述。"""
    for attr in ("model_json_schema", "schema"):
        fn = getattr(schema, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                continue
    return {"type": "object"}


def _build_metrics(names: tuple[str, ...], judge: Any) -> list[Any]:
    """按名字构造 DeepEval 指标实例(延迟导入)。

    每个指标都显式传 `model=judge`——否则 DeepEval 会去实例化默认的
    `OpenAIModel` 并在无 `OPENAI_API_KEY` 时抛错(ToolCorrectness 尤其)。
    """
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        FaithfulnessMetric,
        GEval,
        TaskCompletionMetric,
        ToolCorrectnessMetric,
    )

    try:  # DeepEval ≥4 的规范名; 旧版回退到 LLMTestCaseParams
        from deepeval.test_case import SingleTurnParams as _Params
    except ImportError:  # pragma: no cover - 版本兼容
        from deepeval.test_case import LLMTestCaseParams as _Params

    table: dict[str, Any] = {
        "answer_relevancy": lambda: AnswerRelevancyMetric(model=judge, threshold=0.5),
        "task_completion": lambda: TaskCompletionMetric(model=judge, threshold=0.5),
        "tool_correctness": lambda: ToolCorrectnessMetric(model=judge, threshold=0.5),
        "faithfulness": lambda: FaithfulnessMetric(model=judge, threshold=0.5),
        # G-Eval 自定义判据: 兜底"回答是否解决了用户问题"这一通用忠实度。
        "geval": lambda: GEval(
            name="G-Eval",
            criteria="判定 actual_output 是否直接、准确地回应了 input 的诉求。",
            evaluation_params=[_Params.INPUT, _Params.ACTUAL_OUTPUT],
            model=judge,
            threshold=0.5,
        ),
    }
    out = []
    for name in names:
        factory = table.get(name)
        if factory is not None:
            out.append((name, factory()))
    return out


def score_agent_result(
    case: Any,
    agent_result: dict[str, Any],
    *,
    metric_names: tuple[str, ...] | None = None,
    judge: Any = None,
    retrieval_context: list[str] | None = None,
) -> dict[str, Any]:
    """对**已有的** agent 输出跑 DeepEval 指标, 返回 `{"enabled", "metrics"}`。

    不重跑 agent: `actual_output` 取 `agent_result["reply"]`, `tools_called` 取
    `agent_result["tool_calls"]`。裁判不可用/超时 → 该指标 `score=None` +
    `error`, 标 unknown, 绝不抛到调用方。

    DeepEval 未安装时返回 `{"enabled": False, "reason": "deepeval not installed"}`。
    """
    if not available():
        return {"enabled": False, "reason": "deepeval not installed"}

    names = metric_names if metric_names is not None else DEFAULT_METRICS
    actual_output = str(agent_result.get("reply") or "")
    input_text = str(getattr(case, "input", "") or "")
    tools_called = _tools_called(agent_result)
    expected_tools = _expected_tools(case)

    try:
        if judge is None:
            judge = _build_judge_class()()
        metrics = _build_metrics(tuple(names), judge)
    except Exception as exc:  # 构造阶段就失败 → 整体 unknown, 不 panic
        return {
            "enabled": True,
            "metrics": {
                name: {"score": None, "error": f"{type(exc).__name__}: {exc}"} for name in names
            },
        }

    from deepeval.test_case import LLMTestCase

    test_case = LLMTestCase(
        input=input_text,
        actual_output=actual_output,
        tools_called=tools_called,
        expected_tools=expected_tools,
        retrieval_context=retrieval_context,
    )

    # 走 a_measure(在新线程的新 loop 里), 不走 metric.measure——后者内部用
    # get_or_create_event_loop().run_until_complete, 在已有运行中 loop 的调用方
    # (agent_eval 的 async suite)里会炸。
    out = _run_in_thread(_run_all_metrics(metrics, test_case))
    return {"enabled": True, "metrics": out}


async def _run_all_metrics(metrics: list[Any], test_case: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, metric in metrics:
        out[name] = await _run_metric(metric, test_case)
    return out


async def _run_metric(metric: Any, test_case: Any) -> dict[str, Any]:
    """跑单个指标; 任何异常 → `{"score": None, "error": ...}` 标 unknown。"""
    try:
        try:
            await metric.a_measure(test_case, _show_indicator=False)
        except TypeError:
            await metric.a_measure(test_case)
        score = getattr(metric, "score", None)
        reason = getattr(metric, "reason", None)
        error = getattr(metric, "error", None)
        if error is not None:
            return {"score": None, "reason": reason, "success": False, "error": str(error)}
        success = None
        try:
            success = metric.is_successful()
        except Exception:
            success = None
        return {
            "score": float(score) if score is not None else None,
            "reason": reason,
            "success": bool(success) if success is not None else None,
            "error": None,
        }
    except Exception as exc:
        return {
            "score": None,
            "reason": None,
            "success": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _tools_called(agent_result: dict[str, Any]) -> list[Any]:
    from deepeval.test_case import ToolCall

    calls = agent_result.get("tool_calls") or []
    out = []
    for tc in calls:
        name = str(tc.get("tool") or tc.get("name") or "")
        if name:
            out.append(ToolCall(name=name))
    return out


def _expected_tools(case: Any) -> list[Any] | None:
    from deepeval.test_case import ToolCall

    expected = getattr(case, "expected", None) or {}
    want = expected.get("tool")
    if not want:
        return None
    return [ToolCall(name=str(want))]


def supervisor_finish_score(
    objective: str,
    last_evidence: str | None,
    *,
    judge: Any = None,
) -> dict[str, Any] | None:
    """W12 supervisor 完成验证时**可选**跑一道任务完成度检查。

    flag `VEYA_DEEPEVAL` 关(默认)→ 返回 None, 调用方行为与今天完全一致。
    开 → 对「目标 vs 最新证据」跑 `task_completion`, 分数作为**附加信息**返回;
    不改变 is_done 判定, 裁判不可用/超时/未装一律降级(返回 enabled=False)。
    """
    if not enabled():
        return None
    if not available():
        return {"enabled": False, "reason": "deepeval not installed"}
    if not last_evidence:
        return {"enabled": False, "reason": "no evidence to score"}

    class _Case:
        input = objective

    try:
        return score_agent_result(
            _Case(),
            {"reply": last_evidence, "tool_calls": []},
            metric_names=("task_completion",),
            judge=judge,
        )
    except Exception as exc:
        return {"enabled": False, "reason": f"{type(exc).__name__}: {exc}"}
