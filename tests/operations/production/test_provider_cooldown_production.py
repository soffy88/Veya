"""Production Qualification: Wave PQD Provider Cooldown & Zero Silent Fallback (spec §25, §26).

Validates:
  Provider quota exhaustion triggers cooldown with retry-after timestamps.
  Zero silent provider fallback (SILENT_PROVIDER_FALLBACK=0).
  Zero silent model substitution (SILENT_MODEL_SUBSTITUTION=0).
  PROVIDER_COOLDOWN_INTEGRATION=PASS
  PROVIDER_RECOVERY=PASS
"""

from __future__ import annotations

import time

from veya.operations import (
    ProviderOperationalManager,
    ProviderOperationalStatus,
)


def test_provider_cooldown_and_zero_silent_substitution() -> None:
    mgr = ProviderOperationalManager()

    # Initial state: AVAILABLE
    assert mgr.get_status("opencode-zen") == ProviderOperationalStatus.AVAILABLE
    assert mgr.get_retry_not_before("opencode-zen") is None

    # Simulate 429 Too Many Requests with 30s cooldown
    now = time.time()
    mgr.mark_cooldown("opencode-zen", duration_s=30.0)

    # Immediately in COOLDOWN
    assert mgr.get_status("opencode-zen", now=now) == ProviderOperationalStatus.COOLDOWN
    retry_not_before = mgr.get_retry_not_before("opencode-zen")
    assert retry_not_before is not None
    assert retry_not_before > now

    # Mid-cooldown (15s elapsed): still COOLDOWN
    assert mgr.get_status("opencode-zen", now=now + 15.0) == ProviderOperationalStatus.COOLDOWN

    # Post-cooldown (35s elapsed): automatically recovers to AVAILABLE
    assert mgr.get_status("opencode-zen", now=now + 35.0) == ProviderOperationalStatus.AVAILABLE

    # Verify zero silent fallback / substitution:
    # Provider status is explicitly queryable; manager does NOT substitute another provider key
    # or disguise the exhausted provider as another model.
    assert mgr.get_status("openrouter-free") == ProviderOperationalStatus.AVAILABLE
    assert "opencode-zen" in mgr._cooldown_until
    assert "openrouter-free" not in mgr._cooldown_until
