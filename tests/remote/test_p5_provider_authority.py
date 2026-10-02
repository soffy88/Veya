"""ProviderRegistry authority and executor/provider decoupling (spec P5.1, P5.2).

P5.2 is the part that decays. It is easy to leave a provider field on the
executor record and pass every behavioural test, because the field is never read
on the happy path. So the decoupling tests below assert the *absence* of
provider runtime state on the executor plane, not just its presence on the
provider plane.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from veya.remote.executor_registry import ExecutorRuntimeIdentity, get_executor_registry
from veya.remote.provider_registry import (
    ProviderAuthState,
    ProviderAvailability,
    ProviderQuotaState,
    ProviderRecord,
    ProviderRegistry,
    get_provider_registry,
    normalize_provider_name,
    reset_provider_registry,
)

CREDENTIAL_TOKENS = ("key", "token", "secret", "password", "credential", "passphrase")


@pytest.fixture
def registry() -> ProviderRegistry:
    reset_provider_registry()
    try:
        yield get_provider_registry()
    finally:
        reset_provider_registry()


# ── P5.1 the registry is the single authority ────────────────────────


def test_registry_admits_the_providers_the_executors_name(registry: ProviderRegistry) -> None:
    """Every provider an executor references must exist here.

    A dangling provider name is how an executor ends up holding its own private
    copy of provider state: there is no record to ask, so it invents one.
    """

    known = set(registry.snapshot())
    for identity in get_executor_registry().snapshot().values():
        if identity.provider:
            assert normalize_provider_name(identity.provider) in known, identity.provider


def test_snapshot_is_the_only_inventory(registry: ProviderRegistry) -> None:
    assert registry.snapshot(), "provider registry is empty"
    assert set(registry.ordered_ids()) == set(registry.snapshot())


def test_ordered_ids_is_canonical_not_seed_order(registry: ProviderRegistry) -> None:
    ordered = registry.ordered_ids()
    assert ordered[0] == "opencode"
    assert len(set(ordered)) == len(ordered), "an alias admitted the same provider twice"


def test_identity_refuses_unknown_without_admitting_it(registry: ProviderRegistry) -> None:
    before = set(registry.snapshot())
    with pytest.raises(ValueError, match="unknown provider"):
        registry.identity("nope")
    assert set(registry.snapshot()) == before, "reading a provider widened admission"


def test_register_is_the_only_writer(registry: ProviderRegistry) -> None:
    record = ProviderRecord(name="probe", provider_type="test")
    registry.register(record)
    assert registry.identity("probe") is record


# ── P5.1 no credential may ever be stored ────────────────────────────


def test_record_declares_no_credential_field() -> None:
    for spec in ProviderRecord.__dataclass_fields__.values():
        assert not any(t in spec.name.lower() for t in CREDENTIAL_TOKENS), spec.name


def test_record_refuses_a_declared_credential_field() -> None:
    """The guard must survive someone adding the field later."""

    @dataclass(frozen=True)
    class Leaky(ProviderRecord):
        api_key: str = "sk-should-never-exist"

    with pytest.raises(ValueError, match="credential"):
        Leaky(name="leaky")


def test_projection_is_an_allowlist(registry: ProviderRegistry) -> None:
    for record in registry.snapshot().values():
        payload = record.to_dict()
        assert set(payload) == {
            "name",
            "provider_type",
            "models",
            "capabilities",
            "health_state",
            "availability",
            "auth_state",
            "quota_state",
            "failure_state",
        }
        for key, value in payload.items():
            assert not any(t in key.lower() for t in CREDENTIAL_TOKENS), key
            assert "sk-" not in str(value)


# ── P5.1 the record answers reachability, auth and quota separately ──


def test_auth_state_is_a_state_not_a_credential() -> None:
    for state in ProviderAuthState:
        assert state.value in {"UNKNOWN", "AVAILABLE", "INVALID", "EXPIRED"}


def test_usable_requires_reachability_auth_and_quota() -> None:
    """All three, not any one.

    A reachable provider that cannot authenticate fails every request, and an
    authenticated provider out of quota also fails every request. An OR here
    would report a provider usable while it refuses all traffic.
    """

    base = dict(name="p", provider_type="first_party", capabilities=frozenset({"text"}))
    healthy = ProviderRecord(
        **base,
        availability=str(ProviderAvailability.AVAILABLE),
        auth_state=str(ProviderAuthState.AVAILABLE),
        quota_state=str(ProviderQuotaState.OK),
    )
    assert healthy.usable
    for broken in (
        ProviderRecord(
            **base,
            availability=str(ProviderAvailability.AVAILABLE),
            auth_state=str(ProviderAuthState.INVALID),
            quota_state=str(ProviderQuotaState.OK),
        ),
        ProviderRecord(
            **base,
            availability=str(ProviderAvailability.AVAILABLE),
            auth_state=str(ProviderAuthState.EXPIRED),
            quota_state=str(ProviderQuotaState.OK),
        ),
        ProviderRecord(
            **base,
            availability=str(ProviderAvailability.AVAILABLE),
            auth_state=str(ProviderAuthState.AVAILABLE),
            quota_state=str(ProviderQuotaState.EXHAUSTED),
        ),
        ProviderRecord(
            **base,
            availability=str(ProviderAvailability.UNAVAILABLE),
            auth_state=str(ProviderAuthState.AVAILABLE),
            quota_state=str(ProviderQuotaState.OK),
        ),
    ):
        assert not broken.usable, broken


def test_unobserved_provider_is_not_claimed_usable(registry: ProviderRegistry) -> None:
    """Seeded providers default to UNKNOWN auth, so none may claim usable.

    Claiming usable before anyone authenticated would hide the very state this
    registry exists to expose.
    """

    assert not any(record.usable for record in registry.snapshot().values())


def test_observation_merges_without_dropping_other_axes() -> None:
    registry = ProviderRegistry.from_mapping({"p": {"capabilities": ["text"]}})
    registry.record_observation("p", auth_state=str(ProviderAuthState.AVAILABLE))
    after = registry.identity("p")
    assert after.authed
    assert after.capabilities == frozenset({"text"}), "an auth update must not reset capabilities"


# ── P5.2 the executor asks for a capability, not for provider state ──


def test_executor_plane_holds_no_provider_runtime_state() -> None:
    """The decoupling, asserted as absence.

    `provider` as a *name* is a reference and is allowed; a provider's health,
    quota or auth as executor-owned state is the bug this spec forbids.
    """

    for forbidden in ("provider_health", "provider_quota", "provider_auth", "provider_failure"):
        assert not hasattr(ExecutorRuntimeIdentity, forbidden), forbidden
        assert forbidden not in ExecutorRuntimeIdentity.__dataclass_fields__


def test_request_returns_only_capable_providers(registry: ProviderRegistry) -> None:
    for record in registry.request("text"):
        assert record.supports("text")
    assert all(
        "text" not in [c.lower() for c in r.capabilities] for r in registry.request("nonexistent")
    )


def test_request_is_the_provider_facing_surface(registry: ProviderRegistry) -> None:
    """An executor reaches a provider through request(), never field-by-field.

    Asserting the absence of a lookup-by-provider-health keeps a future caller
    from adding a second path that bypasses the registry.
    """

    assert callable(registry.request)
    for forbidden in ("provider_health", "provider_quota", "health_of", "quota_of"):
        assert not hasattr(registry, forbidden), forbidden


def test_alias_collapse_keeps_one_record_per_provider(registry: ProviderRegistry) -> None:
    for alias in ("cliproxy", "cliproxy-google", "local_cliproxy", "Local-CLIPROXY-Google"):
        assert normalize_provider_name(alias) == "local-cliproxy-google"


# ── P5.2 the executor declares a requirement, not provider state ──────


def test_executor_declares_what_it_needs_from_a_provider() -> None:
    """The requirement is a capability ask, and it is never an observation.

    An empty requirement means "not recorded", not "anything acceptable":
    acp declares nothing and must therefore be handed no provider at all,
    rather than a full list that implies it could use any of them.
    """

    executors = get_executor_registry()
    for executor_id in ("pi", "opencode", "codex"):
        identity = executors.identity(executor_id)
        assert identity.provider_capabilities, executor_id
        assert identity.provider_request(), executor_id
    assert executors.identity("acp").provider_capabilities == frozenset()
    assert executors.identity("acp").provider_request() == ()


def test_request_is_deduplicated_in_canonical_order() -> None:
    """stream and text both match most providers; an executor gets each once."""

    identity = get_executor_registry().identity("pi")
    names = [record.name for record in identity.provider_request()]
    assert len(names) == len(set(names)), names
    registry = get_provider_registry()
    assert names == [n for n in registry.ordered_ids() if n in set(names)]


def test_executor_reaches_provider_state_only_through_the_registry() -> None:
    """Reading the provider record must not return an executor-local copy."""

    identity = get_executor_registry().identity("pi")
    assert identity.provider_record() is get_provider_registry().identity(identity.provider)
    for forbidden in ("provider_health", "provider_quota", "provider_auth_state"):
        assert not hasattr(identity, forbidden), forbidden


def test_naming_an_unregistered_provider_fails_closed() -> None:
    """A dangling provider name must raise, not fall back to private state."""

    identity = ExecutorRuntimeIdentity(
        executor_id="probe",
        executor_kind="l1_worker",
        provider="no-such-provider",
        model=None,
        auth_state="AUTHENTICATED",
        reachable=True,
        launcher=None,
    )
    with pytest.raises(ValueError, match="unknown provider"):
        identity.provider_record()


def test_executor_without_a_provider_cannot_ask_for_one() -> None:
    identity = ExecutorRuntimeIdentity(
        executor_id="probe",
        executor_kind="l1_worker",
        provider=None,
        model=None,
        auth_state="AUTHENTICATED",
        reachable=True,
        launcher=None,
    )
    with pytest.raises(ValueError, match="names no provider"):
        identity.provider_record()


# ── P5.2 a provider fault must not degrade the executor ──────────────


def test_provider_fault_is_charged_to_the_provider_not_the_executor() -> None:
    from veya.remote.provider_registry import ProviderHealthState
    from veya.supervision.runner import _record_failure_against_the_responsible_layer

    reset_provider_registry()
    try:
        providers = get_provider_registry()
        _record_failure_against_the_responsible_layer("pi", "PROVIDER_RATE_LIMIT")
        record = providers.identity("local-cliproxy-google")
        assert record.health_state == str(ProviderHealthState.UNHEALTHY)
        assert record.failure_state == "PROVIDER_RATE_LIMIT"
    finally:
        reset_provider_registry()


def test_executor_fault_is_still_charged_to_the_executor() -> None:
    from veya.supervision.runner import _record_failure_against_the_responsible_layer

    reset_provider_registry()
    try:
        providers = get_provider_registry()
        before = providers.identity("local-cliproxy-google").health_state
        _record_failure_against_the_responsible_layer("pi", "WORKER_CRASH")
        assert providers.identity("local-cliproxy-google").health_state == before
    finally:
        reset_provider_registry()


def test_provider_fault_leaves_executor_health_clean_for_failover() -> None:
    """The regression this fixes: a provider wall must not exclude the
    executor that could have recovered the mission on another attempt."""

    from veya.remote.executor_health import ExecutorHealthRegistry
    from veya.supervision.runner import _record_failure_against_the_responsible_layer

    reset_provider_registry()
    try:
        _record_failure_against_the_responsible_layer("pi", "PROVIDER_CONFIGURATION_FAILURE")
        assert ExecutorHealthRegistry().get_health("pi") not in ("UNAVAILABLE", "UNHEALTHY")
    finally:
        reset_provider_registry()
