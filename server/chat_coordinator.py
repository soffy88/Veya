"""Compatibility adapter for the canonical MasterCoordinator-backed ``POST /chat`` route."""

from __future__ import annotations

from typing import Any

CHAT_SYSTEM_PROMPT = (
    "You are Veya, a helpful engineering assistant.\n"
    "\n"
    "# ARTIFACTS PROTOCOL (UI & CHARTS)\n"
    "If the user asks for a UI component, a data dashboard, or a chart, output a dynamic "
    "artifact using this exact XML wrapper when appropriate:\n"
    '<veya-artifact type="react" title="Name of the Component">\n'
    "// your code here\n"
    "</veya-artifact>\n"
    "The canonical MasterAgent remains responsible for deciding whether to answer or call "
    "a registered tool; this text only describes the legacy renderer contract.\n"
)


# /chat is a compatibility facade over the canonical MasterCoordinator. The
# MasterAgent owns model calls, ReAct, tools, history, and execution semantics.


async def chat(
    text: str,
    *,
    session_id: str,
    model: str | None = None,
    provider: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Adapt the canonical MasterCoordinator result to the legacy /chat shape."""
    from server.coordinator_master import master_coordinator

    result = await master_coordinator.chat_stream(
        text,
        session_id=session_id,
        model=model,
        provider=provider,
        config=config,
        system_context=CHAT_SYSTEM_PROMPT,
    )
    return {
        "content": result.get("final_answer", ""),
        "cost_usd": round(float(result.get("cost_usd") or 0.0), 6),
        "status": result.get("status"),
        "rounds": result.get("rounds", 0),
        "tool_calls": result.get("tool_calls", []),
    }
