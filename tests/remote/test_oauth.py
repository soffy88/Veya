from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

import veya.remote.oauth as oauth
from veya.remote.auth import RemoteAuth
from veya.remote.models import RemotePermissions


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(oauth.router)
    return TestClient(app)


def _pkce(verifier: str) -> str:
    return oauth._pkce_s256(verifier)


def test_oauth_metadata_and_dcr(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VEYA_REMOTE_OAUTH_STORE_FILE", str(tmp_path / "oauth.db"))
    client = _app()

    resource = client.get("/.well-known/oauth-protected-resource")
    assert resource.status_code == 200
    assert resource.json()["resource"] == "https://veya.aiinote.com/mcp"
    assert resource.json()["authorization_servers"] == ["https://veya.aiinote.com"]

    server = client.get("/.well-known/oauth-authorization-server")
    assert server.status_code == 200
    metadata = server.json()
    assert metadata["authorization_endpoint"].endswith("/authorize")
    assert metadata["token_endpoint"].endswith("/token")
    assert metadata["registration_endpoint"].endswith("/register")
    assert metadata["code_challenge_methods_supported"] == ["S256"]

    registration = client.post(
        "/register",
        json={
            "redirect_uris": ["http://127.0.0.1:43111/oauth/callback"],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert registration.status_code == 201
    body = registration.json()
    assert body["client_id"].startswith("vc_")
    assert body["token_endpoint_auth_method"] == "none"


def test_oauth_authorization_code_pkce_and_replay(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VEYA_REMOTE_OAUTH_STORE_FILE", str(tmp_path / "oauth.db"))

    auth = RemoteAuth()
    grant, _ = auth.issue(
        "soffy",
        permissions=RemotePermissions(),
        token_id="rt_oauth_test",
        secret="static-secret",
    )
    monkeypatch.setattr(oauth.RemoteAuth, "from_env", classmethod(lambda cls, environ=None: auth))
    monkeypatch.setattr(oauth, "authenticate", lambda username, password: {"user_id": 1})

    client = _app()
    redirect_uri = "http://127.0.0.1:43111/oauth/callback"
    registration = client.post("/register", json={"redirect_uris": [redirect_uri]})
    client_id = registration.json()["client_id"]

    verifier = "A" * 64
    challenge = _pkce(verifier)
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "state-123",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 200
    match = re.search(r'name="tx_id" value="([^"]+)"', response.text)
    assert match is not None

    decision = client.post(
        "/authorize/decision",
        data={"tx_id": match.group(1), "username": "soffy", "password": "test"},
        follow_redirects=False,
    )
    assert decision.status_code == 302
    params = parse_qs(urlparse(decision.headers["location"]).query)
    code = params["code"][0]
    assert params["state"] == ["state-123"]

    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200
    access_token = token.json()["access_token"]
    assert len(access_token) > 40

    replay = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": verifier,
        },
    )
    assert replay.status_code == 400

    resolved = oauth.OAuthStore().resolve_token(access_token)
    assert resolved == grant.token_id
    assert oauth._digest(access_token) != access_token


def test_oauth_rejects_bad_redirect_and_pkce(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VEYA_REMOTE_OAUTH_STORE_FILE", str(tmp_path / "oauth.db"))
    client = _app()

    good = "http://127.0.0.1:43111/oauth/callback"
    assert client.post("/register", json={"redirect_uris": [good]}).status_code == 201
    client_id = client.post("/register", json={"redirect_uris": [good]}).json()["client_id"]

    bad_redirect = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:43112/oauth/callback",
            "state": "x",
            "code_challenge": _pkce("B" * 64),
            "code_challenge_method": "S256",
        },
    )
    assert bad_redirect.status_code == 400

    bad_method = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": good,
            "state": "x",
            "code_challenge": _pkce("B" * 64),
            "code_challenge_method": "plain",
        },
    )
    assert bad_method.status_code == 400
