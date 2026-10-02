"""Failure namespace split: executor vs provider (spec P5.3).

The split is only worth anything if it holds at every boundary, so these tests
cover the enum, the attribution, the classifier, and the receipt shape. A receipt
that still carries a bare `failure_class` for a provider fault would pass every
taxonomy test while leaving the ambiguity in place for whoever reads it.
"""

from __future__ import annotations

import pytest

from veya.remote.executor_health import (
    classify_executor_failure,
    classify_failure,
)
from veya.remote.models import (
    ExecutorFailureClass,
    FailureAttribution,
    ProviderFailureClass,
)

# ── the two namespaces are actually separate ─────────────────────────


def test_provider_codes_are_not_executor_codes() -> None:
    for member in ProviderFailureClass:
        assert member.name.startswith("PROVIDER_")
        assert not hasattr(ExecutorFailureClass, member.name), member.name
        with pytest.raises(ValueError):
            ExecutorFailureClass(member.value)


def test_executor_namespace_keeps_only_executor_faults() -> None:
    """No PROVIDER_* may creep back into the executor enum."""

    assert not [m.name for m in ExecutorFailureClass if m.name.startswith("PROVIDER_")]


def test_spec_required_provider_members_exist() -> None:
    required = {
        "PROVIDER_TIMEOUT",
        "PROVIDER_RATE_LIMIT",
        "PROVIDER_AUTH_FAILURE",
        "PROVIDER_CONFIGURATION_FAILURE",
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_MODEL_UNAVAILABLE",
    }
    assert required <= {m.name for m in ProviderFailureClass}


# ── attribution names exactly one layer ──────────────────────────────


def test_attribution_rejects_a_diagnosis_that_names_both_layers() -> None:
    with pytest.raises(ValueError, match="exactly one layer"):
        FailureAttribution(
            executor_failure_class=ExecutorFailureClass.EXECUTOR_CRASH,
            provider_failure_class=ProviderFailureClass.PROVIDER_RATE_LIMIT,
        )


def test_attribution_rejects_a_diagnosis_that_names_neither() -> None:
    with pytest.raises(ValueError, match="exactly one layer"):
        FailureAttribution()


@pytest.mark.parametrize(
    "detail,expected",
    [
        (
            '400 {"status":"FAILED_PRECONDITION","message":"User location is not supported"}',
            ProviderFailureClass.PROVIDER_CONFIGURATION_FAILURE,
        ),
        ("429 rate limit exceeded", ProviderFailureClass.PROVIDER_RATE_LIMIT),
        ("503 upstream connect error", ProviderFailureClass.PROVIDER_UNAVAILABLE),
        ("timeout_kind=INACTIVITY_TIMEOUT", ProviderFailureClass.PROVIDER_TIMEOUT),
        ("invalid_api_key", ProviderFailureClass.PROVIDER_AUTH_FAILURE),
        ("segfault", ExecutorFailureClass.WORKER_CRASH),
        ("command not found", ExecutorFailureClass.ENVIRONMENT_FAILURE),
    ],
)
def test_classifier_attributes_to_one_layer(detail, expected) -> None:
    attribution = classify_failure(exit_code=1, detail=detail)
    if isinstance(expected, ProviderFailureClass):
        assert attribution.provider_failure_class is expected
        assert attribution.executor_failure_class is None
    else:
        assert attribution.executor_failure_class is expected
        assert attribution.provider_failure_class is None


def test_executor_projection_refuses_to_guess_for_provider_faults() -> None:
    """A caller asking only about the executor must get "not the executor".

    Returning the provider code here is what let a quota wall be charged to
    executor health in the first place.
    """

    assert classify_executor_failure(exit_code=1, detail="429 rate limit") is None
    assert (
        classify_executor_failure(exit_code=1, detail="segfault")
        is ExecutorFailureClass.WORKER_CRASH
    )


# ── receipt shape: both fields named, one null ───────────────────────


def test_receipt_projection_names_both_fields() -> None:
    provider = FailureAttribution.provider(ProviderFailureClass.PROVIDER_RATE_LIMIT).to_dict()
    assert provider == {
        "executor_failure_class": None,
        "provider_failure_class": "PROVIDER_RATE_LIMIT",
    }

    executor = FailureAttribution.executor(ExecutorFailureClass.EXECUTOR_CRASH).to_dict()
    assert executor == {"executor_failure_class": "EXECUTOR_CRASH", "provider_failure_class": None}


def test_execution_record_has_separate_receipt_fields() -> None:
    """The record must not collapse the split back into one field."""

    from veya.remote.execution import ExecutionRecord

    fields = ExecutionRecord.__dataclass_fields__
    assert "executor_failure_class" in fields
    assert "provider_failure_class" in fields
    assert "failure_class" in fields, "the single log code is retained"


# ── retryability differs by layer ───────────────────────────────────


def test_retryability_separates_transient_from_permanent() -> None:
    assert ProviderFailureClass.PROVIDER_RATE_LIMIT.retryable
    assert ProviderFailureClass.PROVIDER_TIMEOUT.retryable
    # retrying a refused credential or an unsupported region only burns quota
    assert not ProviderFailureClass.PROVIDER_AUTH_FAILURE.retryable
    assert not ProviderFailureClass.PROVIDER_CONFIGURATION_FAILURE.retryable
