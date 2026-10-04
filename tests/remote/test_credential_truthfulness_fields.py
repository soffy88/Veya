"""SF-CRED P1 — credential presence and credential validity are different facts.

`_credential_present` answers "does a credential file or env var exist", and its
call sites used it to answer "is this executor authenticated". Measured on
2026-10-04: of the four executors reporting authenticated=True, three failed on
real contact — pi with PROVIDER_CONFIGURATION_FAILURE, codex with
PROVIDER_UNAVAILABLE, claude_code with AUTH_FAILURE. Only opencode worked.

This phase adds the distinction without changing behaviour. That is deliberate:
switching consumers to `credential_valid` before anything probes it would
exclude every executor including the one that genuinely works, which is a
capability regression dressed up as a security fix. P1 makes the lie nameable;
P2 removes it.
"""

from __future__ import annotations

import sys

import pytest

from veya.remote.executor_registry import get_executor_registry

PATH = "platform/3O"
if PATH not in sys.path:
    sys.path.insert(0, PATH)


def _identity(name: str):
    return get_executor_registry().identity(name)


# ── G1: field separation ───────────────────────────────────────────────────
def test_identity_exposes_presence_and_validity_separately():
    for name in ("opencode", "pi", "codex", "claude_code", "antigravity"):
        identity = _identity(name)
        payload = identity.to_dict()
        assert "credential_present" in payload, name
        assert "credential_valid" in payload, name
        assert "credential_proven" in payload, name


def test_presence_is_observed_and_validity_is_tri_state():
    """validity is None when unprobed — not False, and not True."""
    identity = _identity("opencode")
    assert identity.credential_present is True
    assert identity.credential_valid is None
    assert identity.credential_proven is False


def test_absence_of_credential_is_reported_as_absent():
    identity = _identity("antigravity")
    assert identity.credential_present is False
    assert identity.credential_valid is None
    assert identity.credential_proven is False


def test_proven_requires_positive_evidence():
    """The whole point: presence alone must never read as proven."""
    identity = _identity("opencode")
    assert identity.authenticated is True, "presence still drives the legacy field in P1"
    assert identity.credential_proven is False, "but it is not proof"


# ── the projection must not silently drop the new fields ───────────────────
def test_to_dict_does_not_lose_the_distinction():
    payload = _identity("pi").to_dict()
    assert payload["credential_present"] is True
    assert payload["credential_valid"] is None
    assert payload["credential_proven"] is False


def test_runtime_projection_carries_the_fields():
    runtime = _identity("pi").runtime
    assert runtime.credential_present is True
    assert runtime.credential_valid is None


def test_identity_equality_is_unaffected_by_the_new_fields():
    """Two identities with the same facts stay equal; adding fields must not
    make otherwise-identical records compare unequal."""
    from veya.remote.executor_registry import ExecutorRuntimeIdentity

    def _make() -> "ExecutorRuntimeIdentity":
        return ExecutorRuntimeIdentity(
            executor_id="x",
            executor_kind="l1_worker",
            provider=None,
            model=None,
            auth_state="UNKNOWN",
            reachable=False,
            launcher=None,
        )

    first = _make()
    second = _make()
    assert first == second
    assert first.credential_valid is None


# ── the false positive this exists to end ──────────────────────────────────
def test_the_measured_false_positives_are_still_visible_as_unproven():
    """Regression pin for the inventory finding.

    These three read authenticated=True from a file-existence check and failed
    on real contact. P1 does not change that yet; it makes them askable.
    """
    for name in ("pi", "codex", "claude_code"):
        identity = _identity(name)
        assert identity.credential_present is True, name
        assert identity.credential_proven is False, name


def test_unprobed_is_distinguishable_from_proven_and_from_refuted():
    """None must be its own state, not collapsed to False."""
    unprobed = _identity("opencode").credential_valid
    assert unprobed is None
    assert unprobed is not False


@pytest.mark.parametrize("name", ["pi", "codex", "claude_code", "opencode", "antigravity", "dsh"])
def test_identity_lookup_still_works_for_every_executor(name: str):
    """Adding fields must not disturb the admission surface."""
    assert _identity(name).executor_id == name
