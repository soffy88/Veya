"""ProviderRegistry: the single authority for provider state (spec P5.1).

An executor answers "can I run this operation". A provider answers "will it be
served, and may I authenticate to it". Keeping those questions in one record
invites a class of bug where a provider quota wall is reported as a broken
executor, or a dead endpoint is reported as a capability mismatch.

So the split is structural rather than advisory. Provider state lives here, in
``ProviderRecord``, and the executor plane holds only a provider *name* to
request against.

Credentials are never a field. ``ProviderRecord`` refuses them at construction
and ``to_dict()`` projects an explicit allowlist, so an auth failure can be
described ("this provider's credential is expired") without the record ever
holding the credential itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any

__all__ = [
    "ProviderAuthState",
    "ProviderAvailability",
    "ProviderHealthState",
    "ProviderQuotaState",
    "ProviderRecord",
    "ProviderRegistry",
    "get_provider_registry",
    "normalize_provider_name",
    "reset_provider_registry",
]


class ProviderAvailability(StrEnum):
    """Whether the provider is servable right now."""

    UNKNOWN = "UNKNOWN"
    REGISTERED = "REGISTERED"
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class ProviderAuthState(StrEnum):
    """Outcome of the most recent authentication attempt.

    ``AVAILABLE`` means a credential was presented and accepted. It deliberately
    says nothing about *which* credential, because the record never holds one.
    """

    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    INVALID = "INVALID"
    EXPIRED = "EXPIRED"


class ProviderQuotaState(StrEnum):
    """Whether the account may still spend."""

    UNKNOWN = "UNKNOWN"
    OK = "OK"
    LIMITED = "LIMITED"
    EXHAUSTED = "EXHAUSTED"


class ProviderHealthState(StrEnum):
    """Observed reachability of the provider endpoint."""

    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


_CREDENTIAL_TOKENS = ("key", "token", "secret", "password", "credential", "passphrase")


def normalize_provider_name(value: str) -> str:
    """Canonical provider key. Aliases collapse so one provider is one record."""

    name = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "cliproxy": "local-cliproxy-google",
        "cliproxy-google": "local-cliproxy-google",
        "local-cliproxy": "local-cliproxy-google",
        "google": "local-cliproxy-google",
        "anthropic": "anthropic",
        "openai": "openai",
        "opencode": "opencode",
        "gemini": "gemini",
        "xai": "xai",
        "dsh": "dsh",
        "acp": "acp",
    }
    return aliases.get(name, name)


@dataclass(frozen=True)
class ProviderRecord:
    """Everything known about one provider. Never holds a credential."""

    name: str
    provider_type: str = "unknown"
    models: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    health_state: str = str(ProviderHealthState.UNKNOWN)
    availability: str = str(ProviderAvailability.REGISTERED)
    auth_state: str = str(ProviderAuthState.UNKNOWN)
    quota_state: str = str(ProviderQuotaState.UNKNOWN)
    failure_state: str | None = None

    def __post_init__(self) -> None:
        for spec in fields(self):
            if any(token in spec.name.lower() for token in _CREDENTIAL_TOKENS):
                raise ValueError(
                    f"ProviderRecord must not hold credential material: {spec.name!r}. "
                    "Record auth_state instead; the credential itself never belongs here."
                )

    @property
    def authed(self) -> bool:
        return self.auth_state == str(ProviderAuthState.AVAILABLE)

    @property
    def has_quota(self) -> bool:
        return self.quota_state in (str(ProviderQuotaState.OK), str(ProviderQuotaState.LIMITED))

    @property
    def usable(self) -> bool:
        """A provider is usable when it can be reached, authenticated, and paid for.

        All three are required. A reachable provider that cannot authenticate
        will fail every request, and an authenticated provider out of quota will
        fail every request; either way the executor's own health is irrelevant.
        """

        return (
            self.availability == str(ProviderAvailability.AVAILABLE)
            and self.authed
            and self.has_quota
        )

    def supports(self, capability: str) -> bool:
        return str(capability).lower() in {c.lower() for c in self.capabilities}

    def to_dict(self) -> dict[str, Any]:
        """Projection for receipts and diagnostics. Allowlist, not a dump."""

        return {
            "name": self.name,
            "provider_type": self.provider_type,
            "models": sorted(self.models),
            "capabilities": sorted(self.capabilities),
            "health_state": self.health_state,
            "availability": self.availability,
            "auth_state": self.auth_state,
            "quota_state": self.quota_state,
            "failure_state": self.failure_state,
        }


# Seed facts. Model names and capabilities are what the provider is *asked* for,
# not what a discovery probe happened to observe on one machine.
_SEED: dict[str, dict[str, Any]] = {
    "opencode": {
        "provider_type": "gateway",
        "models": frozenset({"deepseek-v4.1-flash"}),
        "capabilities": frozenset({"text", "stream"}),
    },
    "anthropic": {
        "provider_type": "first_party",
        "models": frozenset(),
        "capabilities": frozenset({"text", "stream", "tool_use"}),
    },
    "openai": {
        "provider_type": "first_party",
        "models": frozenset(),
        "capabilities": frozenset({"text", "stream", "tool_use"}),
    },
    "gemini": {
        "provider_type": "first_party",
        "models": frozenset(),
        "capabilities": frozenset({"text", "stream", "tool_use"}),
    },
    "xai": {
        "provider_type": "first_party",
        "models": frozenset(),
        "capabilities": frozenset({"text", "stream"}),
    },
    "local-cliproxy-google": {
        "provider_type": "local_proxy",
        "models": frozenset(),
        "capabilities": frozenset({"text", "stream"}),
    },
    "dsh": {
        "provider_type": "local",
        "models": frozenset(),
        "capabilities": frozenset({"text"}),
    },
    "acp": {
        "provider_type": "protocol",
        "models": frozenset(),
        "capabilities": frozenset(),
    },
}

_CANONICAL_ORDER: tuple[str, ...] = (
    "opencode",
    "anthropic",
    "openai",
    "gemini",
    "local-cliproxy-google",
    "xai",
    "dsh",
    "acp",
)


@dataclass
class ProviderRegistry:
    """Admission lookup for providers. ``register()`` is the only writer."""

    _records: dict[str, ProviderRecord] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self._records:
            for name, seed in _SEED.items():
                self._records[name] = ProviderRecord(
                    name=name,
                    provider_type=seed["provider_type"],
                    models=seed["models"],
                    capabilities=seed["capabilities"],
                    health_state=str(ProviderHealthState.UNKNOWN),
                    availability=str(ProviderAvailability.AVAILABLE),
                    auth_state=str(ProviderAuthState.UNKNOWN),
                    quota_state=str(ProviderQuotaState.UNKNOWN),
                )

    def identity(self, name: str) -> ProviderRecord:
        """Admitted-provider lookup. Read-only; never a discovery hook.

        An unknown name raises rather than being discovered into ``_records``,
        so reading a provider cannot widen what ``snapshot()`` reports.
        """

        key = normalize_provider_name(name)
        record = self._records.get(key)
        if record is None:
            raise ValueError(f"unknown provider: {key!r} is not registered")
        return record

    def register(self, record: ProviderRecord) -> None:
        self._records[normalize_provider_name(record.name)] = record

    def snapshot(self) -> dict[str, ProviderRecord]:
        return dict(self._records)

    def ordered_ids(self) -> tuple[str, ...]:
        """Canonical provider order. Seed sequence is not preference."""

        admitted = self.snapshot()
        ordered = [x for x in _CANONICAL_ORDER if x in admitted]
        ordered.extend(x for x in admitted if x not in _CANONICAL_ORDER)
        return tuple(ordered)

    def request(self, capability: str) -> tuple[ProviderRecord, ...]:
        """Providers satisfying a capability requirement, in canonical order.

        This is the whole provider-facing surface an executor needs. It asks for
        a capability and gets records back; it never reads provider runtime
        state directly, so an executor cannot form a private opinion about
        provider health that the registry would contradict.
        """

        return tuple(
            self._records[name]
            for name in self.ordered_ids()
            if name in self._records and self._records[name].supports(capability)
        )

    def record_observation(
        self,
        name: str,
        *,
        health_state: str | None = None,
        availability: str | None = None,
        auth_state: str | None = None,
        quota_state: str | None = None,
        failure_state: str | None = None,
    ) -> ProviderRecord:
        """Merge an observation into an admitted provider's record.

        Observers report what they saw; this is the one place a provider record
        changes, so a stale value cannot be written from two competing call
        sites.
        """

        current = self.identity(name)
        merged = ProviderRecord(
            name=current.name,
            provider_type=current.provider_type,
            models=current.models,
            capabilities=current.capabilities,
            health_state=health_state if health_state is not None else current.health_state,
            availability=availability if availability is not None else current.availability,
            auth_state=auth_state if auth_state is not None else current.auth_state,
            quota_state=quota_state if quota_state is not None else current.quota_state,
            failure_state=failure_state if failure_state is not None else current.failure_state,
        )
        self.register(merged)
        return merged

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Mapping[str, Any]]) -> ProviderRegistry:
        """Build an explicit registry. Used by tests and offline callers."""

        registry = cls(_records={})
        for name, value in payload.items():
            registry.register(
                ProviderRecord(
                    name=name,
                    provider_type=str(value.get("provider_type", "unknown")),
                    models=frozenset(value.get("models", ())),
                    capabilities=frozenset(value.get("capabilities", ())),
                    health_state=str(value.get("health_state", ProviderHealthState.UNKNOWN)),
                    availability=str(value.get("availability", ProviderAvailability.AVAILABLE)),
                    auth_state=str(value.get("auth_state", ProviderAuthState.UNKNOWN)),
                    quota_state=str(value.get("quota_state", ProviderQuotaState.UNKNOWN)),
                    failure_state=value.get("failure_state"),
                )
            )
        return registry


_DEFAULT_REGISTRY: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ProviderRegistry()
    return _DEFAULT_REGISTRY


def reset_provider_registry() -> None:
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None
