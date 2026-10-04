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


def test_unprobed_material_is_none_not_false():
    """opencode holds a working key and has never been probed.

    Its validity is None, not False. Treating unprobed as invalid would exclude
    the only executor that actually works — the failure mode P1 was written to
    avoid.
    """
    identity = _identity("opencode")
    assert identity.credential_present is True
    assert identity.credential_valid is None
    assert identity.credential_proven is False


def test_absence_of_credential_is_reported_as_absent_and_invalid():
    """No credential means no valid credential. `present` still says why."""
    identity = _identity("antigravity")
    assert identity.credential_present is False
    assert identity.credential_valid is False
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
    assert payload["credential_valid"] is False
    assert payload["credential_proven"] is False


def test_runtime_projection_carries_the_fields():
    runtime = _identity("pi").runtime
    assert runtime.credential_present is True
    assert runtime.credential_valid is False


def test_identity_equality_is_unaffected_by_the_new_fields():
    """Two identities with the same facts stay equal; adding fields must not
    make otherwise-identical records compare unequal."""
    from veya.remote.executor_registry import ExecutorRuntimeIdentity

    def _make() -> ExecutorRuntimeIdentity:
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


# ── the false positives this exists to end ─────────────────────────────────
def test_structurally_empty_credentials_are_now_invalid():
    """The P2a deliverable.

    pi and claude_code presented an existing file with nothing usable inside —
    pi's auth.json is `{}`, claude_code's access and refresh tokens are both
    empty strings. Both failed on real contact. Local evidence now settles them
    as False without a network call.
    """
    for name in ("pi", "claude_code"):
        identity = _identity(name)
        assert identity.credential_present is True, name
        assert identity.credential_valid is False, name
        assert identity.credential_proven is False, name


def test_codex_keeps_its_unprobed_state_because_material_exists():
    """codex holds a real access_token, so structure cannot refute it.

    It is the one false positive that needs P2b. Reporting False here would be
    guessing; reporting None is the truth.
    """
    identity = _identity("codex")
    assert identity.credential_present is True
    assert identity.credential_valid is None
    assert identity.credential_proven is False


def test_unprobed_is_distinguishable_from_proven_and_from_refuted():
    """None must be its own state, not collapsed to False."""
    unprobed = _identity("opencode").credential_valid
    assert unprobed is None
    assert unprobed is not False


@pytest.mark.parametrize("name", ["pi", "codex", "claude_code", "opencode", "antigravity", "dsh"])
def test_identity_lookup_still_works_for_every_executor(name: str):
    """Adding fields must not disturb the admission surface."""
    assert _identity(name).executor_id == name
