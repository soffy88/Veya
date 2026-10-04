"""Quota cooldown classification, as production actually reads it.

This file was ``test_hicode_429_cooldown.py`` and imported
``server.hicode_cooldown``. That module moved to ``veya.provider_cooldown`` when
the Hicode executor was retired, but ``runtime/provider_reliability.py`` still
calls ``classify_upstream_failure`` on every provider failure — so the logic was
live the whole time and only the test's import path was stale, which is why these
two cases were failing rather than testing anything that had gone away.

Two cases were removed rather than repointed, and the difference matters:

* ``test_hicode_result_projects_quota_as_terminal_cooldown`` covered
  ``hicode_agent._hicode_result_error``, deleted with the executor. Its assertion
  — a quota failure surfaces as ``UPSTREAM_QUOTA_EXHAUSTED`` and is not retryable
  — is already covered below through the live adapter, so nothing was lost.
* ``test_hicode_mapping_authority_is_unique_and_explicit`` covered
  ``load_hicode_mapping_authority``, which now only exists under
  ``legacy/executors/hicode/``. That tree is kept for archaeology and must not be
  revived, so pinning its behaviour would be a test that resists the retirement.

The filename followed the module: a cooldown test with "hicode" in its name
pointed at nothing.
"""

from __future__ import annotations

import pytest


def test_resource_exhausted_is_terminal_and_parses_reset_metadata() -> None:
    from veya.provider_cooldown import classify_upstream_failure

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
    from veya.provider_cooldown import effective_cooldown_until

    assert effective_cooldown_until(1000.0, 7200, now=1000.0) == 8200.0


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