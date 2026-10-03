"""Idempotent side-effect execution boundary."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from runtime.bot_scope import DEFAULT_BOT_ID

from .durable import ClaimEnvelope, DurableExecutionError, DurableExecutionRepository, content_hash

if TYPE_CHECKING:
    from server.action_receipt import ActionReceipt

logger = logging.getLogger("veya.execution.side_effects")

# P1-08: a side effect that can commit externally with no replay guarantee is
# the high-impact class. The provider capability the ledger already validates
# carries that guarantee, so the receipt class is derived from it instead of
# every caller restating its own risk profile.
_RECEIPT_CLASS_BY_CAPABILITY = {
    "none": "EXTERNAL_MUTATION",
    "manual_only": "PRIVILEGED",
    "compensation": "IRREVERSIBLE",
    "idempotency_key": "EXTERNAL_MUTATION",
    "status_probe": "EXTERNAL_MUTATION",
}
_DEFAULT_RECEIPT_CLASS = "EXTERNAL_MUTATION"


class SideEffectLedger:
    """Record-before-call protocol for providers that may commit externally."""

    def __init__(self, repository: DurableExecutionRepository):
        self.repository = repository
        self._operation_locks: dict[str, asyncio.Lock] = {}
        # P1-08: append-only receipt chain per goal run, plus one receipt per
        # declared operation so a replay never grows the chain.
        self._receipt_chains: dict[str, list[ActionReceipt]] = {}
        self._receipt_operations: set[tuple[str, str]] = set()
        self._receipt_lock = asyncio.Lock()
        self.receipt_failures = 0

    def receipts(self, goal_run_id: str) -> list[ActionReceipt]:
        """Receipts recorded for a goal run, in declaration order."""
        return list(self._receipt_chains.get(goal_run_id, ()))

    def verify_receipts(self, goal_run_id: str) -> tuple[bool, str]:
        """Verify the receipt chain of a goal run."""
        from server.action_receipt import verify_chain

        return verify_chain(self.receipts(goal_run_id))

    async def _record_receipt(
        self,
        *,
        goal_run_id: str,
        work_item_id: str,
        operation_key: str,
        operation_type: str,
        capability: str,
        target_ref: str,
        request: Any,
        bot_id: str,
        side_effect_class: str,
    ) -> ActionReceipt | None:
        """Emit one chained ActionReceipt for a high-impact declaration.

        Evidence only: any failure here leaves the side-effect path untouched.
        """
        try:
            from server.action_receipt import new_receipt, requires_receipt

            impact = side_effect_class or _RECEIPT_CLASS_BY_CAPABILITY.get(
                capability, _DEFAULT_RECEIPT_CLASS
            )
            if not requires_receipt(side_effect_class=impact):
                return None
            async with self._receipt_lock:
                if (goal_run_id, operation_key) in self._receipt_operations:
                    return None
                chain = self._receipt_chains.setdefault(goal_run_id, [])
                receipt = new_receipt(
                    goal_run_id=goal_run_id,
                    execution_id=work_item_id,
                    agent_id=bot_id,
                    action=operation_type,
                    capability=capability,
                    target=target_ref,
                    request_digest=content_hash(request),
                    previous_receipt_digest=chain[-1].signature if chain else None,
                )
                chain.append(receipt)
                self._receipt_operations.add((goal_run_id, operation_key))
                return receipt
        except Exception:
            self.receipt_failures += 1
            logger.warning(
                "action receipt failed for operation %s; side effect proceeds",
                operation_key,
                exc_info=True,
            )
            return None

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
        bot_id: str = DEFAULT_BOT_ID,
        request_fingerprint: str = "",
        side_effect_class: str = "",
    ) -> Any:
        lock = self._operation_locks.setdefault(operation_key, asyncio.Lock())
        async with lock:
            return await self._execute_unlocked(
                goal_run_id=goal_run_id,
                work_item_id=work_item_id,
                operation_key=operation_key,
                operation_type=operation_type,
                target_ref=target_ref,
                request=request,
                provider=provider,
                capability=capability,
                probe=probe,
                claim=claim,
                bot_id=bot_id,
                request_fingerprint=request_fingerprint,
                side_effect_class=side_effect_class,
            )

    async def _execute_unlocked(
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
        # P3-A: the owning bot. Reusing another bot's operation key is refused.
        bot_id: str = DEFAULT_BOT_ID,
        request_fingerprint: str = "",
        side_effect_class: str = "",
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
            bot_id=bot_id,
            request_fingerprint=request_fingerprint,
        )
        # ACTION_INSTANCE_MUTATION_REJECTED enforcement
        existing_fingerprint = row.get("request_fingerprint")
        if (
            existing_fingerprint
            and request_fingerprint
            and existing_fingerprint != request_fingerprint
        ):
            raise DurableExecutionError(
                "ACTION_INSTANCE_MUTATION_REJECTED",
                f"Action replay mismatch: expected fingerprint {existing_fingerprint}, got {request_fingerprint}",
            )

        # P1-08: high-impact declarations leave tamper-evident evidence before
        # the provider boundary, chained to the previous receipt of this run.
        if row.get("state") == "declared":
            await self._record_receipt(
                goal_run_id=goal_run_id,
                work_item_id=work_item_id,
                operation_key=operation_key,
                operation_type=operation_type,
                capability=capability,
                target_ref=target_ref,
                request=request,
                bot_id=bot_id,
                side_effect_class=side_effect_class,
            )

        previous = _decode_probe(row.get("probe_result_json"))
        if row.get("state") == "committed":
            return previous.get("result")
        if row.get("state") == "unknown":
            if probe is None or capability not in {"status_probe", "idempotency_key"}:
                raise DurableExecutionError(
                    "MANUAL_REVIEW_REQUIRED", "side effect outcome is unknown"
                )
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
                await self.repository.update_side_effect(
                    operation_key, state="unknown", probe_result=probe_result, claim=claim
                )
                raise DurableExecutionError(
                    "MANUAL_REVIEW_REQUIRED", "side effect probe is inconclusive"
                )
            await self.repository.update_side_effect(
                operation_key, state="started", probe_result=probe_result, claim=claim
            )

        await self.repository.update_side_effect(operation_key, state="started", claim=claim)
        try:
            result = provider()
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            # Cancellation after the provider boundary is not evidence that
            # the external effect did not happen. Persist UNKNOWN before
            # propagating cancellation so recovery can probe/reconcile instead
            # of replaying a possibly committed effect.
            await self.repository.update_side_effect(
                operation_key,
                state="unknown",
                probe_result={"status": "unknown", "error_class": "CancelledError"},
                claim=claim,
            )
            raise
        except Exception as exc:
            # Deterministic local tool failures did not create an external
            # side effect. Keep their evidence retryable instead of marking
            # the operation as an unknown external outcome.
            if type(exc).__name__ == "ToolExecutionError":
                await self.repository.update_side_effect(
                    operation_key,
                    state="failed",
                    probe_result={"status": "failed", "error": str(exc)},
                    claim=claim,
                )
                raise
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
