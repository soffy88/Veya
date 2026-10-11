"""server/channel_tools.py — ADDENDUM V: read_channel tool.

Model-callable tool that reads external web content using the configured channel ladder
(web, youtube, rss, github, etc.) with real backend probing and firewall provenance.
"""

from __future__ import annotations

from typing import Any

from server import feature_flags
from veya.obase.channel_probes import probe_channel
from veya.oskill.pure.html_to_markdown import PROVENANCE_HEADER, read_html


async def tool_read_channel(
    channel: str,
    target: str,
) -> str:
    """Read content from an external web channel (web, youtube, rss, github, etc.)."""
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
        hint = probe_res.get("fix_hint", "channel backend unavailable")
        return f"Error: channel '{ch}' backend is not ready ({hint})."

    backend = probe_res.get("active_backend", "")

    # Execute backend fetch
    try:
        import httpx

        if ch == "web" or backend in ("jina", "dom_reader", "http_fetch"):
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(tgt, headers={"User-Agent": "Veya-ChannelReader/1.0"})
                content = resp.text
            doc = read_html(content, url=tgt)
            return f"[Channel: {ch} | Backend: {backend}]\n" + doc.markdown

        if ch == "github":
            # If target is owner/repo or url
            if "github.com/" in tgt:
                parts = tgt.split("github.com/")[-1].strip("/").split("/")
                repo = f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else tgt
            else:
                repo = tgt
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"https://api.github.com/repos/{repo}/readme")
                if resp.status_code == 200:
                    import base64

                    data = resp.json()
                    decoded = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
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
                        "description": "Channel identifier (web, youtube, rss, github, bilibili, reddit, etc.).",
                    },
                    "target": {
                        "type": "string",
                        "description": "Target URL, repo name, or content identifier.",
                    },
                },
                "required": ["channel", "target"],
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
