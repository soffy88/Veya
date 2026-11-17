"""Per-tenant tamper-evident audit chain (Buzz borrowing item 1)."""

from __future__ import annotations

import sqlite3

import pytest

from veya.obase import audit_chain as ac


@pytest.fixture()
def store():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    instance = ac.AuditChainStore(conn)
    prev = ac.configure_audit_chain_store(instance)
    yield instance
    ac.configure_audit_chain_store(prev)
    instance.close()


def test_chain_is_independent_per_tenant(store):
    first = store.append(action="risk.asked", tenant="a", detail={"n": 1})
    other = store.append(action="risk.asked", tenant="b", detail={"n": 2})
    second = store.append(action="risk.allowed", tenant="a", detail={"n": 3})

    assert first["seq"] == 1 and other["seq"] == 1 and second["seq"] == 2
    assert second["prev_hash"] == first["hash"]
    assert store.verify_chain("a")["ok"] is True
    assert store.verify_chain("b")["ok"] is True


def test_verify_detects_tampered_detail_and_broken_link(store):
    store.append(action="risk.asked", tenant="a", detail={"n": 1})
    store.append(action="risk.allowed", tenant="a", detail={"n": 2})
    store._conn.execute("UPDATE audit_chain SET detail='{\"n\": 99}' WHERE seq=1")
    store._conn.commit()

    result = store.verify_chain("a")
    assert result["ok"] is False and "hash mismatch" in result["reason"]

    store._conn.execute("UPDATE audit_chain SET detail='{\"n\": 1}' WHERE seq=1")
    store._conn.execute("UPDATE audit_chain SET prev_hash=? WHERE seq=2", ("f" * 64,))
    store._conn.commit()
    assert store.verify_chain("a")["ok"] is False


def test_tenant_id_is_bound_into_digest(store):
    a = store.append(action="risk.asked", tenant="a", actor="x")
    b = store.append(action="risk.asked", tenant="b", actor="x")
    assert a["hash"] != b["hash"], "tenant id must prevent cross-tenant row moves"

    # A hostile copy of tenant A's row under tenant B fails verification.
    store._conn.execute(
        "INSERT INTO audit_chain (tenant, seq, prev_hash, hash, action, actor, object_id, detail, created_at)"
        " VALUES ('b', 2, ?, ?, ?, ?, ?, ?, ?)",
        (a["prev_hash"], a["hash"], a["action"], a["actor"], "", "{}", a["created_at"]),
    )
    store._conn.commit()
    assert store.verify_chain("b")["ok"] is False


def test_append_failure_is_best_effort(monkeypatch, store):
    def boom(**kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(store, "append", boom)
    ac.configure_audit_chain_store(store)
    assert ac.safe_append(action="risk.asked") is None


def test_approval_store_emits_chain_records(tmp_path, monkeypatch):
    from veya.obase.approvals import ApprovalStore

    monkeypatch.setenv("VEYA_APPROVALS_PATH", str(tmp_path / "approvals.db"))
    monkeypatch.setenv("VEYA_AUDIT_CHAIN_PATH", str(tmp_path / "approvals.db"))
    ac.configure_audit_chain_store(None)
    approvals = ApprovalStore()
    approvals.create_approval(
        {
            "request_id": "req-1",
            "tool": "send_email",
            "scope": "scope",
            "actor": "master",
        }
    )
    approvals.resolve_approval("req-1", decision="once")
    result = ac.verify_chain(ac.DEFAULT_TENANT)
    assert result["ok"] is True and result["count"] == 2
    ac.configure_audit_chain_store(None)


def test_flag_default_on_and_cli(monkeypatch, capsys):
    from server.audit_chain_admin import run_audit_chain_cli

    monkeypatch.delenv("VEYA_AUDIT_CHAIN", raising=False)
    assert ac.audit_chain_enabled() is True
    monkeypatch.setenv("VEYA_AUDIT_CHAIN", "0")
    assert ac.audit_chain_enabled() is False
    monkeypatch.delenv("VEYA_AUDIT_CHAIN", raising=False)
    assert run_audit_chain_cli(["verify", "--tenant", "a", "--json"]) == 0
    assert '"ok": true' in capsys.readouterr().out
