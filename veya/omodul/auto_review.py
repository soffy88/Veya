"""veya/omodul/auto_review — omodul_auto_review（命令自动审查, NANO-W10 主体 3）。

一条 shell 命令 / 一个有副作用的工具调用在真正执行之前, 由**另一个**模型
判定 ``allow`` / ``ask`` / ``block``:

    oprim(既有)  ·  oskill.pure.command_review (prompt 组装 + 解析)
                 →  omodul_auto_review (注入 LLM, 记 decision_trail / cost)

**归属 (3O PARADIGM §6)**
* 内部组合 ≥2 个纯变换 — `build_review_prompt` + `parse_review_verdict` +
  `is_human_only`, 全部在 oskill 纯层;
* 标准签名 ``(config, input_data, output_dir) -> dict``, 必返 status/error;
* 失败不 raise (除 ``CancelledError``), ``_enabled_pillars`` 显式声明;
* **不裸调 sibling omodul**; 不 import ``server.*`` —— 谁提供 LLM 由服务层注入。

**不新增审批系统 (§OBSOLETE)**: ``ask`` 只是把一个**已有的** ActionGateway 判定
从 ALLOW 降级为 APPROVAL_REQUIRED, 后续仍走既有 SideEffectLedger + 用户审批。
本模块不存审批、不解析审批、不记账。

**人审类别不花模型调用**: DB 迁移 / 支付 / 密钥 / 生产 走 W6 的 human-only
分类器 (`oskill.pure.permission.HUMAN_ONLY_MARKERS`), 直接返回 ``ask``。

支柱:
    decision_trail —— 谁在什么时候让哪个动作过了哪一关 (安全相关, 必开)
    cost           —— 审查本身是一次 LLM 调用, 成本必须计入
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from veya.oskill.pure.command_review import (
    VERDICT_ALLOW,
    VERDICT_BLOCK,
    build_review_prompt,
    classify_provenance,
    is_human_only,
    parse_review_verdict,
)

__all__ = [
    "AutoReviewConfig",
    "AutoReviewInput",
    "auto_review",
    "compute_fingerprint_for",
]

_MAX_TRAIL_STEPS = 64


# --------------------------------------------------------------------------
# config / input (§5.2 / §5.3)
# --------------------------------------------------------------------------


@dataclass
class AutoReviewConfig:
    """审查器的注入点。

    Attributes:
        llm_caller: 注入的审查模型 ``(messages=..., ...) -> dict|str``。可以是
            同步或 async。**没有它就没有审查** —— 未注入时本模块返回
            ``ask`` 而不是 ``allow``: 无人评估不等于许可。
        max_tokens: 审查回复的 token 上限 (只需要一个词 + 一句话)。
        record_trail: 是否把 decision trail 写到 ``output_dir``。
    """

    llm_caller: Callable[..., Any] | None = None
    max_tokens: int = 256
    record_trail: bool = True

    _omodul_name: ClassVar[str] = "auto_review"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"command", "tool"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail", "cost"}


@dataclass
class AutoReviewInput:
    """待审查的动作。

    Attributes:
        command: 即将执行的 shell 命令 (或工具动作描述)。
        instruction: 产生它的用户指令原文。
        tool: 工具名 (非 shell 时填)。
        effect: 已声明的副作用类别。
        untrusted_sources: 影响了本动作的不可信来源 (email/web/channel/tool_result)。
        arguments: 工具参数 (服务层已做密钥掩码)。
        goal_run_id: 审计用; 不参与判定。
    """

    command: str = ""
    instruction: str = ""
    tool: str = ""
    effect: str = ""
    untrusted_sources: list[str] = field(default_factory=list)
    arguments: dict[str, Any] = field(default_factory=dict)
    goal_run_id: str = ""
    bot_id: str = ""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _fingerprint(payload: dict[str, Any]) -> str:
    import hashlib

    body = "|".join(f"{key}={payload.get(key, '')}" for key in sorted(payload))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def compute_fingerprint_for(config: AutoReviewConfig, input_data: Any) -> str:
    """同一 (command, tool) 的重复审查可去重 (§5.5)。"""

    fields = set(getattr(config, "_fingerprint_fields", set()) or set())
    if not fields or input_data is None:
        return ""
    data = dict(getattr(input_data, "__dict__", {}))
    if not data:
        return ""
    return _fingerprint({key: data.get(key, "") for key in sorted(fields)})


def _content_of(response: Any) -> str:
    """Pull the text out of whatever shape the injected caller returned."""

    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                return str(message.get("content") or "")
            if isinstance(choices[0], dict):
                return str(choices[0].get("text") or "")
        if "content" in response:
            return str(response.get("content") or "")
        if "output" in response:
            return str(response.get("output") or "")
    return ""


def _cost_of(response: Any) -> float:
    """Read the cost the injected caller already computed; never invent one."""

    if not isinstance(response, dict):
        return 0.0
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return 0.0
    value = usage.get("cost_usd")
    return float(value) if isinstance(value, int | float) else 0.0


def _write_trail(
    output_dir: Path | None, trail: list[dict[str, Any]], *, enabled: bool = True
) -> str | None:
    if output_dir is None or not enabled or not trail:
        return None
    try:
        path = Path(output_dir) / "auto_review_trail.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(trail, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return str(path)
    except Exception:
        # An audit artefact that cannot be written must not decide the verdict;
        # the verdict is already computed and returned to the caller.
        return None


def _envelope(
    verdict: str,
    reason: str,
    *,
    reviewed: bool,
    provenance: str,
    fingerprint: str,
    trail: list[dict[str, Any]],
    cost_usd: float = 0.0,
    trail_path: str | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "completed",
        "error": error,
        "verdict": verdict,
        "reason": reason,
        "reviewed": reviewed,
        "provenance": provenance,
        "fingerprint": fingerprint,
        "decision_trail": trail,
        "cost_usd": cost_usd,
        "trail_path": trail_path,
    }


# --------------------------------------------------------------------------
# omodul — auto_review
# --------------------------------------------------------------------------


async def auto_review(
    config: AutoReviewConfig,
    input_data: AutoReviewInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """审查一个即将执行的动作。

    Args:
        config: 注入的审查模型 + 支柱开关。
        input_data: 待审查的动作。
        output_dir: 可选; 写 decision trail。
        on_step: 同步进度回调。

    Returns:
        ``{"status", "error", "verdict", "reason", "reviewed", "provenance",
        "fingerprint", "decision_trail", "cost_usd", "trail_path"}``。
        ``verdict`` ∈ ``allow`` / ``ask`` / ``block``。失败不 raise。

    Notes:
        顺序是刻意的: 人审类别先短路 (省一次模型调用), 再看注入的模型,
        最后才是普通策略链 —— 本模块只做**降级**, 从不把 ALLOW 升级成放行以外
        的任何东西。

    Example:
        >>> import asyncio
        >>> async def reviewer(**kwargs):
        ...     return "ask\\nthe action is destructive"
        >>> cfg = AutoReviewConfig(llm_caller=reviewer)
        >>> asyncio.run(auto_review(cfg, AutoReviewInput(
        ...     command="rm -rf /", instruction="clean up")))["verdict"]
        'ask'
    """
    trail: list[dict[str, Any]] = []

    def record(event: str, **payload: Any) -> None:
        step = {"step_no": len(trail) + 1, "event": event, **payload}
        trail.append(step)
        del trail[:-_MAX_TRAIL_STEPS]
        if on_step is not None:
            with contextlib.suppress(Exception):
                on_step(dict(step))

    try:
        command = str(input_data.command or "").strip()
        tool = str(input_data.tool or "").strip()
        provenance = classify_provenance(input_data.untrusted_sources)
        fingerprint = compute_fingerprint_for(config, input_data)
        record(
            "review.requested",
            tool=tool,
            effect=input_data.effect,
            provenance=provenance,
            goal_run_id=input_data.goal_run_id,
            bot_id=input_data.bot_id,
        )

        # 1. Human-only classes never reach the model (§4 of the design).
        if is_human_only(tool) or is_human_only(command):
            record("review.human_only", tool=tool)
            return _envelope(
                "ask",
                "human-only category (db migration / payment / secret / production)",
                reviewed=False,
                provenance=provenance,
                fingerprint=fingerprint,
                trail=trail,
                trail_path=_write_trail(output_dir, trail, enabled=config.record_trail),
            )

        # 2. No reviewer injected: nobody evaluated this, which is not permission.
        if config.llm_caller is None:
            record("review.no_reviewer")
            return _envelope(
                "ask",
                "no reviewer model is bound; a requirement nobody evaluated is not permission",
                reviewed=False,
                provenance=provenance,
                fingerprint=fingerprint,
                trail=trail,
                trail_path=_write_trail(output_dir, trail, enabled=config.record_trail),
            )

        # 3. Ask the reviewer.
        messages = build_review_prompt(
            command=command,
            instruction=input_data.instruction,
            tool=tool,
            effect=input_data.effect,
            untrusted_sources=list(input_data.untrusted_sources or []),
            arguments=dict(input_data.arguments or {}),
        )
        response = config.llm_caller(messages=messages, max_tokens=config.max_tokens)
        if asyncio.iscoroutine(response) or isinstance(response, asyncio.Future):
            response = await response
        parsed = parse_review_verdict(_content_of(response))
        record(
            "review.decided",
            verdict=parsed["verdict"],
            reason=parsed["reason"],
            provenance=provenance,
        )
        return _envelope(
            parsed["verdict"],
            parsed["reason"],
            reviewed=True,
            provenance=provenance,
            fingerprint=fingerprint,
            trail=trail,
            cost_usd=_cost_of(response),
            trail_path=_write_trail(output_dir, trail, enabled=config.record_trail),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # §5.4 不 raise; fail closed instead
        record("review.failed", error_type=type(exc).__name__)
        return _envelope(
            VERDICT_BLOCK,
            f"reviewer could not be reached: {type(exc).__name__}",
            reviewed=False,
            provenance=classify_provenance(input_data.untrusted_sources),
            fingerprint="",
            trail=trail,
            error={"code": type(exc).__name__, "reason": str(exc)},
            trail_path=_write_trail(output_dir, trail, enabled=config.record_trail),
        )


# Re-exported so a service-layer caller can branch without importing oskill.
REVIEW_ALLOW = VERDICT_ALLOW
REVIEW_BLOCK = VERDICT_BLOCK

__all__ += ["REVIEW_ALLOW", "REVIEW_BLOCK", "VERDICT_BLOCK"]
