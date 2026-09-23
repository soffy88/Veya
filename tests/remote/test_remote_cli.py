"""Operator CLI tests for remote token administration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from veya.remote import RemoteAuth, RemoteAuthError
from veya.remote.cli import main


def _use_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    store = tmp_path / "tokens.json"
    monkeypatch.setenv("VEYA_REMOTE_TOKENS_FILE", str(store))
    monkeypatch.delenv("VEYA_REMOTE_TOKENS", raising=False)
    return store


def test_issue_requires_workspace(tmp_path: Path, monkeypatch, capsys) -> None:
    _use_store(monkeypatch, tmp_path)
    assert main(["issue", "--principal", "alice"]) == 2
    assert "workspace" in capsys.readouterr().err


def test_issue_list_rotate_revoke(tmp_path: Path, monkeypatch, capsys) -> None:
    store = _use_store(monkeypatch, tmp_path)
    assert (
        main(
            [
                "issue",
                "--principal",
                "alice",
                "--workspace",
                str(tmp_path),
                "--write",
                "--shell",
            ]
        )
        == 0
    )
    issued = json.loads(capsys.readouterr().out)
    token_id, secret = issued["token_id"], issued["secret"]
    assert secret not in store.read_text(encoding="utf-8")

    assert main(["list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["token_id"] == token_id
    assert rows[0]["permissions"]["shell"] is True

    assert main(["rotate", "--token-id", token_id]) == 0
    rotated = json.loads(capsys.readouterr().out)["secret"]
    assert rotated != secret

    auth = RemoteAuth.from_env()
    assert auth.verify(f"Bearer {rotated}").token_id == token_id
    with pytest.raises(RemoteAuthError):
        auth.verify(f"Bearer {secret}")

    assert main(["revoke", "--token-id", token_id]) == 0
    with pytest.raises(RemoteAuthError):
        RemoteAuth.from_env().verify(f"Bearer {rotated}")

    assert main(["revoke", "--token-id", "missing"]) == 1
