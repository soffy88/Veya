# 3O-IO-ALLOW: Tool wrapper delegates network operations to obase adapter.
"""server/dom_reader_tools.py — ADDENDUM R: read_browser tool.

Model-callable tool that converts web DOM to structured Markdown with [#n] control
indexing, sensitive field redaction, SSRF defense, and firewall provenance.
"""

from __future__ import annotations

from typing import Any

from server import feature_flags
from veya.obase.channel_probes import fetch_url_text
from veya.oskill.pure.html_to_markdown import is_ssrf_safe_url, read_html
from veya.gsk.envelope import sanitize_and_wrap


async def tool_read_browser(
    url: str | None = None,
    html: str | None = None,
    max_chars: int | None = None,
) -> str:
    """Read webpage or DOM as structured Markdown with [#n] control indexing."""
    if not feature_flags.enabled("VEYA_DOM_READER"):
        return (
            "read_browser is currently disabled (feature flag VEYA_DOM_READER is OFF). "
            "To use DOM reading, enable VEYA_DOM_READER in configuration."
        )

    target_url = (url or "").strip()
    html_content = html or ""

    if target_url:
        safe, reason = is_ssrf_safe_url(target_url)
        if not safe:
            return f"Error: URL rejected by security policy ({reason})"

        try:
            html_content = await fetch_url_text(target_url, timeout=5.0, user_agent="Veya-DOM-Reader/1.0")
        except Exception as exc:
            return f"Failed to fetch {target_url}: {type(exc).__name__}: {exc}"

    if not html_content:
        return "Error: no HTML content provided and no active browser page available."

    doc = read_html(html_content, url=target_url)
    res = doc.markdown
    if max_chars and max_chars > 0 and len(res) > max_chars:
        res = res[:max_chars] + "\n\n[Content truncated by max_chars limit]"
    return sanitize_and_wrap(res, source_type="read_browser")


def register_tools(registry: Any) -> int:
    """Register read_browser into MasterToolRegistry."""
    from server.tool_registry import SideEffect

    tools = [
        (
            "read_browser",
            (
                "Convert visible web DOM to structured Markdown with [#n] indexed references "
                "for interactive controls and links. Automatically skips hidden elements, "
                "redacts sensitive inputs, and enforces strict safety limits."
            ),
            {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Optional webpage URL to fetch and convert to Markdown.",
                    },
                    "html": {
                        "type": "string",
                        "description": "Optional raw HTML string to convert to Markdown.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Optional maximum characters to return.",
                    },
                },
            },
            tool_read_browser,
            SideEffect.PURE_READ,
        ),
    ]

    count = 0
    for name, desc, schema, fn, side_effect in tools:
        if getattr(registry, "has", None) and registry.has(name):
            continue
        if hasattr(registry, "register"):
            registry.register(
                name=name,
                description=desc,
                parameters=schema,
                func=fn,
                side_effect=side_effect,
            )
            count += 1
    return count


__all__ = ["register_tools", "tool_read_browser"]
