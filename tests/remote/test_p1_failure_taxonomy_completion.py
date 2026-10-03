"""P1 failure-taxonomy completion: provider evidence keeps its owning layer.

Regression for the defect where a provider refusal (e.g. 429) travelled
``provider -> classify_executor_failure() -> None -> record_failure``,
raised ``ValueError`` and was washed into a generic ``PROVIDER_ERROR``,
losing the root cause.
"""

from __future__ import annotations

import pytest

from veya.remote.executor_health import (
    ExecutorHealthRegistry,
    classify_failure,
)
from veya.remote.models import ProviderFailureClass


def test_provider_429_maps_to_rate_limit_not_none() -> None:
    detail = (
        "exit_code=1; stderr=; stdout=" + '{"type":"result","api_error_status":429,'
        '"result":"API Error: Request rejected (429) usage_limit_reached"}'
    )
    attribution = classify_failure(exit_code=1, detail=detail)
    assert attribution.is_provider_fault
    assert attribution.provider_failure_class == ProviderFailureClass.PROVIDER_RATE_LIMIT
    assert attribution.executor_failure_class is None


def test_provider_400_configuration_maps_to_configuration_failure() -> None:
    attribution = classify_failure(
        exit_code=1,
        detail="exit_code=1; stderr=400: User location is not supported for the API use",
    )
    assert attribution.is_provider_fault
    assert (
        attribution.provider_failure_class
        == ProviderFailureClass.PROVIDER_CONFIGURATION_FAILURE
    )


def test_provider_unreachable_maps_to_unavailable() -> None:
    attribution = classify_failure(exit_code=1, detail="503 service unavailable upstream")
    assert attribution.is_provider_fault
    assert (
        attribution.provider_failure_class == ProviderFailureClass.PROVIDER_UNAVAILABLE
    )


def test_worker_segfault_stays_executor_crash() -> None:
    attribution = classify_failure(exit_code=-11, detail="segfault")
    assert not attribution.is_provider_fault
    assert attribution.executor_failure_class is not None


def test_record_failure_refuses_none_loudly() -> None:
    registry = ExecutorHealthRegistry()
    with pytest.raises(ValueError, match="classify_failure"):
        registry.record_failure("codex", None)  # type: ignore[arg-type]


def test_failure_class_of_sees_provider_faults() -> None:
    from types import SimpleNamespace

    from veya.supervision.runner import _failure_class_of

    result = SimpleNamespace(
        block_reason="API Error: Request rejected (429) usage_limit_reached",
        final_summary="",
    )
    assert _failure_class_of(result) == "PROVIDER_RATE_LIMIT"
