from __future__ import annotations

import pytest


def test_resource_exhausted_is_terminal_and_parses_reset_metadata() -> None:
    from server.hicode_cooldown import classify_upstream_failure

    failure = classify_upstream_failure(
        {
            "code": "model_cooldown",
            "reset_seconds": 7200,
            "provider": "cliproxy-google",
            "model": "gemini-pro-agent",
            "last_upstream_error": "429 RESOURCE_EXHAUSTED: Resets in 2h",
        }
    )

    assert failure is not None
    assert failure.failure_class == "UPSTREAM_QUOTA_EXHAUSTED"
    assert failure.retryable_immediately is False
    assert failure.upstream_reset_seconds == 7200
    assert failure.provider == "cliproxy-google"
    assert failure.model == "gemini-pro-agent"
    assert "RESOURCE_EXHAUSTED" in failure.upstream_evidence


def test_effective_cooldown_never_precedes_upstream_reset() -> None:
    from server.hicode_cooldown import effective_cooldown_until

    assert effective_cooldown_until(1000.0, 7200, now=1000.0) == 8200.0


def test_hicode_result_projects_quota_as_terminal_cooldown() -> None:
    from server.hicode_agent import _hicode_result_error

    error = _hicode_result_error(
        {
            "type": "result",
            "is_error": True,
            "error": {
                "code": "model_cooldown",
                "reset_seconds": 7200,
                "provider": "cliproxy-google",
                "model": "gemini-pro-agent",
                "last_upstream_error": "429 RESOURCE_EXHAUSTED",
            },
        },
        raw_events=[],
        stderr_tail="",
        exit_code=1,
    )

    assert error is not None
    assert error.failure_class == "UPSTREAM_QUOTA_EXHAUSTED"
    assert error.retryable_immediately is False
    assert error.retry_not_before is not None


@pytest.mark.asyncio
async def test_single_credential_does_not_retry_quota_failure() -> None:
    from runtime.provider_reliability import ReliableProviderAdapter

    adapter = ReliableProviderAdapter()
    calls: list[str] = []

    async def request(name: str):
        calls.append(name)
        raise RuntimeError("429 RESOURCE_EXHAUSTED; reset_seconds=7200")

    with pytest.raises(Exception) as caught:
        await adapter.call(request, ["cliproxy-google"], goal_run_id="g", context={})

    assert calls == ["cliproxy-google"]
    assert getattr(caught.value, "failure_class", None) == "UPSTREAM_QUOTA_EXHAUSTED"


@pytest.mark.asyncio
async def test_503_remains_bounded_transient_retry() -> None:
    from runtime.provider_reliability import ReliableProviderAdapter

    adapter = ReliableProviderAdapter()
    calls: list[str] = []

    async def request(name: str):
        calls.append(name)
        if len(calls) < 2:
            raise RuntimeError("503 server error")
        return {"ok": True}

    name, result, _ = await adapter.call(request, ["primary"], goal_run_id="g", context={})
    assert (name, result) == ("primary", {"ok": True})
    assert calls == ["primary", "primary"]


def test_hicode_mapping_authority_is_unique_and_explicit() -> None:
    from server.hicode_cooldown import load_hicode_mapping_authority

    authority = load_hicode_mapping_authority()
    assert len(authority) == 1
    mapping = authority["gemini-pro-agent"]
    assert mapping["internal_provider"] == "antigravity"
    assert mapping["upstream_model"] == "gemini-pro-default"
    assert mapping["active"] is True
    assert mapping["source"]
    assert mapping["updated_at"]
