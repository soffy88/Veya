from __future__ import annotations

import re
import sqlite3
from base64 import b64encode
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
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


# ── RFC 7591 dynamic registration tolerance ────────────────────────────
# A real MCP client registers with `authorization_code` + `refresh_token` and
# may send a scope list. Refusing those standard shapes failed DCR while
# nothing was unsafe (no client secret is ever issued). Genuine boundary
# violations must still be refused.


def _dcr(**overrides) -> dict:
    body = {
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    "overrides",
    [
        {"grant_types": ["authorization_code", "refresh_token"]},
        {"grant_types": ["refresh_token", "authorization_code"]},
        {"scope": "mcp"},
        {"scope": "mcp profile"},
        {"scope": "profile,mcp"},
        {"client_name": "Claude", "application_type": "web"},
    ],
    ids=[
        "refresh_token_added",
        "refresh_token_first",
        "scope_plain",
        "scope_space_list",
        "scope_comma_list",
        "client_name_present",
    ],
)
def test_dcr_accepts_standard_public_client_shapes(tmp_path, monkeypatch, overrides) -> None:
    monkeypatch.setenv("VEYA_REMOTE_OAUTH_STORE_FILE", str(tmp_path / "oauth.db"))
    response = _app().post("/register", json=_dcr(**overrides))
    assert response.status_code == 201, response.text
    assert response.json()["client_id"]
    assert not response.json().get("client_secret")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"grant_types": ["implicit"]}, "authorization-code"),
        ({"grant_types": ["authorization_code", "urn:unknown"]}, "authorization-code"),
        ({"response_types": ["token"]}, "authorization-code"),
        # Implicit flow returns a token in the redirect and skips PKCE;
        # this server implements code only, so it must be refused.
        ({"response_types": ["code", "token"]}, "authorization-code"),
        ({"token_endpoint_auth_method": "client_secret_post"}, "authorization-code"),
        ({"scope": "profile"}, "scope"),
        ({"redirect_uris": ["claude://callback"]}, "HTTPS"),
        ({"redirect_uris": ["http://claude.ai/cb"]}, "HTTPS"),
    ],
    ids=[
        "implicit_only",
        "unknown_grant",
        "token_response_only",
        "code_and_token_response",
        "client_secret_method",
        "scope_without_mcp",
        "private_scheme_redirect",
        "http_remote_redirect",
    ],
)
def test_dcr_still_refuses_boundary_violations(tmp_path, monkeypatch, overrides, reason) -> None:
    monkeypatch.setenv("VEYA_REMOTE_OAUTH_STORE_FILE", str(tmp_path / "oauth.db"))
    response = _app().post("/register", json=_dcr(**overrides))
    assert response.status_code == 400, response.text
    assert reason in response.json()["error_description"]


def test_consent_works_with_a_real_uuid_user_id(tmp_path, monkeypatch) -> None:
    """A Veya user id is uuid4().hex, so the consent path must not assume int.

    Regression: `issue_code(user_id=int(user["user_id"]))` raised ValueError for
    every id containing a hex letter, so every real consent answered 500. The
    stub in the test above returns an integer, which is exactly why it passed.
    """

    monkeypatch.setenv("VEYA_REMOTE_OAUTH_STORE_FILE", str(tmp_path / "oauth.db"))
    auth = RemoteAuth()
    grant, _ = auth.issue(
        "soffy",
        permissions=RemotePermissions(),
        token_id="rt_oauth_hex_user",
        secret="static-secret",
    )
    monkeypatch.setattr(oauth.RemoteAuth, "from_env", classmethod(lambda cls, environ=None: auth))
    real_user_id = uuid4().hex
    assert not real_user_id.isdigit()  # the case that used to 500
    monkeypatch.setattr(oauth, "authenticate", lambda username, password: {"user_id": real_user_id})

    client = _app()
    redirect_uri = "http://127.0.0.1:43111/oauth/callback"
    client_id = client.post("/register", json={"redirect_uris": [redirect_uri]}).json()["client_id"]

    verifier = "B" * 64
    consent = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "state-hex",
            "code_challenge": _pkce(verifier),
            "code_challenge_method": "S256",
        },
    )
    tx = re.search(r'name="tx_id" value="([^"]+)"', consent.text)
    assert tx is not None

    decision = client.post(
        "/authorize/decision",
        data={"tx_id": tx.group(1), "username": "soffy", "password": "test"},
        follow_redirects=False,
    )
    assert decision.status_code == 302, decision.text
    code = parse_qs(urlparse(decision.headers["location"]).query)["code"][0]

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
    assert token.status_code == 200, token.text
    # The code row kept the hex id verbatim, and the grant is unchanged.
    with sqlite3.connect(oauth.OAuthStore().path) as db:
        stored = db.execute("SELECT user_id FROM codes").fetchone()[0]
    assert stored == real_user_id
    assert oauth.OAuthStore().resolve_token(token.json()["access_token"]) == grant.token_id


def test_codes_table_declares_user_id_as_text(tmp_path, monkeypatch) -> None:
    """A fresh store must not declare user_id INTEGER again."""

    monkeypatch.setenv("VEYA_REMOTE_OAUTH_STORE_FILE", str(tmp_path / "oauth.db"))
    store = oauth.OAuthStore()
    with sqlite3.connect(store.path) as db:
        declared = {row[1]: str(row[2]).upper() for row in db.execute("PRAGMA table_info(codes)")}
    assert declared["user_id"] == "TEXT"


def test_existing_integer_store_is_migrated(tmp_path, monkeypatch) -> None:
    """A store created before the fix is rebuilt, keeping its client registry."""

    path = tmp_path / "oauth.db"
    monkeypatch.setenv("VEYA_REMOTE_OAUTH_STORE_FILE", str(path))
    oauth.OAuthStore().register_client(["http://127.0.0.1:1/cb"], "mcp")
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            DROP TABLE codes;
            CREATE TABLE codes (
                code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
                user_id INTEGER NOT NULL, remote_token_id TEXT NOT NULL,
                code_challenge TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
            );
            """
        )
    oauth.OAuthStore()  # construction migrates
    with sqlite3.connect(path) as db:
        declared = {row[1]: str(row[2]).upper() for row in db.execute("PRAGMA table_info(codes)")}
        clients = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    assert declared["user_id"] == "TEXT"
    assert clients == 1  # the migration did not discard registered clients


@pytest.mark.parametrize("encoding", ["form", "json", "basic"])
def test_token_accepts_form_json_and_basic_encodings(tmp_path, monkeypatch, encoding) -> None:
    """RFC 6749 says form-encoded; real clients also send JSON or Basic.

    Reading only request.form() made those clients fail with a bare 400 and no
    diagnostic, so an interop bug looked identical to a wrong password.
    """

    monkeypatch.setenv("VEYA_REMOTE_OAUTH_STORE_FILE", str(tmp_path / "oauth.db"))
    auth = RemoteAuth()
    grant, _ = auth.issue(
        "soffy",
        permissions=RemotePermissions(),
        token_id="rt_oauth_encoding",
        secret="static-secret",
    )
    monkeypatch.setattr(oauth.RemoteAuth, "from_env", classmethod(lambda cls, environ=None: auth))
    monkeypatch.setattr(oauth, "authenticate", lambda u, p: {"user_id": uuid4().hex})
    monkeypatch.setenv("VEYA_REMOTE_OAUTH_TOKEN_ID", grant.token_id)

    client = _app()
    redirect_uri = "http://127.0.0.1:43111/oauth/callback"
    client_id = client.post("/register", json={"redirect_uris": [redirect_uri]}).json()["client_id"]
    verifier = "C" * 64
    consent = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "s",
            "code_challenge": _pkce(verifier),
            "code_challenge_method": "S256",
        },
    )
    tx = re.search(r'name="tx_id" value="([^"]+)"', consent.text).group(1)
    decision = client.post(
        "/authorize/decision",
        data={"tx_id": tx, "username": "soffy", "password": "p"},
        follow_redirects=False,
    )
    assert decision.status_code == 302, decision.text
    code = parse_qs(urlparse(decision.headers["location"]).query)["code"][0]

    fields = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code": code,
        "code_verifier": verifier,
    }
    if encoding == "form":
        response = client.post("/token", data=fields)
    elif encoding == "json":
        response = client.post("/token", json=fields)
    else:
        # HTTP Basic carries the client_id; the rest stays form-encoded.
        basic = b64encode(f"{client_id}:".encode()).decode()
        rest = {k: v for k, v in fields.items() if k != "client_id"}
        response = client.post(
            "/token",
            data=rest,
            headers={"Authorization": f"Basic {basic}"},
        )
    assert response.status_code == 200, f"{encoding}: {response.status_code} {response.text}"
    assert oauth.OAuthStore().resolve_token(response.json()["access_token"]) == grant.token_id


def test_token_refusal_records_the_request_shape(tmp_path, monkeypatch) -> None:
    """A refused token request must say which fields and encoding arrived."""

    monkeypatch.setenv("VEYA_REMOTE_OAUTH_STORE_FILE", str(tmp_path / "oauth.db"))
    response = _app().post("/token", json={"grant_type": "authorization_code"})
    assert response.status_code == 400
    assert "code_verifier" in response.json()["error_description"]
