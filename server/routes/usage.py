"""server/routes/usage.py — Quota meter API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from runtime.coding.cli_agents import get_cli_agent_registry
from runtime.coding.quota import get_quota_meter
from server import auth as auth_mod

router = APIRouter(tags=["usage"])


class QuotaInfoResponse(BaseModel):
    backend: str
    account: str = "unknown"
    used: str | int = "unknown"
    limit: str | int = "unknown"
    reset_at: str = "unknown"
    unit: str = "unknown"


class QuotaResponse(BaseModel):
    quotas: list[QuotaInfoResponse]


def _get_repo_root() -> Path:
    import os

    root = os.environ.get("VEYA_PROJECT_ROOT", ".")
    return Path(root).expanduser().resolve()


@router.get("/api/v1/usage/quota", response_model=QuotaResponse)
async def get_quota() -> dict[str, list[dict[str, Any]]]:
    """Get quota information for all detected CLI agents."""
    repo_root = _get_repo_root()

    # Get all known backend names from CLI agent registry
    cli_registry = get_cli_agent_registry(repo_root)
    cli_profiles = cli_registry.list_all()
    backend_names = [p.name for p in cli_profiles if p.available]

    # Also include any registered backends from the backend registry
    try:
        from server.backends import get_backend_registry

        backend_registry = get_backend_registry()
        for b in backend_registry.list():
            if b["name"] not in backend_names:
                backend_names.append(b["name"])
    except Exception:
        pass

    # Get quota for each backend
    meter = get_quota_meter(repo_root)
    quotas = meter.get_all_quotas(backend_names)

    return {"quotas": [q.to_dict() for q in quotas]}


__all__ = ["router"]


# ── NANO-W10 ADDENDUM S: token usage detail ─────────────────────────────────
# Appended.  Read-only aggregation over the EXISTING usage events; no new store,
# tokens only (never cost, never request text).  Authentication is required so a
# second account cannot read the fleet's token totals.


class TokenUsageResponse(BaseModel):
    range: str
    days: float
    totals: dict[str, int]
    by_bot: dict[str, dict[str, int]] = {}
    by_model: dict[str, dict[str, int]] = {}
    by_kind: dict[str, dict[str, int]] = {}


@router.get("/api/v1/usage/tokens", response_model=TokenUsageResponse)
async def get_token_usage(
    range: str = "7d",
    days: float | None = None,
    bot_id: str = "",
    model: str = "",
    user: dict[str, Any] = Depends(auth_mod.require_user),
) -> dict[str, Any]:
    """Token usage by time range, bot and model (including steward/review calls)."""
    import time

    from server.bot_team.prompt_cache import USAGE_TOPIC, prompt_cache_enabled
    from server.events import event_store
    from veya.omodul.token_usage import TokenUsageConfig, TokenUsageInput, token_usage

    records: list[dict[str, Any]] = []
    if prompt_cache_enabled():
        for row in event_store.read_all(topics={USAGE_TOPIC}):
            records.append({**(row.get("payload") or {}), "ts": row.get("ts")})
    out = await token_usage(
        TokenUsageConfig(),
        TokenUsageInput(
            range_key=range,
            days=days,
            bot_id=bot_id,
            model=model,
            now=time.time(),
            records=records,
        ),
    )
    return {
        "range": out["range"],
        "days": out["days"],
        "totals": out["totals"],
        "by_bot": out["by_bot"],
        "by_model": out["by_model"],
        "by_kind": out["by_kind"],
    }
