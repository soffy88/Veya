"""Omni-Channel Gateway inbound: lightweight webhook -> notification toast.

Deliberately not routed through server/automata.py's webhook (which wakes a full
MasterCoordinator LLM session on unauthenticated payloads) — this just surfaces
"something happened" as a toast via server/notification_center.py. Lower blast
radius, so no auth is required for this endpoint either, unlike a mission-triggering
webhook would need.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter

from server.firewall import VeyaFirewall
from server.notification_center import global_notifier

router = APIRouter(prefix="/omni", tags=["omni"])


@router.post("/webhook/{channel_name}")
async def inbound_webhook(channel_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False)[:500]
    result = VeyaFirewall.sanitize(raw, source=f"webhook:{channel_name}")
    global_notifier.push(
        "INFO",
        f"{channel_name} 有新动态",
        result["sanitized_content"],
        {"channel": channel_name, "payload": payload, "firewall_safe": result["safe"]},
    )
    return {"status": "received", "firewall_safe": result["safe"]}
