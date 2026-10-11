"""veya/omodul/token_usage — omodul_token_usage（ADDENDUM S）.

The usage page needs token totals by time range, by bot and by model, including
the steward / checkpoint / memory-review / image-reading calls. All of those are
already in the **existing** usage events; this omodul only aggregates them and
never reads a store of its own.

**归属（3O PARADIGM §6）**: a deterministic fold over injected records —
no IO, no LLM, one transaction. Records are injected so the module stays free of
any ``server.*`` import.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

__all__ = [
    "RANGE_DAYS",
    "TokenUsageConfig",
    "TokenUsageInput",
    "token_usage",
]

#: Ranges the usage page offers (24h .. 12 months), plus an explicit day list.
RANGE_DAYS: dict[str, float] = {
    "24h": 1.0,
    "7d": 7.0,
    "30d": 30.0,
    "90d": 90.0,
    "180d": 180.0,
    "365d": 365.0,
}


@dataclass
class TokenUsageConfig:
    """配置：注入的读取器 + 范围表。"""

    #: 注入的读取器 ``() -> list[dict]``（既有 usage 事件）。
    usage_reader: Callable[..., Any] | None = None

    _omodul_name: ClassVar[str] = "token_usage"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"range_key"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint"}


@dataclass
class TokenUsageInput:
    """输入：范围键 / 显式天数 / 可选 bot 与 model 过滤。"""

    range_key: str = "7d"
    days: float | None = None
    bot_id: str = ""
    model: str = ""
    now: float = 0.0
    #: 调用方已筛好的记录（给了就不读 usage_reader）。
    records: list[dict[str, Any]] = field(default_factory=list)


def _num(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _usage_of(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    usage = record.get("usage")
    return usage if isinstance(usage, dict) else record


def _timestamp_of(record: Any) -> float:
    if not isinstance(record, dict):
        return 0.0
    for key in ("ts", "timestamp"):
        value = record.get(key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _cached_of(usage: dict[str, Any]) -> int:
    from veya.oprim.prompt_cache import cached_token_split

    return cached_token_split(usage)[0]


async def token_usage(
    config: TokenUsageConfig,
    input_data: TokenUsageInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Aggregate token usage by range / bot / model.

    Args:
        config: 注入的 usage 读取器。
        input_data: 范围、过滤条件、可选已筛记录。
        output_dir: 未使用（只读聚合）；保留以满足签名。
        on_step: 同步进度回调。

    Returns:
        ``{"status", "error", "range", "from_ts", "totals", "by_bot",
        "by_model", "by_kind", "calls", "fingerprint", "decision_trail",
        "cost_usd"}``。失败不 raise。

    Example:
        >>> import asyncio
        >>> out = asyncio.run(token_usage(TokenUsageConfig(),
        ...     TokenUsageInput(records=[{"usage": {"prompt_tokens": 5}}], now=0.0)))
        >>> out["totals"]["input_tokens"]
        5
    """
    trail: list[dict[str, Any]] = []
    try:
        days = input_data.days if input_data.days is not None else RANGE_DAYS.get(
            input_data.range_key, RANGE_DAYS["7d"]
        )
        now = float(input_data.now or 0.0)
        from_ts = now - float(days) * 86400.0
        records: Sequence[dict[str, Any]]
        if input_data.records:
            records = input_data.records
        elif config.usage_reader is not None:
            read = config.usage_reader()
            if hasattr(read, "__await__"):
                read = await read
            records = list(read or []) if isinstance(read, (list, tuple)) else []
        else:
            records = []
        trail.append({"step_no": 1, "event": "usage.read", "records": len(records)})

        totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "calls": 0}
        by_bot: dict[str, dict[str, int]] = {}
        by_model: dict[str, dict[str, int]] = {}
        by_kind: dict[str, dict[str, int]] = {}

        def bucket(store: dict[str, dict[str, int]], key: str) -> dict[str, int]:
            return store.setdefault(
                key or "unknown",
                {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "calls": 0},
            )

        for record in records:
            if _timestamp_of(record) and _timestamp_of(record) < from_ts:
                continue
            if input_data.bot_id and str(record.get("bot_id") or "") != input_data.bot_id:
                continue
            usage = _usage_of(record)
            if input_data.model and str(record.get("model") or "") != input_data.model:
                continue
            model = str(record.get("model") or usage.get("model") or "")
            if input_data.model and model != input_data.model:
                continue
            input_tokens = _num(usage.get("prompt_tokens") or usage.get("input_tokens"))
            cached = _cached_of(usage)
            output_tokens = _num(usage.get("completion_tokens") or usage.get("output_tokens"))
            for target in (
                totals,
                bucket(by_bot, str(record.get("bot_id") or "")),
                bucket(by_model, model),
                bucket(by_kind, str(record.get("kind") or "main")),
            ):
                target["input_tokens"] += input_tokens
                target["cached_input_tokens"] += cached
                target["output_tokens"] += output_tokens
                target["calls"] += 1

        return {
            "status": "completed",
            "error": None,
            "range": input_data.range_key,
            "days": float(days),
            "from_ts": from_ts,
            "to_ts": now,
            "totals": totals,
            "by_bot": by_bot,
            "by_model": by_model,
            "by_kind": by_kind,
            "calls": totals["calls"],
            "fingerprint": "",
            "decision_trail": trail,
            "cost_usd": 0.0,
        }
    except Exception as exc:  # §5.4 不 raise
        return {
            "status": "failed",
            "error": {"code": type(exc).__name__, "reason": str(exc)},
            "range": input_data.range_key,
            "days": 0.0,
            "from_ts": 0.0,
            "to_ts": 0.0,
            "totals": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "calls": 0},
            "by_bot": {},
            "by_model": {},
            "by_kind": {},
            "calls": 0,
            "fingerprint": "",
            "decision_trail": trail,
            "cost_usd": 0.0,
        }