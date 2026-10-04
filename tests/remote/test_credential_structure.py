"""Unit tests for structural credential inspection.

Synthetic fixtures only. These must hold regardless of what credentials happen to
exist on the machine running them, because the whole point of the module is that
"file exists" and "credential exists" are different facts — a test that reads the
developer's real auth.json cannot demonstrate that.
"""

from __future__ import annotations

import json

import pytest

from veya.remote.credential_structure import CredentialInspection, inspect, material_in


# ── material_in: does the payload hold a usable credential value ───────────
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="empty-dict"),
        pytest.param({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}}, id="empty-tokens"),
        pytest.param({"opencode-go": {"type": "api", "key": ""}}, id="empty-key"),
        pytest.param({"auth_mode": "chatgpt"}, id="mode-string-is-not-material"),
        pytest.param({"type": "api"}, id="type-string-is-not-material"),
        pytest.param({"scopes": ["user:inference"]}, id="scope-list-is-not-material"),
        pytest.param({"expiresAt": 0}, id="int-is-not-material"),
        pytest.param(None, id="null"),
        pytest.param("plain string", id="bare-string"),
        pytest.param([], id="empty-list"),
    ],
)
def test_payloads_without_usable_material(payload: object) -> None:
    assert material_in(payload) is False


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"opencode-go": {"type": "api", "key": "k" * 67}}, id="opencode-api-key"),
        pytest.param({"claudeAiOauth": {"accessToken": "a"}}, id="claude-access-token"),
        pytest.param({"claudeAiOauth": {"refreshToken": "r"}}, id="claude-refresh-token"),
        pytest.param({"tokens": {"access_token": "t", "id_token": "i"}}, id="codex-oauth-tokens"),
        pytest.param({"OPENAI_API_KEY": "sk-x"}, id="codex-plain-key"),
        pytest.param({"nested": {"deep": {"secret": "s"}}}, id="nested-secret"),
        pytest.param([{"key": "in-a-list"}], id="material-in-list"),
    ],
)
def test_payloads_with_usable_material(payload: object) -> None:
    assert material_in(payload) is True


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_whitespace_only_values_are_not_material(blank: str) -> None:
    """An empty-but-defined token is the claude_code failure mode exactly."""
    assert material_in({"accessToken": blank}) is False


def test_field_matching_ignores_case_and_separators() -> None:
    for name in ("accessToken", "access_token", "ACCESS-TOKEN", "AccessToken"):
        assert material_in({name: "v"}) is True, name


# ── CredentialInspection verdicts ──────────────────────────────────────────
def test_unusable_covers_both_absent_and_empty() -> None:
    """No usable material either way. `present` keeps them distinguishable."""
    absent = CredentialInspection(present=False, material_found=False)
    empty = CredentialInspection(present=True, material_found=False, source="f")
    assert absent.unusable is True
    assert empty.unusable is True
    assert absent.present is not empty.present


def test_material_present_is_not_unusable() -> None:
    found = CredentialInspection(present=True, material_found=True, source="f")
    assert found.unusable is False


# ── inspect(): env wins, files are the fallback ────────────────────────────
def test_env_var_with_value_wins_over_a_stale_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_EXECUTOR_KEY", "live-value")
    verdict = inspect("no-such-executor", ["TEST_EXECUTOR_KEY"])
    assert verdict.material_found is True
    assert verdict.source == "TEST_EXECUTOR_KEY"


def test_defined_but_empty_env_var_is_not_material(monkeypatch) -> None:
    monkeypatch.setenv("TEST_EXECUTOR_KEY", "   ")
    verdict = inspect("no-such-executor", ["TEST_EXECUTOR_KEY"])
    assert verdict.present is True
    assert verdict.material_found is False
    assert verdict.unusable is True


def test_missing_everything_is_absent_and_unusable(monkeypatch) -> None:
    monkeypatch.delenv("TEST_EXECUTOR_KEY", raising=False)
    verdict = inspect("no-such-executor", ["TEST_EXECUTOR_KEY"])
    assert verdict.present is False
    assert verdict.material_found is False
    assert verdict.unusable is True


def test_unparseable_file_is_not_material(tmp_path, monkeypatch) -> None:
    """A corrupt file is not a working credential, and must not raise."""
    bad = tmp_path / "auth.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setitem(
        __import__(
            "veya.remote.credential_structure", fromlist=["CREDENTIAL_FILES"]
        ).CREDENTIAL_FILES,
        "fixture-executor",
        [bad],
    )
    verdict = inspect("fixture-executor", [])
    assert verdict.present is True
    assert verdict.material_found is False


def test_detail_never_leaks_a_credential_value(tmp_path, monkeypatch) -> None:
    """`detail` is logged, so it must describe shape only."""
    secret = "sk-do-not-log-this-value"
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"apiKey": secret}), encoding="utf-8")
    from veya.remote import credential_structure as cs

    monkeypatch.setitem(cs.CREDENTIAL_FILES, "fixture-executor", [path])
    verdict = inspect("fixture-executor", [])
    assert secret not in verdict.detail
    assert secret not in str(verdict)
    assert verdict.material_found is True
