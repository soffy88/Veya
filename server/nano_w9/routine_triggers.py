"""server/nano_w9/routine_triggers — layer-4 on_signal path for event triggers.

NANO-W9 element 2.  Routines may declare triggers besides ``schedule``:
``webhook`` (HMAC-signed per-routine secret), ``message`` (regex / mention) and
``reaction`` (e.g. 👍).

**No new scheduler.**  Matching is pure
(:mod:`veya.oskill.pure.trigger_match`); the *mechanism* is the existing
``oservi.TriageEngine`` (``trigger_mode="on_signal"``) assembled with project
filters; the *dispatch* is the existing
``server.goal_run.routine_dispatch.dispatch_trigger`` (GoalRun-bound,
idempotent) or the existing ``RoutineRegistry.lookup``.

Reaction-as-approval does **not** approve anything by itself: it raises an
approval request through the existing ``ActionGatewayAdapter`` /
``SideEffectLedger``.  W9 holds no approval state of its own.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server.nano_w9.flags import require_flag
from veya.oskill.pure.trigger_match import signal_matches_triggers

__all__ = [
    "ApprovalBridge",
    "RoutineSignalRouter",
    "build_triage_manifest",
    "extract_triggers",
    "verify_webhook_signature",
]


def extract_triggers(routine: Any) -> list[dict[str, Any]]:
    """Pull the trigger list off a routine record, whichever shape it is.

    Args:
        routine: a ``RoutineSpec`` (dataclass or its ``to_dict()``), or a dict.

    Returns:
        A normalised ``[{"kind": "webhook"|"message"|"reaction", ...}]`` list;
        empty when the routine declares none (then only ``schedule`` applies).

    Example:
        >>> extract_triggers({"triggers": [{"kind": "reaction", "reaction": "👍"}]})[0]["kind"]
        'reaction'
    """
    raw: Any = None
    if isinstance(routine, dict):
        raw = routine.get("triggers")
    elif hasattr(routine, "triggers"):
        raw = routine.triggers
    elif hasattr(routine, "to_dict"):
        try:
            raw = (routine.to_dict() or {}).get("triggers")
        except Exception:
            raw = None
    out: list[dict[str, Any]] = []
    for trigger in raw or []:
        if not isinstance(trigger, dict):
            continue
        kind = str(trigger.get("kind") or trigger.get("type") or "")
        if kind in {"webhook", "message", "reaction"}:
            out.append({**trigger, "kind": kind})
    return out


def _sign_payload(payload: bytes, secret: str, algo: str) -> str | None:
    """Canonical HMAC signer, resolved from obase (single source, §1.4).

    Tries the package-level ``obase.webhook`` export, then the submodule
    directly (older obase pins only expose the latter).  Returns ``None`` when
    obase is unmounted so the caller can fall back to the identical local HMAC.
    """
    import importlib

    module = None
    try:
        from veya.platform import load

        module = getattr(load("obase"), "webhook", None)
    except Exception:
        module = None
    if module is None:
        try:
            module = importlib.import_module("obase.webhook")
        except Exception:
            return None
    try:
        return str(module.sign_payload(payload=payload, secret=secret, algo=algo))  # type: ignore[arg-type]
    except Exception:
        return None


def verify_webhook_signature(
    body: bytes | str,
    *,
    secret: str,
    signature: str,
    algo: str = "sha256",
) -> bool:
    """Verify an HMAC webhook signature.

    Single source (§1.4): the canonical HMAC signer is ``obase.webhook.sign_payload``;
    this only adapts the byte form it expects and compares in constant time.  When
    the external main library is mounted the two agree exactly; otherwise the local
    fallback is the same HMAC construction.

    Args:
        body: the raw request body (bytes) or its text form.
        secret: the per-routine shared secret (≥32 bytes).
        signature: the hex digest presented by the caller.
        algo: ``"sha256"`` or ``"sha512"``.

    Returns:
        True only when the presented signature matches.  A short/empty secret or
        an unsupported algorithm is **False**, never an exception.

    Example:
        >>> verify_webhook_signature(b"{}", secret="s"*32, signature="0"*64)
        False
    """
    if not isinstance(secret, str) or len(secret) < 32:
        return False
    if not isinstance(signature, str) or not signature:
        return False
    if algo not in {"sha256", "sha512"}:
        return False
    payload = body if isinstance(body, bytes) else str(body).encode("utf-8")
    expected = _sign_payload(payload, secret, algo)
    if expected is None:
        digestmod = hashlib.sha256 if algo == "sha256" else hashlib.sha512
        expected = hmac.new(secret.encode("utf-8"), payload, digestmod).hexdigest()
    return hmac.compare_digest(expected, signature)


@dataclass
class ApprovalBridge:
    """Raises approval requests through the **existing** action gateway.

    This is the only place W9 touches approvals, and it does so by *delegating*:
    ``ActionGatewayAdapter.execute`` runs the policy hook, the ledger records the
    declaration, and ``server.user_control`` resolves the decision.  W9 never
    stores an approval verdict.
    """

    #: (action, arguments) -> awaitable verdict (injected in tests)
    request_approval: Callable[..., Any] | None = None
    #: ledger callables used for the audit trail
    audit_writer: Callable[..., Any] | None = None
    last_request: dict[str, Any] = field(default_factory=dict)

    async def request(self, *, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Ask the existing approval path to decide ``action``.

        Args:
            action: the effect name (e.g. ``"routine.approve"``).
            arguments: redacted arguments for the approver.

        Returns:
            ``{"status": "requested"|"unavailable", "verdict": str}``.
        """
        self.last_request = {"action": action, "arguments": dict(arguments or {})}
        if self.request_approval is None:
            return {"status": "unavailable", "verdict": ""}
        try:
            verdict = self.request_approval(action, arguments)
            if hasattr(verdict, "__await__"):
                verdict = await verdict
        except Exception as exc:
            return {"status": "error", "verdict": "", "error": str(exc)}
        return {"status": "requested", "verdict": str(verdict or "")}


def build_triage_manifest(
    *,
    name: str,
    filters: list[Callable[[dict[str, Any]], bool]],
    config: dict[str, Any] | None = None,
) -> Any:
    """Assemble the **existing** ``oservi.TriageEngine`` via a ServiceManifest.

    Matching is deterministic, so the injected ``llm_caller`` is a passthrough
    scorer rather than a model — the triage engine's scoring hook stays
    injectable but the on_signal path itself costs nothing.

    Args:
        name: manifest/service name.
        filters: layer4 predicates (each takes a signal, returns keep/drop).
        config: engine config (``min_score``, ``dedup_bucket_seconds``, ...).

    Returns:
        A ``ServiceManifest`` ready for ``oservi.assemble(manifest)``, or ``None``
        when the external oservi library is not mounted (the caller then runs the
        router directly, which behaves identically).
    """
    try:
        from veya.platform import load

        oservi = load("oservi")
    except Exception:
        return None

    def passthrough_scorer(
        *, config: dict[str, Any] | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        return []

    return oservi.ServiceManifest(
        name=name,
        skeleton="triage",
        inject={"llm_caller": [passthrough_scorer], "filters": list(filters)},
        trigger={"on_signal": True},
        config=dict(config or {}),
    )


class RoutineSignalRouter:
    """Route an inbound signal to the routines whose triggers it satisfies.

    Wires the three pieces: pure matching → existing triage engine (mechanism) →
    existing routine registry + dispatch (business).
    """

    def __init__(
        self,
        *,
        registry: Any = None,
        dispatch: Callable[..., Any] | None = None,
        approval: ApprovalBridge | None = None,
        lookup: Callable[[], list[Any]] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.registry = registry
        self.dispatch = dispatch
        self.approval = approval or ApprovalBridge()
        self.lookup = lookup or self._default_lookup
        self.config = dict(config or {})
        self.manifest = build_triage_manifest(
            name="routine-signal-router", filters=[], config=self.config
        )

    def _default_lookup(self) -> list[Any]:
        if self.registry is None:
            return []
        try:
            return list(self.registry.list(include_disabled=False) or [])
        except TypeError:
            return list(self.registry.list() or [])
        except Exception:
            return []

    def routines(self) -> list[Any]:
        """All enabled routines with event triggers."""
        return [r for r in self.lookup() if extract_triggers(r)]

    def match(self, signal: dict[str, Any]) -> list[dict[str, Any]]:
        """Which routines does this signal fire?

        Args:
            signal: ``{"kind", "text"|"reaction"|"event", "verified", ...}``.

        Returns:
            ``[{"routine_id", "trigger_index", "trigger", "routine"}]``, ordered by
            routine id so a replay of the same signal is deterministic.

        Example:
            >>> # router.match({"kind": "reaction", "reaction": "👍"})
        """
        hits: list[dict[str, Any]] = []
        for routine in self.routines():
            triggers = extract_triggers(routine)
            for index in signal_matches_triggers(signal, triggers):
                hits.append(
                    {
                        "routine_id": str(getattr(routine, "routine_id", "") or ""),
                        "trigger_index": index,
                        "trigger": triggers[index],
                        "routine": routine,
                    }
                )
        hits.sort(key=lambda h: (h["routine_id"], h["trigger_index"]))
        return hits

    async def handle(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Handle one signal end to end (match → approval or dispatch).

        Args:
            signal: the inbound signal.

        Returns:
            ``{"status", "fired", "dispatched", "approvals", "errors"}``.  A signal
            that fires nothing is ``{"status": "no_match"}`` — not an error.

        Raises:
            FeatureDisabled: when ``VEYA_ROUTINE_TRIGGERS`` is off.
        """
        require_flag("VEYA_ROUTINE_TRIGGERS")
        hits = self.match(signal)
        if not hits:
            return {
                "status": "no_match",
                "fired": [],
                "dispatched": [],
                "approvals": [],
                "errors": [],
            }

        fired: list[dict[str, Any]] = []
        dispatched: list[dict[str, Any]] = []
        approvals: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for hit in hits:
            trigger = hit["trigger"]
            action = str(trigger.get("action") or "")
            if action in {"approve", "approval"}:
                outcome = await self.approval.request(
                    action=f"routine.{action}",
                    arguments={
                        "routine_id": hit["routine_id"],
                        "reaction": str(signal.get("reaction") or ""),
                    },
                )
                approvals.append({**outcome, "routine_id": hit["routine_id"]})
                fired.append({**hit, "outcome": outcome})
                continue
            result = self._dispatch(hit, signal)
            if result.get("status") == "ok":
                dispatched.append(result)
            else:
                errors.append({"routine_id": hit["routine_id"], **result})
            fired.append({**hit, "outcome": result})

        return {
            "status": "ok" if not errors else "partial",
            "fired": fired,
            "dispatched": dispatched,
            "approvals": approvals,
            "errors": errors,
        }

    def _dispatch(self, hit: dict[str, Any], signal: dict[str, Any]) -> dict[str, Any]:
        """Feed the existing routine dispatch (GoalRun authority) — never a 2nd scheduler."""
        if self.dispatch is None:
            return {"status": "skipped", "reason": "no dispatcher injected"}
        topic = str(hit["trigger"].get("dispatch_topic") or "event.arrive")
        trigger_id = str(signal.get("id") or signal.get("event") or "signal")
        try:
            out = self.dispatch(topic=topic, trigger_id=trigger_id, payload=dict(signal))
        except TypeError:
            out = self.dispatch(topic, trigger_id, dict(signal))
        except Exception as exc:
            return {"status": "error", "reason": str(exc)}
        return {"status": "ok", "routine_id": hit["routine_id"], "result": out}
