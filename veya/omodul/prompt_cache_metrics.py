"""veya/omodul/prompt_cache_metrics — omodul_prompt_cache_metrics（主体 4）.

One recording transaction for prompt-cache accounting: take a per-call usage
mapping plus the turn's frozen section/tool digests, normalise cached vs
uncached input tokens, hand the record to the **injected** usage sink, and
return the aggregate. It owns no store — the sink is the existing
``server/events.py`` event store, injected by layer 4.

**归属（3O PARADIGM §6）**:

* cached/uncached 归一 = ``veya.oprim.prompt_cache.cached_token_split``；
* 命中率/冻结判定 = ``veya.oskill.pure.prompt_cache``；
* 标准签名 + ``status/error`` 必返 + ``_enabled_pillars`` 声明
  ``fingerprint``/``decision_trail``/``cost``；
* 不 import ``server.*``，不新建事件存储。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from veya.oskill.pure.prompt_cache import (
    frozen_tool_set,
    ordered_sections,
    toolset_unchanged,
    usage_cache_rate,
)

__all__ = [
    "PromptCacheMetricsConfig",
    "PromptCacheMetricsInput",
    "prompt_cache_metrics",
]


@dataclass
class PromptCacheMetricsConfig:
    """配置：注入的 usage sink + 支柱开关。"""

    #: 注入的记录器 ``(record: dict) -> None``。**唯一**的持久化出口。
    usage_sink: Callable[..., Any] | None = None
    #: 注入的 usage 读取器 ``() -> list[dict]``（统计用；可为空）。
    usage_reader: Callable[..., Any] | None = None

    _omodul_name: ClassVar[str] = "prompt_cache_metrics"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"session_ref", "turn_ref"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail", "cost"}


@dataclass
class PromptCacheMetricsInput:
    """输入：一次模型调用的 usage + 本回合冻结的分段/工具集。"""

    session_ref: str
    turn_ref: str = ""
    #: provider 返回的原始 usage（OpenAI / Anthropic 形态）。
    usage: dict[str, Any] = field(default_factory=dict)
    #: ``[(name, text)]``，顺序随意，内部会定序。
    sections: list[tuple[str, str]] = field(default_factory=list)
    #: 本回合开始时冻结的工具集。
    frozen_tool_names: tuple[str, ...] = ()
    frozen_tools_digest: str = ""
    #: 本次调用时实际带的工具 schema（用来发现中途变更）。
    tools: list[Any] = field(default_factory=list)
    #: 调用种类：main / steward / checkpoint / review / image ...
    kind: str = "main"
    model: str = ""
    bot_id: str = ""


def _int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


async def prompt_cache_metrics(
    config: PromptCacheMetricsConfig,
    input_data: PromptCacheMetricsInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """记录一次调用的 cached/uncached 输入 token 并返回聚合。

    Args:
        config: 注入的 usage sink / reader。
        input_data: usage + 冻结分段 + 工具集。
        output_dir: 未使用（记录出口是注入的 sink）；保留以满足签名。
        on_step: 同步进度回调。

    Returns:
        ``{"status", "error", "cached_input_tokens", "uncached_input_tokens",
        "prefix", "toolset", "cache_broken", "record", "aggregate",
        "decision_trail", "cost_usd"}``。未注入 sink 时仍返回归一后的数字，
        只是不落盘——统计不会因为没接事件存储而失真。失败不 raise。

    Example:
        >>> import asyncio
        >>> out = asyncio.run(prompt_cache_metrics(PromptCacheMetricsConfig(),
        ...     PromptCacheMetricsInput(session_ref="s1",
        ...         usage={"prompt_tokens": 100, "cached_tokens": 30})))
        >>> out["cached_input_tokens"]
        30
    """
    trail: list[dict[str, Any]] = []

    def record(event: str, **payload: Any) -> None:
        step = {"step_no": len(trail) + 1, "event": event, **payload}
        trail.append(step)
        if on_step is not None:
            with contextlib.suppress(Exception):
                on_step(dict(step))

    try:
        from veya.oprim.prompt_cache import cached_token_split

        cached, uncached = cached_token_split(input_data.usage or {})
        sections = ordered_sections(input_data.sections)
        frozen_names, frozen_digest = frozen_tool_set(input_data.tools)
        cache_broken = False
        if input_data.frozen_tools_digest:
            cache_broken = not toolset_unchanged(input_data.frozen_tools_digest, input_data.tools)
            if cache_broken:
                record(
                    "cache.toolset_changed",
                    frozen=list(input_data.frozen_tool_names),
                    current=list(frozen_names),
                )
        else:
            record("cache.toolset_frozen", tools=list(frozen_names))

        completion_tokens = _int(
            (input_data.usage or {}).get("completion_tokens")
            or (input_data.usage or {}).get("output_tokens")
        )
        entry: dict[str, Any] = {
            "session_ref": input_data.session_ref,
            "turn_ref": input_data.turn_ref,
            "kind": input_data.kind or "main",
            "model": input_data.model or "",
            "bot_id": input_data.bot_id or "",
            "cached_input_tokens": cached,
            "uncached_input_tokens": uncached,
            "output_tokens": completion_tokens,
            "cache_broken": cache_broken,
            "prefix_digests": {section["name"]: section["digest"] for section in sections},
            "tools_digest": frozen_digest,
        }
        record("cache.recorded", cached=cached, uncached=uncached)

        persisted = False
        if config.usage_sink is not None:
            with contextlib.suppress(Exception):
                sink_result = config.usage_sink(dict(entry))
                if hasattr(sink_result, "__await__"):
                    await sink_result  # type: ignore[misc]
                persisted = True
        if not persisted:
            record("cache.sink_missing")

        aggregate = {"calls": 1, "cached_input_tokens": cached, "uncached_input_tokens": uncached}
        if config.usage_reader is not None:
            with contextlib.suppress(Exception):
                reader_result = config.usage_reader()
                if hasattr(reader_result, "__await__"):
                    reader_result = await reader_result  # type: ignore[misc]
                if isinstance(reader_result, (list, tuple)):
                    aggregate = usage_cache_rate(list(reader_result))

        return {
            "status": "completed",
            "error": None,
            "cached_input_tokens": cached,
            "uncached_input_tokens": uncached,
            "output_tokens": completion_tokens,
            "prefix": [
                {"name": section["name"], "digest": section["digest"]} for section in sections
            ],
            "toolset": {"names": list(frozen_names), "digest": frozen_digest},
            "cache_broken": cache_broken,
            "persisted": persisted,
            "record": entry,
            "aggregate": aggregate,
            "decision_trail": trail,
            "cost_usd": 0.0,
        }
    except Exception as exc:  # §5.4 不 raise
        return {
            "status": "failed",
            "error": {"code": type(exc).__name__, "reason": str(exc)},
            "cached_input_tokens": 0,
            "uncached_input_tokens": 0,
            "output_tokens": 0,
            "prefix": [],
            "toolset": {"names": [], "digest": ""},
            "cache_broken": False,
            "persisted": False,
            "record": {},
            "aggregate": {},
            "decision_trail": trail,
            "cost_usd": 0.0,
        }