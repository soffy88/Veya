"""server/nano_w10/wake_router — layer-4 intake for every wake source (§2).

Every wake source calls :meth:`WakeRouter.ingest`. This module is pure
**assembly**; it holds no policy of its own and no store:

* the dedupe set is an in-process dict (same trade-off ``async_turns``
  documents: a restart forgets, which is safe because the durable record of a
  wake is the existing conversation / event);
* the on-signal path is the **existing** ``oservi.TriageEngine`` assembled
  through a ``ServiceManifest`` — this work adds no engine;
* delivery is the existing P5 push / W7 channel / W7 morning-digest functions,
  injected by the caller. Push and channels fire **only** for ``reply_now``.

Gated by ``VEYA_WAKE_ROUTER`` (default OFF).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from server.nano_w10.flags import flag_enabled
from veya.omodul.wake_router import (
    WAKE_SOURCES,
    WakeRouterConfig,
    WakeRouterInput,
    compute_fingerprint_for,
    wake_router,
)

_log = logging.getLogger("veya.bot_team.wake_router")

WAKE_ROUTER_FLAG = "VEYA_WAKE_ROUTER"


def wake_router_enabled() -> bool:
    """Is the wake router on? (spec item 2, default OFF)."""

    return flag_enabled(WAKE_ROUTER_FLAG)


def build_triage_engine(*, name: str = "veya-wake-triage") -> Any:
    """Assemble the EXISTING ``triage`` skeleton through a ServiceManifest.

    The classifier (:mod:`veya.oskill.pure.wake_policy`) is injected as the
    skeleton ``filters`` list — a filter is exactly "keep or drop", which is the
    decision this intake makes. ``llm_caller`` is the canonical
    ``oprim.llm_call`` (the same injection point :mod:`server.assembly` uses);
    the deterministic policy runs before it, so a signal that the policy drops
    never reaches the model at all.

    Args:
        name: manifest/service name.

    Returns:
        The assembled engine, or ``None`` when oservi is unavailable (the
        caller then runs the omodul directly — the intake never *requires*
        the engine).
    """
    try:
        from veya.platform import load

        load("oservi")  # mounts the skeleton registry on sys.path
        import oservi.assembler as assembler
        from oprim import llm_call

        manifest = assembler.ServiceManifest(
            name=name,
            skeleton="triage",
            inject={"llm_caller": llm_call, "filters": [_wake_filter]},
            trigger={"on_signal": True},
            config={"persona": "wake"},
        )
        return assembler.assemble(manifest)
    except Exception:
        _log.debug("triage engine unavailable; wake router will run without it", exc_info=True)
        return None


def _wake_filter(event: dict[str, Any]) -> bool:
    """Engine-level filter: keep everything the intake is allowed to classify."""

    return str(event.get("source") or "") in WAKE_SOURCES or True


class WakeRouter:
    """The one intake every wake source passes through.

    Parameters
    ----------
    settings:
        ``() -> {"notification_level": ..., "quiet_hours": {...}}`` per owner.
    on_deliver:
        ``(owner_ref, outcome, summary, payload) -> Any`` — the **existing**
        push / channel / digest delivery. Injected, never re-implemented here.
    """

    def __init__(
        self,
        *,
        settings: Callable[[], dict[str, Any]] | None = None,
        on_deliver: Callable[..., Any] | None = None,
        local_seconds: float | None = None,
    ) -> None:
        self._settings = settings
        self._on_deliver = on_deliver
        self._seen: dict[str, float] = {}
        self._engine = build_triage_engine()
        self._local_seconds = local_seconds

    # ── injected state handles ──────────────────────────────────────────

    def _seen_before(self, fingerprint: str) -> bool:
        if fingerprint in self._seen:
            return True
        cutoff = time.time() - 24 * 3600.0
        for stale in [key for key, at in self._seen.items() if at < cutoff]:
            self._seen.pop(stale, None)
        return False

    def _mark_seen(self, fingerprint: str, _signal: dict[str, Any]) -> None:
        self._seen[fingerprint] = time.time()

    # ── public surface ──────────────────────────────────────────────────

    def local_seconds(self) -> float:
        """Seconds since local midnight (the policy never reads a clock)."""

        if self._local_seconds is not None:
            return float(self._local_seconds)
        return float(time.localtime().tm_hour * 3600 + time.localtime().tm_min * 60)

    def ingest(
        self,
        *,
        owner_ref: str,
        source: str,
        summary: str,
        signal_id: str = "",
        user_directed: bool = False,
        urgent: bool = False,
        novel: bool = False,
        ack_only: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one wake through dedupe + policy, then deliver.

        Returns the omodul result. Delivery failures are recorded, never raised:
        a wake that could not be pushed still has a classification.
        """

        if not wake_router_enabled():
            return {
                "status": "failed",
                "error": {"code": "FEATURE_DISABLED", "flag": WAKE_ROUTER_FLAG},
                "outcome": "silent",
                "reason": f"{WAKE_ROUTER_FLAG} is disabled",
                "fingerprint": "",
                "duplicate": False,
                "source": source,
                "known_source": source in WAKE_SOURCES,
                "deliver": [],
                "decision_trail": [],
                "trail_path": "",
            }

        envelope = WakeRouterInput(
            owner_ref=owner_ref,
            source=source,
            signal_id=signal_id,
            summary=summary,
            user_directed=user_directed,
            urgent=urgent,
            novel=novel,
            ack_only=ack_only,
            payload=dict(payload or {}),
        )
        config = WakeRouterConfig(
            settings=self._settings,
            seen_before=self._seen_before,
            mark_seen=self._mark_seen,
            local_seconds=self.local_seconds(),
        )
        result = wake_router(config, envelope, None)

        if result["status"] == "completed" and result["deliver"] and self._on_deliver:
            try:
                self._on_deliver(
                    owner_ref,
                    result["outcome"],
                    summary,
                    dict(payload or {}),
                    list(result["deliver"]),
                )
                result["decision_trail"].append({"event": "wake.delivered", "to": result["deliver"]})
            except Exception as exc:
                _log.warning("wake delivery failed: %s", exc)
                result["decision_trail"].append(
                    {"event": "wake.delivery_failed", "error": type(exc).__name__}
                )
                result["reason"] += f" (delivery failed: {type(exc).__name__})"
        return result

    async def ingest_via_engine(self, **kwargs: Any) -> dict[str, Any] | None:
        """Run one wake through the assembled ``triage(on_signal)`` engine.

        Returns ``None`` when the engine is unavailable — the caller then uses
        :meth:`ingest`, which is the same policy without the skeleton.
        """

        engine = self._engine
        if engine is None:
            return None
        try:
            engine.run()
            return await engine.process(dict(kwargs))
        except Exception:
            _log.debug("triage on_signal failed; falling back to direct ingest", exc_info=True)
            return None

    def stats(self) -> dict[str, Any]:
        """Observability for the settings/health surface."""

        return {
            "engine": "triage" if self._engine is not None else "unavailable",
            "tracked_fingerprints": len(self._seen),
            "sources": list(WAKE_SOURCES),
        }


_ROUTER: WakeRouter | None = None


def get_wake_router() -> WakeRouter:
    """The process-wide :class:`WakeRouter`."""

    global _ROUTER
    if _ROUTER is None:
        _ROUTER = WakeRouter()
    return _ROUTER


def reset_wake_router() -> None:
    """Drop the process router (tests / operator reset)."""

    global _ROUTER
    _ROUTER = None


__all__ = [
    "WAKE_ROUTER_FLAG",
    "WakeRouter",
    "build_triage_engine",
    "compute_fingerprint_for",
    "get_wake_router",
    "reset_wake_router",
    "wake_router_enabled",
]
