from types import SimpleNamespace

from veya.remote.oauth import _choose_remote_token


class FakeAuth:
    def __init__(self, records):
        self._records = records

    def tokens(self):
        return list(self._records)

    def get(self, token_id):
        return next((record for record in self._records if record.token_id == token_id), None)


def token(token_id, principal, active=True):
    return SimpleNamespace(
        token_id=token_id,
        principal=principal,
        is_active=lambda: active,
    )


def test_oauth_requires_owner_and_exact_principal(monkeypatch):
    monkeypatch.delenv("VEYA_REMOTE_OAUTH_TOKEN_ID", raising=False)
    auth = FakeAuth([token("owner-grant", "owner")])
    assert (
        _choose_remote_token(auth, "owner", {"username": "owner", "is_owner": True})
        == "owner-grant"
    )
    assert _choose_remote_token(auth, "member", {"username": "member", "is_owner": False}) is None
    assert _choose_remote_token(auth, "owner", {"username": "other", "is_owner": True}) is None


def test_oauth_never_uses_global_single_active_token_fallback(monkeypatch):
    monkeypatch.delenv("VEYA_REMOTE_OAUTH_TOKEN_ID", raising=False)
    auth = FakeAuth([token("only-active", "somebody-else")])
    assert _choose_remote_token(auth, "owner", {"username": "owner", "is_owner": True}) is None


def test_configured_oauth_token_must_match_owner_principal(monkeypatch):
    monkeypatch.setenv("VEYA_REMOTE_OAUTH_TOKEN_ID", "configured")
    auth = FakeAuth([token("configured", "another-user")])
    assert _choose_remote_token(auth, "owner", {"username": "owner", "is_owner": True}) is None
    auth = FakeAuth([token("configured", "owner")])
    assert _choose_remote_token(auth, "owner", {"username": "owner", "is_owner": False}) is None
    assert (
        _choose_remote_token(auth, "owner", {"username": "owner", "is_owner": True}) == "configured"
    )


def test_oauth_ambiguous_owner_grants_fail_closed(monkeypatch):
    monkeypatch.delenv("VEYA_REMOTE_OAUTH_TOKEN_ID", raising=False)
    auth = FakeAuth([token("one", "owner"), token("two", "owner")])
    assert _choose_remote_token(auth, "owner", {"username": "owner", "is_owner": True}) is None
