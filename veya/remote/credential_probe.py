"""Lazy, cached credential probing for executors.

`credential_structure` can refute a credential with local evidence but cannot
confirm one. Confirmation needs a real call, and that call is expensive and must
not happen on the import path: on 2026-10-04 the real calls measured
opencode 32.0s, pi 12.7s, codex 20.6s and claude_code 217.7s. `executor_registry`
discovery stays network-free, and this module is what a caller reaches for when
it actually needs to know.

Three properties this exists to guarantee:

**Probing is opt-in per provider.** A provider with no verified probe is reported
`UNPROBABLE`, never `VALID` and never `INVALID`. Emitting `INVALID` for an
unprobed provider would exclude working executors — the exact regression that
`credential_valid = None` was introduced to prevent.

**A result is cached and not re-fetched.** TTL for success, exponential backoff
for failure, because the failure path is the expensive one and a broken provider
is the case most likely to be retried in a loop.

**A verdict is never stronger than its evidence.** `VALID` requires the probe to
have completed successfully; a probe that raises is `UNKNOWN`, not `VALID`.

No probe is registered by default. As of 2026-10-04 no non-generative
authenticated endpoint could be verified for any executor:
`https://opencode.ai/zen/v1/models` answers HTTP 403 `error code: 1010`
identically for a valid key, a deliberately wrong key and no credential at all,
so it cannot discriminate. Registering it would mark a working credential
invalid. See docs/reports/CREDENTIAL_ENDPOINT_RESEARCH.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "DEFAULT_BACKOFF",
    "DEFAULT_TTL",
    "MAX_BACKOFF",
    "CredentialProbeCache",
    "ProbeOutcome",
    "ProbeResult",
]

DEFAULT_TTL = 900.0
DEFAULT_BACKOFF = 300.0
MAX_BACKOFF = 3600.0


class ProbeOutcome(StrEnum):
    """What a real call established about a credential."""

    #: A real authenticated call succeeded.
    VALID = "VALID"
    #: The provider rejected the credential itself.
    AUTH_FAILURE = "AUTH_FAILURE"
    #: The provider could not be reached, or refused for non-credential reasons.
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    #: The provider was reachable but the request was malformed or unsupported,
    #: which says nothing about the credential.
    PROVIDER_CONFIGURATION_FAILURE = "PROVIDER_CONFIGURATION_FAILURE"
    #: No verified probe exists for this provider. Not evidence either way.
    UNPROBABLE = "UNPROBABLE"
    #: This executor owns no credential the registry can see, so credential
    #: validity is not a gate for it. Established by real acceptance on
    #: 2026-10-04: antigravity declares no credential source at all and still
    #: completes real tasks, so refusing it for an unprovable credential would
    #: exclude a working executor.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: A registered probe ran and failed to produce a verdict.
    UNKNOWN = "UNKNOWN"


#: Outcomes that count as a definitive negative about the credential.
NEGATIVE_OUTCOMES = frozenset(
    {
        ProbeOutcome.AUTH_FAILURE,
        ProbeOutcome.PROVIDER_UNAVAILABLE,
        ProbeOutcome.PROVIDER_CONFIGURATION_FAILURE,
    }
)


@dataclass(frozen=True)
class ProbeResult:
    """One verdict, with when it was obtained.

    `detail` is safe to log and to surface in an error message: it describes
    shape and status codes, never credential material.
    """

    outcome: ProbeOutcome
    checked_at: float
    detail: str = ""

    @property
    def is_valid(self) -> bool:
        return self.outcome is ProbeOutcome.VALID

    @property
    def is_negative(self) -> bool:
        return self.outcome in NEGATIVE_OUTCOMES

    @property
    def is_not_applicable(self) -> bool:
        return self.outcome is ProbeOutcome.NOT_APPLICABLE

    @property
    def is_unusable(self) -> bool:
        """Whether this result rules the credential out.

        False for UNPROBABLE and UNKNOWN on purpose. Absence of a probe is not
        evidence of a bad credential, and treating it as such is how a working
        executor gets locked out of the running.
        """
        return self.is_negative


# A probe returns a verdict, or raises to be classified as UNKNOWN.
ProbeFn = Callable[[], str | ProbeOutcome]


class CredentialProbeCache:
    """TTL for success, exponential backoff for failure, per executor."""

    def __init__(
        self,
        ttl: float = DEFAULT_TTL,
        backoff: float = DEFAULT_BACKOFF,
        max_backoff: float = MAX_BACKOFF,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._backoff = backoff
        self._max_backoff = max_backoff
        self._clock = clock
        self._probes: dict[str, ProbeFn] = {}
        self._results: dict[str, ProbeResult] = {}
        self._failures: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}

    # ── registration ────────────────────────────────────────────────────────
    def register(self, executor_id: str, probe: ProbeFn) -> None:
        """Attach a verified probe. Registering is an explicit claim that the
        probe discriminates, so an unverified endpoint must not be added."""
        self._probes[executor_id] = probe

    def has_probe(self, executor_id: str) -> bool:
        return executor_id in self._probes

    # ── reading ─────────────────────────────────────────────────────────────
    def peek(self, executor_id: str) -> ProbeResult | None:
        """The cached verdict without probing or refreshing anything."""
        return self._results.get(executor_id)

    def credential_valid(self, executor_id: str) -> bool | None:
        """Tri-state validity derived from a probe.

        None whenever the probe has not established anything, which includes
        having no probe. Never True without a completed successful call.
        """
        result = self._results.get(executor_id)
        if result is None:
            return None
        if result.is_valid:
            return True
        if result.is_negative:
            return False
        return None

    # ── probing ─────────────────────────────────────────────────────────────
    def probe(self, executor_id: str, *, force: bool = False) -> ProbeResult:
        """Return a verdict, probing only if the cache does not already settle it."""
        now = self._clock()
        if not force:
            cached = self._reusable(executor_id, now)
            if cached is not None:
                return cached

        probe_fn = self._probes.get(executor_id)
        if probe_fn is None:
            # Not cached as a failure: nothing was learned about the credential,
            # so backoff must not make a later probe stale.
            return ProbeResult(ProbeOutcome.UNPROBABLE, now, "no verified probe registered")

        result = self._run(probe_fn, now)
        self._results[executor_id] = result
        if result.is_negative:
            count = self._failures.get(executor_id, 0) + 1
            self._failures[executor_id] = count
            delay = min(self._backoff * (2 ** (count - 1)), self._max_backoff)
            self._retry_after[executor_id] = now + delay
        else:
            self._failures.pop(executor_id, None)
            self._retry_after.pop(executor_id, None)
        return result

    def _run(self, probe_fn: ProbeFn, now: float) -> ProbeResult:
        try:
            raw = probe_fn()
        except Exception as exc:  # a broken probe is not a bad credential
            return ProbeResult(ProbeOutcome.UNKNOWN, now, f"{type(exc).__name__}: {str(exc)[:120]}")
        if isinstance(raw, ProbeResult):
            return ProbeResult(raw.outcome, now, raw.detail)
        if isinstance(raw, ProbeOutcome):
            return ProbeResult(raw, now)
        return ProbeResult(ProbeOutcome.UNKNOWN, now, f"probe returned {type(raw).__name__}")

    def _reusable(self, executor_id: str, now: float) -> ProbeResult | None:
        result = self._results.get(executor_id)
        if result is None:
            return None
        if result.is_negative:
            if now < self._retry_after.get(executor_id, 0.0):
                return result
            return None
        # UNPROBABLE and UNKNOWN are not credential findings, so they never
        # become stale in a way that would block a real probe.
        if not (result.is_valid or result.outcome is ProbeOutcome.UNPROBABLE):
            return None
        if now - result.checked_at >= self._ttl:
            return None
        return result

    def retry_after(self, executor_id: str) -> float | None:
        """Seconds until the next probe is allowed, if backoff is active."""
        until = self._retry_after.get(executor_id)
        return None if until is None else max(0.0, until - self._clock())

    def forget(self, executor_id: str) -> None:
        self._results.pop(executor_id, None)
        self._failures.pop(executor_id, None)
        self._retry_after.pop(executor_id, None)
