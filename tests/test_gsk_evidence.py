import pytest
from veya.gsk.evidence import EvidenceLedger, compute_workspace_fingerprint

def test_evidence_ledger_lifecycle(tmp_path):
    ledger_file = tmp_path / "evidence_ledger.jsonl"
    ledger = EvidenceLedger(ledger_path=ledger_file)
    
    label = "test_run_1"
    record = ledger.record_run(label, "echo 'hello'", 0, "hello\n")
    assert record["label"] == label
    assert record["exit_code"] == 0
    
    status = ledger.check(label)
    assert status == "FRESH"
    
    assert ledger.check("non_existent") == "MISSING"
