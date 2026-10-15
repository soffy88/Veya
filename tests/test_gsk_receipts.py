import pytest
from veya.gsk.receipts import EgressReceiptLedger

def test_egress_receipts_chain_and_verify(tmp_path):
    receipts_file = tmp_path / "egress_receipts.jsonl"
    ledger = EgressReceiptLedger(path=receipts_file, event_root=tmp_path)

    r1 = ledger.log_egress("https://api.example.com/hook", "webhook", 128, {"event": "ping"})
    r2 = ledger.log_egress("https://api.example.com/hook", "webhook", 256, {"event": "pong"})

    assert r2["prev_hash"] == r1["entry_hash"]

    ver = ledger.verify()
    assert ver["valid"] is True
    assert ver["count"] == 2

    lines = receipts_file.read_text(encoding="utf-8").splitlines()
    import json
    d1 = json.loads(lines[0])
    d1["payload_size"] = 999
    lines[0] = json.dumps(d1)
    receipts_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ver_tampered = ledger.verify()
    assert ver_tampered["valid"] is False



def test_channel_delivery_records_metadata_only_when_enabled(monkeypatch, tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace
    from server.channels import clients, dispatch

    async def fake_client_for(_platform):
        return object()

    class FakeAdapter:
        platform = "testchat"

        def __init__(self):
            self.delivered = []

        async def send_reply(self, client, *, external_chat_id, text):
            self.delivered.append((external_chat_id, text))

    adapter = FakeAdapter()
    monkeypatch.setattr(clients, "client_for", fake_client_for)
    path = tmp_path / "receipts.jsonl"
    monkeypatch.setenv("VEYA_GSK_EVENT_ROOT", str(tmp_path))
    monkeypatch.setenv("VEYA_GSK_EGRESS_RECEIPTS_PATH", str(path))
    monkeypatch.delenv("VEYA_GSK_EGRESS_RECEIPTS", raising=False)

    asyncio.run(dispatch._deliver(adapter, "private-recipient", "confidential reply body"))
    assert adapter.delivered == [("private-recipient", "confidential reply body")]
    assert not path.exists()

    monkeypatch.setenv("VEYA_GSK_EGRESS_RECEIPTS", "1")
    asyncio.run(dispatch._deliver(adapter, "private-recipient", "confidential reply body"))
    assert path.exists()
    raw = path.read_text(encoding="utf-8")
    assert "confidential reply body" not in raw
    assert "private-recipient" not in raw
    item = json.loads(raw.splitlines()[0])
    assert item["destination"] == "channel:testchat"
    assert item["payload_type"] == "channel_reply_attempt"
    assert item["payload_size"] == len("confidential reply body".encode("utf-8"))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    assert EgressReceiptLedger(path=path, event_root=tmp_path).verify()["valid"] is True
    event_path = tmp_path / ".veya" / "execution-events" / "gsk-egress-receipts.jsonl"
    assert event_path.exists()


def test_egress_receipts_detect_deleted_tail_against_event_anchor(tmp_path):
    receipts_file = tmp_path / "egress_receipts.jsonl"
    ledger = EgressReceiptLedger(path=receipts_file, event_root=tmp_path)
    ledger.log_egress("channel:test", "attempt", 3, payload_hash="a" * 64)
    ledger.log_egress("channel:test", "delivered", 3, payload_hash="a" * 64)
    receipts_file.write_text(receipts_file.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    verification = ledger.verify()
    assert verification["valid"] is False
    assert "anchors" in verification["error"]



def test_failed_channel_delivery_records_failed_receipt(monkeypatch, tmp_path):
    import asyncio
    import json
    from server.channels import clients, dispatch

    async def fake_client_for(_platform):
        return object()

    class FailingAdapter:
        platform = "testchat"

        async def send_reply(self, client, *, external_chat_id, text):
            raise RuntimeError("provider unavailable")

    path = tmp_path / "failed-receipts.jsonl"
    monkeypatch.setattr(clients, "client_for", fake_client_for)
    monkeypatch.setenv("VEYA_GSK_EVENT_ROOT", str(tmp_path))
    monkeypatch.setenv("VEYA_GSK_EGRESS_RECEIPTS_PATH", str(path))
    monkeypatch.setenv("VEYA_GSK_EGRESS_RECEIPTS", "1")

    asyncio.run(dispatch._deliver(FailingAdapter(), "private-recipient", "private body"))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["payload_type"] for row in rows] == ["channel_reply_attempt", "channel_reply_failed"]
    assert all("private body" not in json.dumps(row) for row in rows)
    assert all("private-recipient" not in json.dumps(row) for row in rows)
    assert EgressReceiptLedger(path=path, event_root=tmp_path).verify()["valid"] is True
