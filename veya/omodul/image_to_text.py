"""veya/omodul/image_to_text — omodul_image_to_text（ADDENDUM U）.

For a model without image input, a vision model describes the image first and
the text goes on in its place. Descriptions are cached by content hash, so the
same screenshot is described once regardless of how many turns quote it.

The describe call is **injected** (the caller passes
``veya.oskill.vision.analyze_image`` or any equivalent): this module owns the
cache and the substitution shape, never a second vision stack. The cache itself
is an injected ``dict`` so the module performs no I/O.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

__all__ = [
    "ImageToTextConfig",
    "ImageToTextInput",
    "describe_image_cached",
]


@dataclass
class ImageToTextConfig:
    """配置：注入的视觉描述器 + 缓存容器。"""

    #: 注入的描述器 ``(image_bytes, *, mime) -> {"description", "cost_usd"}``。
    describe: Callable[..., Any] | None = None
    #: 注入的内容哈希缓存 ``{sha256: description}``。
    cache: dict[str, str] = field(default_factory=dict)
    #: 注入的 LLM 记录器 ``(kind, tokens...) -> None``，用于 usage 页统计。
    usage_sink: Callable[..., Any] | None = None

    _omodul_name: ClassVar[str] = "image_to_text"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"mime"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "cost"}


@dataclass
class ImageToTextInput:
    """输入：一张图的原始字节 + MIME。"""

    image: bytes = b""
    mime: str = "image/png"


def _describe_text(raw: Any) -> tuple[str, float]:
    if isinstance(raw, str):
        return (raw, 0.0)
    if isinstance(raw, dict):
        description = raw.get("description") or raw.get("content")
        if isinstance(description, dict):
            description = description.get("content")
        try:
            cost = float(raw.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        return (str(description or ""), cost)
    return (str(raw or ""), 0.0)


async def describe_image_cached(
    config: ImageToTextConfig,
    input_data: ImageToTextInput,
    output_dir: Path | None = None,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Describe one image, memoised by content hash.

    Args:
        config: 注入的描述器 + 缓存 + usage 记录器。
        input_data: 图片字节 + MIME。
        output_dir: 未使用（缓存是注入的容器）；保留以满足签名。
        on_step: 同步进度回调。

    Returns:
        ``{"status", "error", "text", "cache_key", "cached", "mime",
        "fingerprint", "decision_trail", "cost_usd"}``。没有描述器或描述为空
        时 ``status="failed"`` 且 ``text=""``——调用方据此知道该给用户什么提示。
        失败不 raise。

    Example:
        >>> import asyncio
        >>> out = asyncio.run(describe_image_cached(ImageToTextConfig(describe=None),
        ...     ImageToTextInput(image=b"x")))
        >>> out["status"]
        'failed'
    """
    trail: list[dict[str, Any]] = []
    try:
        from veya.oprim.prompt_cache import section_digest

        digest = section_digest(
            f"{input_data.mime}:{len(input_data.image or b'')}:"
            f"{(input_data.image or b'')[:4096].hex()}",
            length=32,
        )
        if digest in config.cache:
            trail.append({"step_no": 1, "event": "image.cache_hit", "key": digest})
            return {
                "status": "completed",
                "error": None,
                "text": config.cache[digest],
                "cache_key": digest,
                "cached": True,
                "mime": input_data.mime,
                "fingerprint": digest[:24],
                "decision_trail": trail,
                "cost_usd": 0.0,
            }
        if config.describe is None:
            return {
                "status": "failed",
                "error": {"code": "NO_DESCRIBER", "reason": "no describe callable injected"},
                "text": "",
                "cache_key": digest,
                "cached": False,
                "mime": input_data.mime,
                "fingerprint": digest[:24],
                "decision_trail": trail,
                "cost_usd": 0.0,
            }
        raw = config.describe(input_data.image or b"", mime=input_data.mime)
        if hasattr(raw, "__await__"):
            raw = await raw
        text, cost = _describe_text(raw)
        if not text.strip():
            return {
                "status": "failed",
                "error": {"code": "EMPTY_DESCRIPTION", "reason": "vision model returned nothing"},
                "text": "",
                "cache_key": digest,
                "cached": False,
                "mime": input_data.mime,
                "fingerprint": digest[:24],
                "decision_trail": trail,
                "cost_usd": cost,
            }
        config.cache[digest] = text
        trail.append({"step_no": 1, "event": "image.described", "key": digest, "chars": len(text)})
        if config.usage_sink is not None:
            with contextlib.suppress(Exception):
                config.usage_sink(
                    {
                        "kind": "image",
                        "cached_input_tokens": 0,
                        "uncached_input_tokens": 0,
                        "output_tokens": len(text) // 4,
                    }
                )
        return {
            "status": "completed",
            "error": None,
            "text": text,
            "cache_key": digest,
            "cached": False,
            "mime": input_data.mime,
            "fingerprint": digest[:24],
            "decision_trail": trail,
            "cost_usd": cost,
        }
    except Exception as exc:  # §5.4 不 raise
        return {
            "status": "failed",
            "error": {"code": type(exc).__name__, "reason": str(exc)},
            "text": "",
            "cache_key": "",
            "cached": False,
            "mime": input_data.mime,
            "fingerprint": "",
            "decision_trail": trail,
            "cost_usd": 0.0,
        }
