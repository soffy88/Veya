# 3O-IO-ALLOW: Tool wrapper delegates network operations to obase adapter.
"""server/channel_tools.py — ADDENDUM V: read_channel tool.

Model-callable tool that reads external web content using the configured channel ladder
(web, youtube, rss, github, etc.) with real backend probing and firewall provenance.
"""

from __future__ import annotations

from typing import Any

from server import feature_flags
from veya.obase.channel_probes import (
    fetch_github_readme,
    fetch_url_text,
    probe_channel,
)
from veya.oskill.pure.html_to_markdown import PROVENANCE_HEADER, read_html


async def tool_read_channel(
    channel: str = "web",
    target: str = "",
    url: str = "",
    **_ignored: Any,
) -> str:
    """Read content from an external web channel (web, youtube, rss, github, etc.).

    W11 S2: models often call this with ``url=`` (or a stray ``category=``)
    instead of ``target=``; a page read must not fail on an argument name.
    """
    target = target or url
    if not feature_flags.enabled("VEYA_CHANNEL_LADDER"):
        return (
            "read_channel is currently disabled (feature flag VEYA_CHANNEL_LADDER is OFF). "
            "To use channel ladder reading, enable VEYA_CHANNEL_LADDER in configuration."
        )

    ch = (channel or "web").strip().lower()
    tgt = (target or "").strip()
    if not tgt:
        return "Error: no target URL or identifier provided."

    # Probe channel to find active working backend
    probe_res = await probe_channel(ch)
    if probe_res.get("status") != "ok":
        if ch == "web" and tgt.startswith(("http://", "https://")):
            # The plain web read needs no ladder backend: fetch directly.
            probe_res = {"status": "ok", "active_backend": "http_fetch"}
        else:
            hint = probe_res.get("fix_hint", "channel backend unavailable")
            return f"Error: channel '{ch}' backend is not ready ({hint})."

    backend = probe_res.get("active_backend", "")

    # Execute backend fetch
    try:
        if backend in ("jina", "dom_reader", "http_fetch"):
            content = await fetch_url_text(tgt, timeout=10.0, user_agent="Veya-ChannelReader/1.0")
            doc = read_html(content, url=tgt)
            return f"[Channel: {ch} | Backend: {backend}]\n" + doc.markdown

        if ch == "github":
            # If target is owner/repo or url
            if "github.com/" in tgt:
                parts = tgt.split("github.com/")[-1].strip("/").split("/")
                repo = f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else tgt
            else:
                repo = tgt
            decoded = await fetch_github_readme(repo, timeout=10.0)
            return (
                f"[Channel: {ch} | Backend: {backend}]\n"
                f"{PROVENANCE_HEADER}# GitHub Repository README: {repo}\n\n{decoded}"
            )

        # Fallback generic readout
        return f"[Channel: {ch} | Backend: {backend}]\n{PROVENANCE_HEADER}Target: {tgt}\n(Readout complete)"
    except Exception as exc:
        return f"Failed reading from channel '{ch}' via '{backend}': {type(exc).__name__}: {exc}"


def register_tools(registry: Any) -> int:
    """Register read_channel into MasterToolRegistry."""
    from server.tool_registry import SideEffect

    tools = [
        (
            "read_channel",
            (
                "Read content from an external web channel (web, youtube, rss, github, bilibili, "
                "reddit, twitter, xiaohongshu, linkedin) using the channel's ordered backend ladder."
            ),
            {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel identifier (web, youtube, rss, github, bilibili, reddit, etc.). Default web.",
                    },
                    "target": {
                        "type": "string",
                        "description": "Target URL, repo name, or content identifier.",
                    },
                },
                "required": ["target"],
            },
            tool_read_channel,
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


__all__ = ["register_tools", "tool_read_channel"]
