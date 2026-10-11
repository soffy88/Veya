"""veya/omodul/bot_secrets — omodul_bot_secrets (每 bot 密钥管理, NANO-W10 Addendum Q).

Standard 3O omodul managing bot secrets and requests with decision_trail.
Does not import server.*: store and dependencies are injected through config.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

__all__ = [
    "BotSecretConfig",
    "BotSecretInput",
    "bot_secrets_operation",
]


@dataclass
class BotSecretConfig:
    """Dependencies and configuration injected by caller (layer 4)."""

    store: Any = None  # Injected BotSecretStore-compatible adapter


@dataclass
class BotSecretInput:
    """Action payload for bot secret operations."""

    action: str  # 'request', 'set_value', 'list', 'update_scope', 'delete'
    name: str = ""
    purpose: str = ""
    requesting_bot_id: str = ""
    value: str = ""
    scope: str = "this bot"  # 'this bot' | 'all bots'
    is_group: bool = False
    conversation_id: str = ""


async def bot_secrets_operation(
    config: BotSecretConfig,
    input_data: BotSecretInput,
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Execute a bot secret operation, recording decision trail."""
    _enabled_pillars: ClassVar[list[str]] = ["decision_trail"]

    store = config.store
    trail: list[dict[str, Any]] = [
        {
            "step": "intake",
            "action": input_data.action,
            "name": input_data.name,
            "requesting_bot_id": input_data.requesting_bot_id,
            "scope": input_data.scope,
            "is_group": input_data.is_group,
            "ts": time.time(),
        }
    ]

    if store is None:
        trail.append({"step": "error", "error": "store not configured", "ts": time.time()})
        return {
            "status": "failed",
            "error": {"code": "StoreUnconfigured", "message": "no secret store injected"},
            "decision_trail": trail,
        }

    try:
        act = (input_data.action or "").strip().lower()

        if act == "request":
            if input_data.is_group:
                trail.append({
                    "step": "rejected",
                    "reason": "SECRETS_NOT_IN_GROUP",
                    "ts": time.time(),
                })
                return {
                    "status": "failed",
                    "error": {
                        "code": "SECRETS_NOT_IN_GROUP",
                        "message": "secrets cannot be requested in a group chat",
                    },
                    "decision_trail": trail,
                }
            card = store.request_secret(
                name=input_data.name,
                purpose=input_data.purpose,
                requesting_bot_id=input_data.requesting_bot_id,
                is_group=input_data.is_group,
                conversation_id=input_data.conversation_id,
            )
            trail.append({"step": "card_created", "card": card, "ts": time.time()})
            res = {"status": "completed", "card": card, "decision_trail": trail}

        elif act == "set_value":
            meta = store.set_secret_value(
                name=input_data.name,
                value=input_data.value,
                scope=input_data.scope,
                requesting_bot_id=input_data.requesting_bot_id,
            )
            trail.append({"step": "value_saved", "name": input_data.name, "ts": time.time()})
            res = {"status": "completed", "meta": meta.to_dict(), "decision_trail": trail}

        elif act == "list":
            records = store.list_secrets(requesting_bot_id=input_data.requesting_bot_id or None)
            trail.append({"step": "listed", "count": len(records), "ts": time.time()})
            res = {"status": "completed", "secrets": records, "decision_trail": trail}

        elif act == "update_scope":
            meta = store.update_scope(name=input_data.name, scope=input_data.scope)
            trail.append({"step": "scope_updated", "scope": input_data.scope, "ts": time.time()})
            res = {
                "status": "completed" if meta else "failed",
                "meta": meta.to_dict() if meta else None,
                "decision_trail": trail,
            }

        elif act == "delete":
            deleted = store.delete_secret(name=input_data.name)
            trail.append({"step": "deleted", "name": input_data.name, "ts": time.time()})
            res = {"status": "completed", "deleted": deleted, "decision_trail": trail}

        else:
            return {
                "status": "failed",
                "error": {"code": "InvalidAction", "message": f"unknown action {input_data.action!r}"},
                "decision_trail": trail,
            }

        if output_dir:
            out_p = Path(output_dir)
            out_p.mkdir(parents=True, exist_ok=True)
            trail_path = out_p / f"secret_{input_data.name or 'op'}_trail.json"
            trail_path.write_text(json.dumps(trail, ensure_ascii=False, indent=2), encoding="utf-8")
            res["trail_path"] = str(trail_path)

        return res

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        trail.append({"step": "exception", "error": str(exc), "ts": time.time()})
        return {
            "status": "failed",
            "error": {"code": type(exc).__name__, "message": str(exc)},
            "decision_trail": trail,
        }
