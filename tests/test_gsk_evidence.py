import pytest
from veya.gsk.evidence import EvidenceLedger, compute_workspace_fingerprint


def test_evidence_ledger_lifecycle(tmp_path):
    ledger_file = tmp_path / "evidence_ledger.jsonl"
    ledger = EvidenceLedger(ledger_path=ledger_file, event_root=tmp_path)

    label = "test_run_1"
    record = ledger.record_run(label, "echo 'hello'", 0, "hello\n")
    assert record["label"] == label
    assert record["exit_code"] == 0

    status = ledger.check(label)
    assert status == "FRESH"

    assert ledger.check("non_existent") == "MISSING"


def test_direct_execution_records_evidence_only_when_enabled(monkeypatch, tmp_path):
    import asyncio
    import hashlib
    import json
    import sys
    from veya.remote.direct_exec import run_direct_command

    ledger_path = tmp_path / "evidence.jsonl"
    monkeypatch.setenv("VEYA_GSK_EVENT_ROOT", str(tmp_path))
    monkeypatch.setenv("VEYA_GSK_EVIDENCE_PATH", str(ledger_path))
    monkeypatch.delenv("VEYA_GSK_EVIDENCE", raising=False)
    command = [sys.executable, "-c", "print(6 * 7)"]

    raw = asyncio.run(
        run_direct_command(
            tmp_path, command, cwd=tmp_path, profile="l0_workspace_full", timeout_s=10
        )
    )
    assert raw.status == "passed"
    assert raw.stdout_tail.strip() == "42"
    assert not ledger_path.exists()

    monkeypatch.setenv("VEYA_GSK_EVIDENCE", "1")
    recorded = asyncio.run(
        run_direct_command(
            tmp_path, command, cwd=tmp_path, profile="l0_workspace_full", timeout_s=10
        )
    )
    assert recorded.status == "passed"
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["exit_code"] == 0
    assert "42\n" not in json.dumps(rows[0])
    assert rows[0]["stdout_summary"] == hashlib.sha256(b"42\n").hexdigest()
    assert rows[0]["command"].startswith(f"cwd={tmp_path} command=")
    ledger = EvidenceLedger(ledger_path=ledger_path, event_root=tmp_path)
    assert ledger.check(rows[0]["label"]) == "FRESH"
    assert ledger.evidence_for(rows[0]["label"])["status"] == "FRESH"
    assert (tmp_path / ".veya" / "execution-events" / "gsk-evidence-ledger.jsonl").exists()
