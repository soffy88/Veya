import pytest
from veya.gsk.receipts import EgressReceiptLedger

def test_egress_receipts_chain_and_verify(tmp_path):
    receipts_file = tmp_path / "egress_receipts.jsonl"
    ledger = EgressReceiptLedger(path=receipts_file)
    
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
