"""veya/omodul/context_steward — omodul_context_steward（ADDENDUM M）.

At turn end, once the conversation passes the check line, ONE small model call
decides ``CHECKPOINT`` (condense in place) or ``HANDOFF`` (work stopped; the
next turn starts a new part). The user keeps seeing a single chat — a part is an
internal boundary, not a UI marker.

**归属（3O PARADIGM §6）**:

* 判定 + prompt 构建 + 解析 = `veya.oskill.pure.steward_decide`（纯，无 IO）；
* 标准签名 ``(config, input_data, output_dir) -> dict``，必返 status/error；
* 失败不 raise（``CancelledError`` 除外）；``_enabled_pillars`` 显式声明
  ``cost`` + ``decision_trail``（调 LLM 且改用户可见上下文，两者都必须可审计）；
* **不 import ``server.*``** —— 阈值、LLM 调用、落盘句柄全部由 layer4 注入。

不新建第二套压缩/调度：checkpoint 产物就是一个 summary 文本，由调用方写回
既有 session store（compact 的既有落点）。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from veya.oskill.pure.steward_decide import (
    CHECKPOINT,
    HANDOFF,
    build_steward_prompt,
    parse_steward_decision,
    steward_threshold,
)

__all__ = [
    "CHECKPOINT",
    "ContextStewardConfig",
    "ContextStewardInput",
    "HANDOFF",
    "context_steward",
    "compute_fingerprint_for",
]

#: Below / check / hard — mirrors ``steward_threshold``' return values.
BELOW = "below"


@dataclass
class ContextStewardConfig:
    """配置：注入的 LLM + 阈值 + 支柱开关。"""

    #: 注入的 LLM 调用 ``(messages) -> {"content": str, "cost_usd": float}``。
    llm_caller: Callable[..., Any] | None = None
    #: 可用窗口（模型总窗口）。
    window: int = 128_000
    #: 为回复预留的 token。
    reply_reserve: int = 4_000
    #: 判定线 / 硬线（占 usable window 的比例）。
    check_ratio: float = 0.8
    hard_ratio: float = 0.95
    #: 注入的 prompt 尾部消息条数。
    message_limit: int = 40

    _omodul_name: ClassVar[str] = "context_steward"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"session_ref", "part"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail", "cost"}


@dataclass
class ContextStewardInput:
    """输入：会话引用 + 当前 part + 最近的对话。"""

    #: 会话标识（用于指纹与 trail；不是 user id）。
    session_ref: str
    part: int = 1
    #: 最近的消息 ``[{"role","content"}]``，最旧在前。
    messages: list[dict[str, Any]] = field(default_factory=list)
    #: 注入的当前 token 估算；``None`` 表示由调用方算好后放在 messages 里。
    tokens: int | None = None
    #: 强制判定（``check``/``hard``）；``None`` 表示按 tokens 自动判定。
    force_threshold: str | None = None


#: Identity fields that must never reach a fingerprint or a decision trail
#: (§5.5.1).  Mirrors the canonical set; kept local because an omodul may not
#: import another omodul (OMODUL_DIRECT_OMODUL_CALLS), and the redaction itself
#: still goes through obase's single implementation via the platform loader.
_PII_KEYS: frozenset[str] = frozenset(
    {"user_id", "student_id", "account_id", "email", "phone", "name", "username", "device_id"}
)


def compute_fingerprint_for(config: Any, input_data: Any) -> str:
    """Expose the fingerprint for service-layer dedup (§5.5 MUST).

    Hashes the fields the config declares in ``_fingerprint_fields`` with the
    canonical audit-chain hash.  Implemented here rather than borrowed from
    another omodul because omodul→omodul calls are gated; the *hash* is still
    the single oprim implementation, so two omoduls fingerprinting the same
    fields agree byte for byte.

    Args:
        config: omodul config (reads ``_fingerprint_fields``).
        input_data: omodul input.

    Returns:
        24-char stable fingerprint; empty string when the fields are missing.
    """
    fields = set(getattr(config, "_fingerprint_fields", set()) or set())
    if not fields or input_data is None:
        return ""
    if hasattr(input_data, "model_dump"):
        data = input_data.model_dump()
    elif hasattr(input_data, "__dict__"):
        data = dict(input_data.__dict__)
    elif isinstance(input_data, dict):
        data = dict(input_data)
    else:
        return ""
    try:
        from veya.oprim.audit_chain import GENESIS_HASH, compute_event_hash

        return compute_event_hash(
            {k: data.get(k) for k in sorted(fields)}, prev_hash=GENESIS_HASH
        )[:24]
    except Exception:
        return ""


def _fingerprint_for(config: Any, input_data: Any) -> str:
    try:
        return compute_fingerprint_for(config, input_data)
    except Exception:
        return ""


def _strip_pii(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_pii(v) for k, v in value.items() if str(k).lower() not in _PII_KEYS}
    if isinstance(value, list):
        return [_strip_pii(item) for item in value]
    return value


def _redact(value: Any) -> Any:
    """Drop identity keys, then let obase mask anything secret-shaped."""
    stripped = _strip_pii(value)
    try:
        from veya.platform import load

        fn = getattr(load("obase"), "redact_value", None)
        if fn is not None:
            return fn(stripped)
    except Exception:
        pass
    return stripped


def _write_trail(output_dir: Path | None, trail: list[dict[str, Any]]) -> str | None:
    if output_dir is None:
        return None
    try:
        import json

        path = Path(output_dir) / "context_steward_trail.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_redact(trail), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return str(path)
    except Exception:
        return None


async def _reply_text(config: ContextStewardConfig, messages: list[dict[str, Any]]) -> tuple[str, float]:
    """Call the injected LLM, returning ``(content, cost_usd)``.

    A failing provider returns ``("", 0.0)`` so the caller reports "no decision"
    and leaves the conversation untouched. The await is deliberately *outside*
    any suppression: swallowing it would leave a never-awaited coroutine in hand
    and turn a provider outage into a fabricated decision.
    """
    caller = config.llm_caller
    if caller is None:
        return ("", 0.0)
    result: Any
    try:
        result = caller(messages)
        if hasattr(result, "__await__"):
            result = await result
    except Exception:
        return ("", 0.0)
    if isinstance(result, str):
        return (result, 0.0)
    if isinstance(result, dict):
        content = result.get("content")
        if not isinstance(content, str):
            choices = result.get("choices") or []
            message = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
            content = message.get("content")
        try:
            cost = float(result.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        return (str(content or ""), cost)
    return (str(result or ""), 0.0)


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    from veya.oskill.pure.context_compress import estimate_messages_tokens

    return int(estimate_messages_tokens(messages))


async def context_steward(
    config: ContextStewardConfig,
    input_data: ContextStewardInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """在回合末判断是否需要 CHECKPOINT / HANDOFF。

    Args:
        config: 注入的 LLM + 阈值。
        input_data: 会话引用、part、最近消息。
        output_dir: 可选；写 decision trail。
        on_step: 同步进度回调。

    Returns:
        ``{"status", "error", "action", "threshold", "part", "next_part",
        "summary", "verbatim", "reason", "fingerprint", "decision_trail",
        "cost_usd"}``。``action`` 为 ``"none"`` 表示未过线或不可判定——此时
        绝不改动上下文。失败不 raise。

    Example:
        >>> import asyncio
        >>> asyncio.run(context_steward(ContextStewardConfig(llm_caller=None),
        >>>     ContextStewardInput(session_ref="s1", messages=[], tokens=10)))
    """
    trail: list[dict[str, Any]] = []

    def record(event: str, **payload: Any) -> None:
        step = {"step_no": len(trail) + 1, "event": event, **payload}
        trail.append(step)
        if on_step is not None:
            with contextlib.suppress(Exception):
                on_step(dict(step))

    def result(**overrides: Any) -> dict[str, Any]:
        base = {
            "status": "completed",
            "error": None,
            "action": "none",
            "threshold": BELOW,
            "part": int(input_data.part or 1),
            "next_part": int(input_data.part or 1),
            "summary": "",
            "verbatim": "",
            "reason": "",
            "fingerprint": _fingerprint_for(config, input_data),
            "decision_trail": trail,
            "cost_usd": 0.0,
        }
        base.update(overrides)
        return base

    try:
        tokens = input_data.tokens
        if tokens is None:
            tokens = _estimate_tokens(input_data.messages)
        threshold = input_data.force_threshold or steward_threshold(
            tokens=int(tokens),
            window=int(config.window),
            reply_reserve=int(config.reply_reserve),
            check_ratio=float(config.check_ratio),
            hard_ratio=float(config.hard_ratio),
        )
        record("steward.evaluated", threshold=threshold, tokens=int(tokens))
        if threshold == BELOW:
            return result(reason="below check line")

        prompt = build_steward_prompt(
            input_data.messages,
            limit=int(config.message_limit),
            threshold=threshold,
        )
        content, cost = await _reply_text(config, prompt)
        record("steward.replied", chars=len(content))
        if not content.strip():
            return result(
                status="failed",
                error={"code": "NO_LLM_REPLY", "reason": "injected llm_caller returned nothing"},
                threshold=threshold,
                cost_usd=cost,
            )
        decision = parse_steward_decision(content)
        record(
            "steward.decided",
            action=decision["action"],
            parsed=decision["parsed"],
            reason=decision["reason"],
        )
        action = decision["action"]
        next_part = int(input_data.part or 1) + (1 if action == HANDOFF else 0)
        return result(
            action=action,
            threshold=threshold,
            next_part=next_part,
            summary=decision["summary"],
            verbatim=decision["verbatim"],
            reason=decision["reason"],
            cost_usd=cost,
        )
    except Exception as exc:  # §5.4 不 raise
        return result(
            status="failed",
            error={"code": type(exc).__name__, "reason": str(exc)},
        )