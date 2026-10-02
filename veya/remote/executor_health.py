"""Canonical Executor Health, Failure Taxonomy, and Health-Aware Routing.

Separates qualification from runtime health:
- Qualification: "this executor/runtime implementation passed verification"
- Health: "is the executor/provider available right now"

Formal Preference Order:
AGY > OPENCODE > CLAUDE_CODE > PI > GROK > DSH > CODEX (Hicode retired).
"""

from __future__ import annotations

import asyncio
import signal
import time
from dataclasses import dataclass
from typing import Any

from .executor_registry import (
    ExecutorRuntimeIdentity,
    get_executor_registry,
    normalize_executor_id,
)
from .models import ExecutorFailureClass, ExecutorHealth
from .worker_runtime import (
    WORKER_CAPABILITIES,
    WorkerCapabilities,
    capabilities_for,
)


def registry_order() -> tuple[str, ...]:
    """Canonical executor universe and ordering, owned by ``ExecutorRegistry``.

    A static ``DEFAULT_EXECUTOR_PREFERENCE`` tuple used to decide both ordering
    *and* which executors a health snapshot could see. The registry is now the
    sole admission authority, so the universe is projected from it: registry
    admission fixes membership, registry canonical order fixes precedence, and
    runtime capability data decides which admitted executors are routable.
    """

    registry = get_executor_registry()
    return tuple(
        executor_id for executor_id in registry.ordered_ids() if executor_id in WORKER_CAPABILITIES
    )


def normalize_executor_name(name: str) -> str:
    """Normalize via the ExecutorRegistry alias authority.

    ``EXECUTOR_ALIASES`` used to be duplicated here; the registry is the sole
    alias authority so ``agy``/``claude-code``/``opencode_go`` stay in sync.
    """

    return normalize_executor_id(name)


def classify_executor_failure(
    *,
    error: Exception | None = None,
    exit_code: int | None = None,
    status: str | None = None,
    detail: str | None = None,
    event_kind: str | None = None,
) -> ExecutorFailureClass:
    """Map exit state, lifecycle, exceptions, and structured signals to canonical failure taxonomy."""
    # 1. Cancelled checks
    if (
        isinstance(error, asyncio.CancelledError)
        or status in {"CANCELLED", "WORKER_CANCELLED"}
        or event_kind == "CANCELLED"
    ):
        return ExecutorFailureClass.WORKER_CANCELLED

    # 2. Timeout checks
    if (
        isinstance(error, TimeoutError)
        or status in {"TIMEOUT", "WORKER_TIMEOUT"}
        or event_kind in {"TIMEOUT", "WORKER_TIMEOUT_EVIDENCE"}
    ):
        return ExecutorFailureClass.WORKER_TIMEOUT

    # 3. Process signal/crash checks
    if exit_code is not None and exit_code in (
        -signal.SIGKILL,
        -9,
        137,
        -signal.SIGSEGV,
        -11,
        -signal.SIGBUS,
        -7,
    ):
        return ExecutorFailureClass.WORKER_CRASH

    detail_str = str(detail or "").lower()
    error_str = str(error or "").lower()
    combined = f"{detail_str} {error_str}"

    # 4. Process reap / zombie checks
    if any(k in combined for k in ("zombie", "process reap", "process_reap", "leftover process")):
        return ExecutorFailureClass.PROCESS_REAP_FAILURE

    # 5. Submodule checks
    if any(
        k in combined
        for k in (
            "reference is not a tree",
            "submodule failure",
            "git-submodule",
            "clone failed",
            "submodule provisioning failed",
        )
    ):
        return ExecutorFailureClass.SUBMODULE_FAILURE

    # 6. Worktree checks
    if any(
        k in combined
        for k in (
            "fatal: cannot create worktree",
            "worktree locked",
            "git worktree add",
            "fatal: worktree",
            "worktree failure",
            "worktree creation failed",
        )
    ):
        return ExecutorFailureClass.WORKTREE_FAILURE

    # 7. Transport / network / proxy checks
    if any(
        k in combined
        for k in (
            "proxy",
            "transport channel closed",
            "econnrefused",
            "connection refused",
            "connection closed",
            "websocket reconnect failed",
            "broken pipe",
            "transport_failure",
            "failed to connect",
            "tunnel",
        )
    ):
        return ExecutorFailureClass.TRANSPORT_FAILURE

    # 8. Auth checks
    if any(
        k in combined
        for k in (
            "auth_denied",
            "unauthorized",
            "401",
            "403",
            "invalid api key",
            "authentication failed",
            "forbidden",
            "bad credentials",
        )
    ):
        return ExecutorFailureClass.AUTH_FAILURE

    # 9b. Rate limiting is its own outcome. "provider unreachable" and "provider
    # says slow down" call for different operator responses, and a quota wall
    # must not be reported as an outage.
    if any(
        k in combined
        for k in ("429", "rate_limit", "rate limit", "resource_exhausted", "too many requests")
    ):
        return ExecutorFailureClass.PROVIDER_RATE_LIMIT

    # 9. Provider connectivity / outage checks
    if any(
        k in combined
        for k in (
            "provider_unavailable",
            "502",
            "503",
            "504",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
            "overloaded",
            "capacity",
            "provider down",
            "upstream connect error",
            "usage limit",
            "rate_limit",
            "rate limit",
            "quota",
            "purchase more credits",
            "insufficient balance",
        )
    ):
        return ExecutorFailureClass.PROVIDER_UNAVAILABLE

    # 9c. A structured refusal from the provider is a configuration fault, not
    # a worker fault. Real example: a provider answering HTTP 400
    # FAILED_PRECONDITION because the account's region is unsupported. The
    # process exited non-zero, so without this check it fell through to
    # WORKER_CRASH and blamed the executor for the provider's policy.
    if any(
        k in combined
        for k in (
            "failed_precondition",
            "user location is not supported",
            "not supported for the api use",
            "unsupported_region",
            "invalid_request_error",
            "400 bad request",
        )
    ) or ("400:" in combined and "api" in combined):
        return ExecutorFailureClass.PROVIDER_CONFIGURATION_FAILURE

    # 9d. The provider accepted the request and then ran out of time.
    if any(
        k in combined
        for k in (
            "provider timeout",
            "read timeout on provider",
            "provider_request_timeout",
            "timeout_kind=inactivity_timeout",
            "inactivity_timeout_ms",
        )
    ):
        return ExecutorFailureClass.PROVIDER_TIMEOUT

    # 10. Environment / binary checks
    if any(
        k in combined
        for k in (
            "command not found",
            "no such file or directory",
            "executable not found",
            "binary missing",
            "environment_failure",
        )
    ):
        return ExecutorFailureClass.ENVIRONMENT_FAILURE

    # 11. Model response checks
    if any(
        k in combined
        for k in (
            "empty model response",
            "context length",
            "rate limit",
            "model_failure",
            "failed to refresh available models",
        )
    ):
        return ExecutorFailureClass.MODEL_FAILURE

    # 12. Fallback on exit code
    if exit_code not in (0, None):
        return ExecutorFailureClass.WORKER_CRASH

    return ExecutorFailureClass.MODEL_FAILURE


@dataclass
class HealthRecord:
    state: ExecutorHealth = ExecutorHealth.UNKNOWN
    provider_reachable: bool = True
    active_process: bool = False
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_failure_class: ExecutorFailureClass | None = None
    last_failure_at: float = 0.0
    last_success_at: float = 0.0
    last_detail: str = ""


class ExecutorHealthRegistry:
    """Tracks runtime health for all executors.

    Separates process heartbeat from provider health:
    Heartbeat proves process is alive, NOT that provider is healthy.
    """

    def __init__(self, executor_registry: Any | None = None) -> None:
        self._records: dict[str, HealthRecord] = {}
        self.executor_registry = executor_registry or get_executor_registry()

    def identity(self, worker: str) -> ExecutorRuntimeIdentity:
        """Project provider/model/auth from the canonical identity authority."""

        return self.executor_registry.identity(worker)

    def _get_or_create(self, worker: str) -> HealthRecord:
        worker = normalize_executor_name(worker)
        if worker not in self._records:
            self._records[worker] = HealthRecord()
        return self._records[worker]

    def record_success(self, worker: str) -> None:
        rec = self._get_or_create(worker)
        rec.total_successes += 1
        rec.consecutive_failures = 0
        rec.last_success_at = time.time()
        rec.provider_reachable = True
        rec.state = ExecutorHealth.HEALTHY

    def record_failure(
        self,
        worker: str,
        failure_class: ExecutorFailureClass | str,
        detail: str = "",
    ) -> None:
        fc = (
            failure_class
            if isinstance(failure_class, ExecutorFailureClass)
            else ExecutorFailureClass(str(failure_class))
        )
        rec = self._get_or_create(worker)
        rec.total_failures += 1
        rec.consecutive_failures += 1
        rec.last_failure_class = fc
        rec.last_failure_at = time.time()
        rec.last_detail = detail

        if fc in {
            ExecutorFailureClass.PROVIDER_UNAVAILABLE,
            ExecutorFailureClass.AUTH_FAILURE,
            ExecutorFailureClass.TRANSPORT_FAILURE,
        }:
            rec.provider_reachable = False
            rec.state = ExecutorHealth.UNAVAILABLE
        elif rec.consecutive_failures >= 3:
            rec.state = ExecutorHealth.UNAVAILABLE
        else:
            rec.state = ExecutorHealth.DEGRADED

    def set_provider_status(
        self,
        worker: str,
        *,
        reachable: bool,
        detail: str = "",
    ) -> None:
        rec = self._get_or_create(worker)
        rec.provider_reachable = reachable
        rec.last_detail = detail
        if not reachable:
            rec.state = ExecutorHealth.UNAVAILABLE
        elif rec.consecutive_failures == 0:
            rec.state = ExecutorHealth.HEALTHY

    def set_heartbeat(self, worker: str, *, alive: bool) -> None:
        rec = self._get_or_create(worker)
        rec.active_process = alive
        # Critical rule: heartbeat alone NEVER marks an unhealthy provider as HEALTHY
        if alive and not rec.provider_reachable:
            rec.state = ExecutorHealth.UNAVAILABLE

    def get_health(self, worker: str, *, allow_unknown: bool = True) -> ExecutorHealth:
        rec = self._get_or_create(worker)
        # Invariant: If provider is dead, health can NEVER be HEALTHY, even if process is alive
        if not rec.provider_reachable:
            return ExecutorHealth.UNAVAILABLE
        if rec.state == ExecutorHealth.UNKNOWN:
            return ExecutorHealth.HEALTHY if allow_unknown else ExecutorHealth.UNKNOWN
        return rec.state

    def snapshot(self) -> dict[str, str]:
        return {w: str(self.get_health(w)) for w in registry_order()}


@dataclass(frozen=True)
class SubstitutionEvidence:
    """Audit evidence trail for executor routing substitution."""

    requested_executor: str
    selected_executor: str
    substitution_reason: str
    health_snapshot: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "executor_substitution",
            "requested_executor": self.requested_executor,
            "selected_executor": self.selected_executor,
            "substitution_reason": self.substitution_reason,
            "health_snapshot": dict(self.health_snapshot),
        }


def check_capability_compatible(
    worker: str,
    required: WorkerCapabilities | dict[str, Any] | None,
) -> bool:
    """Check if worker satisfies required capability contract."""
    if required is None:
        return True
    req_dict = required.to_dict() if isinstance(required, WorkerCapabilities) else dict(required)
    worker_caps = capabilities_for(worker).to_dict()
    for cap_key, req_val in req_dict.items():
        if isinstance(req_val, bool) and req_val:
            if not worker_caps.get(cap_key, False):
                return False
        elif req_val and req_val != "NONE" and worker_caps.get(cap_key) != req_val:
            return False
    return True


def resolve_executor(
    *,
    requested: str | None = None,
    explicit_pin: bool = False,
    required_capabilities: WorkerCapabilities | dict[str, Any] | None = None,
    health_registry: ExecutorHealthRegistry | None = None,
    preference_order: tuple[str, ...] | None = None,
    fail_closed_unknown: bool = False,
) -> tuple[str, SubstitutionEvidence | None]:
    """Resolve an executor using capability eligibility, explicit pinning, health, and preference.

        Order of operations (P4):
        1. Capability eligibility (fail-closed if missing)
        2. Explicit pin (if specified) -> never substituted
        3. Current health (fail-closed if all candidates are UNAVAILABLE or unverified)
    4. Preference (ExecutorRegistry canonical order)
          Retired executors (hicode) are rejected before preference applies.
    """
    order = registry_order() if preference_order is None else tuple(preference_order)
    req_norm = normalize_executor_name(requested or "") if requested else None

    # Admission is registry-owned: who exists. Runtime capability data answers a
    # different question (what this executor can do), so it must never gate
    # admission. An admitted executor with no capability record is NOT_READY,
    # which must fail closed rather than silently substituting another executor.
    registry = get_executor_registry()
    if req_norm and req_norm not in registry.ordered_ids():
        raise ValueError(f"Unknown executor: {requested!r}")
    if req_norm and req_norm not in WORKER_CAPABILITIES:
        raise ValueError(f"Executor not ready: {requested!r} has no runtime capability record")

    # Explicit pin authority: Never substitute an explicitly pinned executor
    if explicit_pin and req_norm:
        if not check_capability_compatible(req_norm, required_capabilities):
            raise ValueError(
                f"Pinned executor {req_norm} is incompatible with required capabilities"
            )
        return req_norm, None

    # Filter candidate pool by capability compatibility (fail-closed on capability missing)
    capable_candidates = [w for w in order if check_capability_compatible(w, required_capabilities)]
    if not capable_candidates:
        raise ValueError("No capable executor available for required capabilities")

    health_reg = health_registry or ExecutorHealthRegistry()
    snapshot = health_reg.snapshot()

    # Determine desired candidate
    target = req_norm if req_norm in capable_candidates else capable_candidates[0]
    target_health = health_reg.get_health(target, allow_unknown=not fail_closed_unknown)

    if target_health == ExecutorHealth.HEALTHY or (
        target_health == ExecutorHealth.UNKNOWN and not fail_closed_unknown
    ):
        return target, None

    # Target is unhealthy (DEGRADED, UNAVAILABLE, or unverified UNKNOWN in fail-closed mode)
    fallback_candidate = None
    for cand in capable_candidates:
        cand_health = health_reg.get_health(cand, allow_unknown=not fail_closed_unknown)
        if cand != target and (
            cand_health == ExecutorHealth.HEALTHY
            or (cand_health == ExecutorHealth.UNKNOWN and not fail_closed_unknown)
        ):
            fallback_candidate = cand
            break

    if fallback_candidate is None:
        if target_health == ExecutorHealth.UNAVAILABLE or (
            target_health == ExecutorHealth.UNKNOWN and fail_closed_unknown
        ):
            raise ValueError(
                f"No available executor: all capable candidates {capable_candidates} are UNAVAILABLE or unverified"
            )
        # All capable candidates are degraded but not UNAVAILABLE; use first capable
        fallback_candidate = capable_candidates[0]

    evidence = SubstitutionEvidence(
        requested_executor=target,
        selected_executor=fallback_candidate,
        substitution_reason=f"{target} is {target_health}",
        health_snapshot=snapshot,
    )
    return fallback_candidate, evidence


__all__ = [
    "ExecutorFailureClass",
    "ExecutorHealth",
    "ExecutorHealthRegistry",
    "HealthRecord",
    "SubstitutionEvidence",
    "check_capability_compatible",
    "classify_executor_failure",
    "normalize_executor_name",
    "resolve_executor",
]
