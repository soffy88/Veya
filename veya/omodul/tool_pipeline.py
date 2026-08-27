"""veya/omodul/tool_pipeline — omodul_tool_pipeline（工具五步管道，最重要防线）。

完整复现「解析 → 校验 → 权限 → 执行 → 包装」：
    1. parse     — oskill_parse_tool_call（坏 JSON/非对象 → 显式 error）
    2. validate  — oskill_validate_args（JSON Schema 绝对校验，幻觉拦截核心）
    3. authorize — 注入 permit 回调（默认放行；生产注入 tool_guard/authz）
    4. exec      — 查注册表调用工具函数（async/sync 皆可）
    5. wrap      — 统一 ToolRunResult + 全步骤 audit + emit_event 事件

注入:
    sandbox / barrier — obase 句柄（默认 container 全局句柄）
    permit           — Callable[[tool_name, args], bool] | None（None = 放行）

事件流（emit_event → EventBarrier）:
    tool.pipeline.parse / tool.pipeline.validate / tool.pipeline.authorize /
    tool.pipeline.exec / tool.pipeline.result
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from veya.oprim.event import emit_event
from veya.oskill.pure.parse_tool_call import ToolCall, parse_tool_calls
from veya.oskill.pure.validate_args import validate_args

ToolFn = Callable[..., Any] | Callable[..., Awaitable[Any]]
PermitFn = Callable[[str, dict[str, Any]], bool]


@dataclass
class ToolSpec:
    """工具注册规格。schema = JSON Schema（绝对校验）；None = 跳过参数校验。"""

    name: str
    fn: ToolFn
    schema: dict | None = None
    description: str = ""


@dataclass
class ToolRunResult:
    """单次工具调用结果：五步管道全审计。"""

    tool_name: str
    ok: bool = False
    output: Any = None
    error: str = ""
    rejected: bool = False
    reject_stage: str = ""  # parse | validate | authorize
    audit: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "rejected": self.rejected,
            "reject_stage": self.reject_stage,
            "audit": self.audit,
            "duration_ms": self.duration_ms,
        }


class ToolPipeline:
    """工具五步管道：解析 → 校验 → 权限 → 执行 → 包装。"""

    def __init__(
        self,
        *,
        barrier: Any = None,
        permit: PermitFn | None = None,
    ) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._permit = permit
        self._barrier = barrier

    # ------------------------------------------------------------------ 注册

    def register(
        self,
        name: str,
        fn: ToolFn,
        *,
        schema: dict | None = None,
        description: str = "",
    ) -> None:
        """注册工具。schema 为 JSON Schema（validate_args 子集）。"""
        if not name or not callable(fn):
            raise ValueError("工具名与函数必填")
        self._tools[name] = ToolSpec(name=name, fn=fn, schema=schema, description=description)

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self) -> list[str]:
        return sorted(self._tools)

    def spec(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        """OpenAI 格式工具声明（发给 LLM 的 tools 参数）。"""
        out: list[dict] = []
        for spec in sorted(self._tools.values(), key=lambda s: s.name):
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.schema or {"type": "object", "properties": {}},
                    },
                }
            )
        return out

    # ------------------------------------------------------------------ 管道

    async def run_message(self, message: dict, *, session_id: str = "") -> list[ToolRunResult]:
        """解析消息内全部 tool_calls 并逐个执行。"""
        calls = parse_tool_calls(message)
        return [await self.run_call(call, session_id=session_id) for call in calls]

    async def run_call(self, call: ToolCall, *, session_id: str = "") -> ToolRunResult:
        """执行单个 ToolCall（五步管道）。"""
        start = time.time()
        audit: list[dict[str, Any]] = []
        ctx = {"session_id": session_id, "tool_name": call.name}

        def _stage(stage: str, ok: bool, detail: str = "") -> None:
            entry = {"stage": stage, "ok": ok, **ctx}
            if detail:
                entry["detail"] = detail
            audit.append(entry)
            emit_event(
                f"tool.pipeline.{stage}",
                {"ok": ok, "tool_name": call.name, "detail": detail},
                barrier=self._barrier,
            )

        # 1. 解析（外部已解析则 error 直达）
        if call.error:
            _stage("parse", False, call.error)
            return self._result(
                call,
                ok=False,
                error=call.error,
                rejected=True,
                reject_stage="parse",
                audit=audit,
                start=start,
            )
        _stage("parse", True, "tool_call 解析成功")

        # 2. 校验（幻觉拦截：参数不合格绝不执行）
        spec = self._tools.get(call.name)
        if spec is None:
            _stage("validate", False, f"工具 {call.name!r} 未注册")
            return self._result(
                call,
                ok=False,
                error=f"工具 {call.name!r} 未注册",
                rejected=True,
                reject_stage="validate",
                audit=audit,
                start=start,
            )
        if spec.schema is not None:
            vr = validate_args(call.arguments, spec.schema)
            if not vr.ok:
                detail = "; ".join(vr.errors[:3])
                _stage("validate", False, detail)
                return self._result(
                    call,
                    ok=False,
                    error=detail,
                    rejected=True,
                    reject_stage="validate",
                    audit=audit,
                    start=start,
                )
        _stage("validate", True, "参数校验通过")

        # 3. 权限
        if self._permit is not None and not self._permit(call.name, call.arguments):
            _stage("authorize", False, "权限拒绝")
            return self._result(
                call,
                ok=False,
                error="权限拒绝",
                rejected=True,
                reject_stage="authorize",
                audit=audit,
                start=start,
            )
        _stage("authorize", True, "权限通过")

        # 4. 执行
        try:
            raw = spec.fn(**call.arguments)
            if inspect.isawaitable(raw):
                output = await raw
            else:
                output = raw
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            _stage("exec", False, detail)
            return self._result(
                call, ok=False, error=detail, rejected=False, audit=audit, start=start
            )
        _stage("exec", True, "执行成功")

        # 5. 包装
        wrapped = _wrap_output(output)
        _stage("wrap", True, "结果包装完成")
        emit_event(
            "tool.pipeline.result",
            {"ok": True, "tool_name": call.name, "output_preview": str(wrapped)[:200]},
            barrier=self._barrier,
        )
        return self._result(call, ok=True, output=wrapped, audit=audit, start=start)

    def _result(
        self,
        call: ToolCall,
        *,
        ok: bool,
        audit: list[dict],
        start: float,
        output: Any = None,
        error: str = "",
        rejected: bool = False,
        reject_stage: str = "",
    ) -> ToolRunResult:
        return ToolRunResult(
            tool_name=call.name,
            ok=ok,
            output=output,
            error=error,
            rejected=rejected,
            reject_stage=reject_stage,
            audit=audit,
            duration_ms=(time.time() - start) * 1000.0,
        )


def _wrap_output(output: Any) -> Any:
    """结果包装：可 JSON 序列化原样返回，否则安全 repr。"""
    if output is None:
        return ""
    if isinstance(output, (str, int, float, bool, list, dict)):
        return output
    return repr(output)


__all__ = ["ToolPipeline", "ToolRunResult", "ToolSpec"]
