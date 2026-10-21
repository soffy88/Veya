"""Hark parity Phase 0: ``veya auth`` mints tokens through the existing AuthGate.

The CLI is only a thin caller of ``server.auth``; these tests assert the token
it prints is accepted by the real ASGI middleware and that no auth rule was
weakened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cli import auth_cli
from server import auth as auth_mod


@pytest.fixture
def auth_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "auth.db"
    monkeypatch.setattr(auth_mod, "_DB_PATH", db)
    monkeypatch.delenv("VEYA_LOCAL_SINGLE_USER", raising=False)
    monkeypatch.delenv("VEYA_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("VEYA_OWNER_USER_IDS", raising=False)
    auth_mod._init_db()
    auth_mod._user_ctx.set(None)
    return db


@pytest.fixture
def account(auth_db: Path) -> dict:
    user = auth_mod.create_user("cli-auto", "a-long-enough-password", is_owner=True)
    user["password"] = "a-long-enough-password"
    return user


def _app() -> FastAPI:
    from server.auth import AuthGateMiddleware

    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)

    @app.get("/api/v1/whoami")
    def whoami() -> dict:
        return auth_mod.current_user()

    return app


def test_mint_prints_token_and_verify_resolves_user(account, capsys, monkeypatch) -> None:
    monkeypatch.setenv(auth_cli.ENV_PASSWORD, account["password"])
    assert auth_cli.run_auth_cli(["token", "--username", account["username"]]) == 0
    token = capsys.readouterr().out.strip()
    assert token

    assert auth_cli.run_auth_cli(["token", "--verify", token, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["user_id"] == account["user_id"]
    assert payload["is_owner"] is True


def test_minted_token_is_accepted_by_auth_gate(account, capsys, monkeypatch) -> None:
    monkeypatch.setenv(auth_cli.ENV_PASSWORD, account["password"])
    auth_cli.run_auth_cli(["token", "--username", account["username"]])
    token = capsys.readouterr().out.strip()

    client = TestClient(_app())
    denied = client.get("/api/v1/whoami")
    assert denied.status_code == 401

    allowed = client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {token}"})
    assert allowed.status_code == 200
    assert allowed.json()["user_id"] == account["user_id"]


def test_token_via_query_parameter_matches_sse_usage(account, capsys, monkeypatch) -> None:
    monkeypatch.setenv(auth_cli.ENV_PASSWORD, account["password"])
    auth_cli.run_auth_cli(["token", "--username", account["username"]])
    token = capsys.readouterr().out.strip()
    client = TestClient(_app())
    response = client.get(f"/api/v1/whoami?token={token}")
    assert response.status_code == 200


def test_bad_password_is_rejected(account, capsys) -> None:
    assert (
        auth_cli.run_auth_cli(["token", "--username", account["username"], "--password", "x"]) == 1
    )
    assert "invalid username or password" in capsys.readouterr().err


def test_missing_password_reports_usage(account, capsys, monkeypatch) -> None:
    monkeypatch.delenv(auth_cli.ENV_PASSWORD, raising=False)
    assert auth_cli.run_auth_cli(["token", "--username", account["username"]]) == 2
    assert auth_cli.ENV_PASSWORD in capsys.readouterr().err


def test_revoke_invalidates_token(account, capsys, monkeypatch) -> None:
    monkeypatch.setenv(auth_cli.ENV_PASSWORD, account["password"])
    auth_cli.run_auth_cli(["token", "--username", account["username"]])
    token = capsys.readouterr().out.strip()
    assert auth_cli.run_auth_cli(["token", "--revoke", token]) == 0
    assert auth_cli.run_auth_cli(["token", "--verify", token]) == 1


def test_whoami_reads_env_token(account, capsys, monkeypatch) -> None:
    monkeypatch.setenv(auth_cli.ENV_PASSWORD, account["password"])
    auth_cli.run_auth_cli(["token", "--username", account["username"]])
    token = capsys.readouterr().out.strip()
    monkeypatch.setenv(auth_cli.ENV_TOKEN, token)
    assert auth_cli.run_auth_cli(["whoami", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["username"] == account["username"]


def test_password_is_never_echoed(account, capsys, monkeypatch) -> None:
    monkeypatch.setenv(auth_cli.ENV_PASSWORD, account["password"])
    auth_cli.run_auth_cli(["token", "--username", account["username"], "--json"])
    captured = capsys.readouterr()
    assert account["password"] not in captured.out
    assert account["password"] not in captured.err


def test_cli_dispatch_routes_auth(monkeypatch) -> None:
    import cli.main as main_mod

    seen: list[list[str]] = []

    def _fake(argv):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr("cli.auth_cli.run_auth_cli", _fake)
    assert main_mod.main(["auth", "whoami"]) == 0
    assert seen == [["whoami"]]
