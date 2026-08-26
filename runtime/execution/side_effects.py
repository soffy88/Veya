"""Idempotent side-effect execution boundary."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from .durable import ClaimEnvelope, DurableExecutionError, DurableExecutionRepository


class SideEffectLedger:
    """Record-before-call protocol for providers that may commit externally."""

    def __init__(self, repository: DurableExecutionRepository):
        self.repository = repository

    async def execute(
        self,
        *,
        goal_run_id: str,
        work_item_id: str,
        operation_key: str,
        operation_type: str,
        target_ref: str,
        request: Any,
        provider: Callable[[], Awaitable[Any] | Any],
        capability: str = "manual_only",
        probe: Callable[[], Awaitable[dict[str, Any]] | dict[str, Any]] | None = None,
        claim: ClaimEnvelope | None = None,
    ) -> Any:
        row = await self.repository.declare_side_effect(
            goal_run_id=goal_run_id,
            work_item_id=work_item_id,
            operation_key=operation_key,
            operation_type=operation_type,
            target_ref=target_ref,
            request=request,
            capability=capability,
            claim=claim,
        )
        previous = _decode_probe(row.get("probe_result_json"))
        if row.get("state") == "committed":
            return previous.get("result")
        if row.get("state") == "unknown":
            if probe is None or capability not in {"status_probe", "idempotency_key"}:
                raise DurableExecutionError("MANUAL_REVIEW_REQUIRED", "side effect outcome is unknown")
            probe_result = probe()
            if inspect.isawaitable(probe_result):
                probe_result = await probe_result
            probe_result = dict(probe_result or {})
            probe_status = probe_result.get("status")
            if probe_status in {"committed", "succeeded"}:
                await self.repository.update_side_effect(
                    operation_key,
                    state="committed",
                    provider_request_id=probe_result.get("provider_request_id"),
                    probe_result={**probe_result, "result": probe_result.get("result")},
                    claim=claim,
                )
                return probe_result.get("result")
            if probe_status not in {"not_found", "not_started"} and capability != "idempotency_key":
                await self.repository.update_side_effect(operation_key, state="unknown", probe_result=probe_result, claim=claim)
                raise DurableExecutionError("MANUAL_REVIEW_REQUIRED", "side effect probe is inconclusive")
            await self.repository.update_side_effect(operation_key, state="started", probe_result=probe_result, claim=claim)

        await self.repository.update_side_effect(operation_key, state="started", claim=claim)
        try:
            result = provider()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            # A provider exception after the call boundary is deliberately
            # unknown; callers may classify a preflight failure separately.
            await self.repository.update_side_effect(
                operation_key,
                state="unknown",
                probe_result={"status": "unknown", "error_class": type(exc).__name__},
                claim=claim,
            )
            raise DurableExecutionError("SIDE_EFFECT_UNKNOWN", str(exc)) from exc
        await self.repository.update_side_effect(
            operation_key,
            state="committed",
            probe_result={"status": "committed", "result": result},
            claim=claim,
        )
        return result


def _decode_probe(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        import json

        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
