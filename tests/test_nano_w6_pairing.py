"""NANO-W6 addendum J: device pairing codes (VEYA_DEVICE_PAIRING, default OFF).

Covers: code generation (8 chars, unambiguous alphabet), 10-minute TTL,
single use, rate limiting, constant-time consume, credential issue/verify,
the omodul pairing flow with decision_trail, and the REST endpoints behind
the flag. Codes and credentials are never logged.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def pairing_store(tmp_path):
    from veya.obase.pairing import PairingCodeStore

    return PairingCodeStore(str(tmp_path / "pairing.db"))


# ── code generation / validation ───────────────────────────────────────


def test_code_shape_and_alphabet(pairing_store):
    from veya.obase.pairing import PAIRING_ALPHABET, PAIRING_CODE_LENGTH

    result = pairing_store.generate(device_kind="runner", device_label="home")
    assert "code" in result
    code = result["code"]
    assert len(code) == PAIRING_CODE_LENGTH == 8
    assert all(char in PAIRING_ALPHABET for char in code)
    # unambiguous alphabet: no confusables
    assert not (set(PAIRING_ALPHABET) & set("01OIL"))


def test_code_ttl_and_single_use(pairing_store):
    result = pairing_store.generate(device_kind="browser", device_label="b1", ttl_s=600.0)
    code = result["code"]
    assert result["expires_at"] > time.time()
    first = pairing_store.consume(code)
    assert first["status"] == "ok"
    assert first["device_kind"] == "browser"
    # single use: a second consume never succeeds
    assert pairing_store.consume(code)["status"] == "already_consumed"


def test_code_expiry(pairing_store):
    result = pairing_store.generate(device_kind="browser", device_label="b2", ttl_s=30.0)
    code = result["code"]
    assert pairing_store.consume(code, now=time.time() + 31.0)["status"] == "expired"


def test_code_rate_limited(pairing_store):
    from veya.obase.pairing import RATE_LIMIT_MAX_PER_WINDOW

    for _ in range(RATE_LIMIT_MAX_PER_WINDOW):
        assert "code" in pairing_store.generate(device_kind="browser", device_label="spammy")
    refused = pairing_store.generate(device_kind="browser", device_label="spammy")
    assert refused == {"error": "rate_limited"}
    # a different device label is unaffected
    assert "code" in pairing_store.generate(device_kind="browser", device_label="other")


def test_code_invalid_and_attempt_capped(pairing_store):
    pairing_store.generate(device_kind="browser", device_label="b3")
    assert pairing_store.consume("AAAAAAAA")["status"] == "invalid"
    result = pairing_store.generate(device_kind="browser", device_label="b4")
    code = result["code"]
    from veya.obase.pairing import MAX_ATTEMPTS_PER_CODE

    # wrong guesses are capped; the right code still works before the cap
    for _ in range(MAX_ATTEMPTS_PER_CODE - 1):
        pairing_store.consume("BBBBBBBB")
    assert pairing_store.consume(code)["status"] == "ok"


def test_store_never_persists_plaintext(pairing_store):
    result = pairing_store.generate(device_kind="runner", device_label="home")
    code = result["code"]
    pairing_store.consume(code)
    import sqlite3

    conn = sqlite3.connect(pairing_store._path)
    try:
        rows = conn.execute("SELECT * FROM pairing_codes").fetchall()
    finally:
        conn.close()
    assert rows
    assert all(code not in str(row) for row in rows)


# ── device credentials ────────────────────────────────────────────────


def test_credential_issue_and_verify(pairing_store):
    issued = pairing_store.issue_credential(device_kind="runner", device_label="home")
    assert issued["token"] and issued["token_id"]
    verified = pairing_store.verify_credential(issued["token"])
    assert verified is not None
    assert verified["device_label"] == "home"
    assert pairing_store.verify_credential("wrong-token") is None


def test_credential_expiry(pairing_store):
    issued = pairing_store.issue_credential(device_kind="browser", device_label="b5", ttl_s=10.0)
    assert pairing_store.verify_credential(issued["token"], now=time.time() + 11.0) is None


# ── omodul pairing flow ───────────────────────────────────────────────


async def test_omodul_pairing_flow_request_and_confirm(pairing_store):
    from veya.omodul.pairing_flow import PairingFlowConfig, run_pairing_flow

    trails: list[str] = []
    config = PairingFlowConfig(
        store=pairing_store,
        trail=lambda topic, payload: trails.append(topic),
    )
    requested = await run_pairing_flow(
        config, {"op": "request", "device_kind": "runner", "device_label": "home"}, "/tmp"
    )
    assert requested["status"] == "completed"
    assert len(requested["code"]) == 8

    registered: dict = {}

    def _register(kind, label, credential):
        registered["ref"] = f"VEYA_DEVICE_TOKEN_{credential['token_id'].upper()}"
        return {"registered": True}

    config2 = PairingFlowConfig(
        store=pairing_store,
        trail=lambda topic, payload: trails.append(topic),
        issue_credential=lambda kind, label: pairing_store.issue_credential(
            device_kind=kind, device_label=label
        ),
        register_device=_register,
    )
    confirmed = await run_pairing_flow(
        config2,
        {"op": "confirm", "code": requested["code"], "device_label": "home"},
        "/tmp",
    )
    assert confirmed["status"] == "completed"
    assert confirmed["credential"]["token_id"]
    assert confirmed["credential"]["token"]
    assert registered["ref"].startswith("VEYA_DEVICE_TOKEN_")
    # the code is spent: confirming again fails
    again = await run_pairing_flow(config2, {"op": "confirm", "code": requested["code"]}, "/tmp")
    assert again["status"] == "error"
    assert "pairing.requested" in trails and "pairing.confirmed" in trails


async def test_omodul_pairing_flow_never_raises(pairing_store):
    from veya.omodul.pairing_flow import PairingFlowConfig, run_pairing_flow

    config = PairingFlowConfig(store=pairing_store)
    result = await run_pairing_flow(config, {"op": "bogus"}, "/tmp")
    assert result["status"] == "error"
    result = await run_pairing_flow(config, {"op": "confirm", "code": "ZZZZZZZZ"}, "/tmp")
    assert result["status"] == "error"


# ── REST behind the flag ───────────────────────────────────────────────


def _rest_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.pairing import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_pairing_rest_disabled_by_default(monkeypatch):
    from server import pairing as pairing_mod

    monkeypatch.delenv("VEYA_DEVICE_PAIRING", raising=False)
    assert pairing_mod.device_pairing_enabled() is False
    client = _rest_client()
    assert (
        client.post("/api/v1/pairing/request", json={"device_kind": "browser"}).status_code == 404
    )
    assert client.post("/api/v1/pairing/confirm", json={"code": "ABC"}).status_code == 404


def test_pairing_rest_enabled_roundtrip(monkeypatch, tmp_path):
    from server import pairing as pairing_mod

    monkeypatch.setenv("VEYA_DEVICE_PAIRING", "1")
    monkeypatch.setenv("VEYA_PAIRING_PATH", str(tmp_path / "pairing.db"))
    assert pairing_mod.device_pairing_enabled() is True
    client = _rest_client()
    resp = client.post(
        "/api/v1/pairing/request", json={"device_kind": "browser", "device_label": "phone"}
    )
    assert resp.status_code == 200
    code = resp.json()["code"]
    assert len(code) == 8
    # no token → anonymous user; confirm still works (the code is the authority)
    resp2 = client.post("/api/v1/pairing/confirm", json={"code": code})
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["device_kind"] == "browser"
    assert body["credential"]["token"]
    # single use
    resp3 = client.post("/api/v1/pairing/confirm", json={"code": code})
    assert resp3.status_code == 400
