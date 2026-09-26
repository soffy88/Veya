"""Phase 1 (P1) Qualification Suite for Veya Execution Contract.

Covers:
1. worktree-per-execution dynamic provisioning & lifecycle teardown
2. SessionEnvelope handoff (generation bump) & fork (branching lineage)
3. VeyaEvent fabric: sequence, causality, and typing
4. Principal / Agent identity enforcement: cross-principal isolation (TOOL_DENIED)
5. Machine-readable CLI (--json surface)
6. Tamper-evident cryptographic SHA-256 evidence chain verification
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from veya.remote.cli import main as run_remote_cli
from veya.remote.events import VeyaEvent, VeyaEventType, create_veya_event
from veya.remote.execution import (
    DurableJobManager,
    ExecutionError,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionStore,
)
from veya.remote.execution_contract import (
    GENESIS_HASH,
    RuntimeCapabilityManifest,
    SessionEnvelope,
    build_evidence_chain,
    verify_evidence_chain,
)


def _binding(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        requested_path=str(root),
        requested_realpath=str(root),
        repo_root=str(root),
        repo_identity=str(root),
        worktree_path=None,
        worktree_repo_root=None,
    )


def _session(
    session_id: str = "s_1", token_id: str = "rt_tester", principal: str = "tester"
) -> SimpleNamespace:
    return SimpleNamespace(session_id=session_id, token_id=token_id, principal=principal)


async def _dummy_runner(reporter: Any) -> str:
    return "ok"


def _submit_helper(manager: DurableJobManager, **kwargs: Any) -> ExecutionRecord:
    defaults = {
        "tool": "shell.exec",
        "veya_tool": "shell.exec",
        "runner": _dummy_runner,
    }
    defaults.update(kwargs)
    return manager.submit(**defaults)


@pytest.fixture
def temp_store(tmp_path: Path) -> ExecutionStore:
    return ExecutionStore(str(tmp_path / "executions.jsonl"))


@pytest.fixture
def job_manager(temp_store: ExecutionStore) -> DurableJobManager:
    return DurableJobManager(store=temp_store)


# ── 1. Worktree-per-execution Lifecycle ─────────────────────────


async def test_worktree_per_execution_isolated_and_teardown(tmp_path: Path) -> None:
    """Mutating tasks provision dedicated worktrees and tear them down on completion."""
    # Setup temporary git repo
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@veya.local"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Veya Tester"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    store = ExecutionStore(str(tmp_path / "executions.jsonl"))
    manager = DurableJobManager(store=store)

    # 1. Provision with isolated_worktree=True
    record = _submit_helper(
        manager,
        session=_session(token_id="rt_tester", principal="tester"),
        binding=_binding(repo_dir),
        token_id="rt_tester",
        workspace_realpath=str(repo_dir),
        isolated_worktree=True,
        keep_worktree=False,
        principal="tester",
        agent_role="specialist",
        agent_identity="agent_spec_1",
    )

    assert record.isolated_worktree is True
    assert record.worktree_path is not None
    wt_path = Path(record.worktree_path)
    assert wt_path.exists()
    assert (wt_path / "README.md").exists()

    # 2. Finish execution -> worktree should be torn down because keep_worktree is False
    manager._finish(record, str(ExecutionStatus.COMPLETED), message="done")
    assert not wt_path.exists()


async def test_worktree_retention_when_keep_worktree_true(tmp_path: Path) -> None:
    """When keep_worktree=True, worktree is preserved after completion."""
    repo_dir = tmp_path / "test_repo_keep"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@veya.local"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Veya Tester"], cwd=repo_dir, check=True)
    (repo_dir / "file.txt").write_text("hello\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    store = ExecutionStore(str(tmp_path / "executions.jsonl"))
    manager = DurableJobManager(store=store)

    record = _submit_helper(
        manager,
        session=_session(token_id="rt_tester", principal="tester"),
        binding=_binding(repo_dir),
        token_id="rt_tester",
        workspace_realpath=str(repo_dir),
        isolated_worktree=True,
        keep_worktree=True,
    )

    assert record.worktree_path is not None
    wt_path = Path(record.worktree_path)
    assert wt_path.exists()

    manager._finish(record, str(ExecutionStatus.COMPLETED), message="done")
    # Should still exist because keep_worktree=True
    assert wt_path.exists()


# ── 2. SessionEnvelope Handoff & Fork ───────────────────────────


def test_session_envelope_handoff_and_generation() -> None:
    """Handoff bumps generation, preserves lineage, and checks target capability."""
    env = SessionEnvelope(
        mission_id="m_100",
        execution_id="ex_original",
        executor_id="hicode",
        session_id="s_sess1",
        objective="Implement feature X",
        workspace="/repo",
        accepted_progress=["task1 done"],
        evidence_refs=["ev_1"],
        git_state={"branch": "main"},
        continuation_summary="Finished task 1",
        created_at=time.time(),
        generation=1,
    )

    # Valid handoff to dsh
    handed_off = env.handoff("dsh", reason="Requires shell diagnostics")
    assert handed_off.generation == 2
    assert handed_off.parent_execution_id == "ex_original"
    assert handed_off.executor_id == "dsh"
    assert handed_off.mission_id == "m_100"
    assert "Handoff from hicode to dsh" in handed_off.continuation_summary

    # Handoff to unavailable executor fails closed
    with patch("veya.remote.execution_contract.probe_runtime_capability_manifest") as mock_probe:
        mock_manifest = MagicMock(spec=RuntimeCapabilityManifest)
        mock_manifest.status = "UNAVAILABLE"
        mock_manifest.status_reason = "binary missing"
        mock_probe.return_value = mock_manifest

        with pytest.raises(ValueError, match="UNAVAILABLE"):
            handed_off.handoff("missing_exec", reason="test")


def test_session_envelope_fork() -> None:
    """Fork branches session into an isolated child session."""
    env = SessionEnvelope(
        mission_id="m_100",
        execution_id="ex_original",
        executor_id="hicode",
        session_id="s_sess1",
        objective="Implement feature X",
        workspace="/repo",
        accepted_progress=["task1 done"],
        evidence_refs=["ev_1"],
        git_state={"branch": "main"},
        continuation_summary="Finished task 1",
        created_at=time.time(),
        generation=2,
    )

    child = env.fork("Investigate side issue Y")
    assert child.generation == 1
    assert child.parent_execution_id == "ex_original"
    assert child.forked_from_session_id == "s_sess1"
    assert child.session_id != env.session_id
    assert child.execution_id != env.execution_id
    assert child.objective == "Investigate side issue Y"


# ── 3. VeyaEvent Lifecycle Fabric ───────────────────────────────


def test_veya_event_fabric_structure_and_causality() -> None:
    """VeyaEvent produces monotonic sequence and preserves causal parent links."""
    ev1 = create_veya_event(
        event_type=VeyaEventType.EXECUTION_CREATED,
        execution_id="ex_test",
        mission_id="m_1",
        actor="orchestrator",
        payload={"task": "init"},
        seq=1,
    )
    assert ev1.event_type == VeyaEventType.EXECUTION_CREATED
    assert ev1.seq >= 1
    assert ev1.parent_event_id is None

    ev2 = create_veya_event(
        event_type=VeyaEventType.EXECUTION_STARTED,
        execution_id="ex_test",
        mission_id="m_1",
        actor="worker",
        parent_event_id=ev1.event_id,
        payload={"worker_pid": 1234},
        seq=2,
    )
    assert ev2.seq > ev1.seq
    assert ev2.parent_event_id == ev1.event_id

    # Verify serialization round-trip
    d = ev2.to_dict()
    ev2_back = VeyaEvent.from_dict(d)
    assert ev2_back.event_id == ev2.event_id
    assert ev2_back.parent_event_id == ev1.event_id
    assert ev2_back.event_type == VeyaEventType.EXECUTION_STARTED


# ── 4. Principal / Agent Identity Enforcement ────────────────────


async def test_principal_identity_authorization_isolation(
    job_manager: DurableJobManager, tmp_path: Path
) -> None:
    """Cross-principal actions must be strictly denied with TOOL_DENIED."""
    rec = _submit_helper(
        job_manager,
        session=_session(token_id="rt_alice_token", principal="alice"),
        binding=_binding(tmp_path),
        token_id="rt_alice_token",
        principal="alice",
        principal_id="alice",
        agent_role="architect",
        agent_identity="agent_alice_v1",
    )

    # 1. Status query: Alice herself succeeds
    res = job_manager.status(rec.execution_id, token_id="rt_alice_token", principal="alice")
    assert res.execution_id == rec.execution_id

    # System / admin succeeds
    res_sys = job_manager.status(rec.execution_id, token_id="rt_alice_token", principal="system")
    assert res_sys.execution_id == rec.execution_id

    res_admin = job_manager.status(rec.execution_id, token_id="rt_alice_token", principal="admin")
    assert res_admin.execution_id == rec.execution_id

    # Cross-principal Bob is DENIED
    with pytest.raises(ExecutionError) as exc_info:
        job_manager.status(rec.execution_id, token_id="rt_alice_token", principal="bob")
    assert exc_info.value.code == "TOOL_DENIED"

    # 2. Cancel: Bob is DENIED
    with pytest.raises(ExecutionError) as exc_info:
        await job_manager.cancel(rec.execution_id, token_id="rt_alice_token", principal="bob")
    assert exc_info.value.code == "TOOL_DENIED"

    # Alice cancel succeeds
    cancelled = await job_manager.cancel(
        rec.execution_id, token_id="rt_alice_token", principal="alice"
    )
    assert cancelled.cancel_requested is True


# ── 5. Machine-Readable CLI ──────────────────────────────────────


def test_cli_execution_manifest_json(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI execution manifest --json returns valid JSON object of manifests."""
    ret = run_remote_cli(["execution", "manifest", "--json"])
    assert ret == 0
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert "HICODE" in data
    assert "status" in data["HICODE"]
    assert data["HICODE"]["status"] == "READY"


async def test_cli_execution_list_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI execution list --json returns valid JSON array of records."""
    store_file = tmp_path / "cli_executions.jsonl"
    monkeypatch.setenv("VEYA_REMOTE_EXECUTION_STORE", str(store_file))

    store = ExecutionStore(str(store_file))
    mgr = DurableJobManager(store=store)
    rec = _submit_helper(
        mgr,
        session=_session(token_id="rt_cli_test", principal="test_user"),
        binding=_binding(tmp_path),
        token_id="rt_cli_test",
        principal="test_user",
    )

    ret = run_remote_cli(["execution", "list", "--json"])
    assert ret == 0
    captured = capsys.readouterr().out
    rows = json.loads(captured)
    assert isinstance(rows, list)
    matching = [r for r in rows if r["execution_id"] == rec.execution_id]
    assert len(matching) == 1
    assert matching[0]["principal_id"] == "test_user"


# ── 6. Tamper-Evident Evidence Hash Chaining ────────────────────


def test_evidence_hash_chain_genesis_and_verification() -> None:
    """Evidence hash chain builds valid cryptographic hashes and verifies cleanly."""
    items: list[dict[str, Any]] = [
        {"category": "test", "name": "test_auth", "outcome": "passed"},
        {"category": "artifact", "path": "build/app.bin", "size": 1024},
        {"category": "change", "file": "src/main.py", "lines_added": 12},
    ]

    chain = build_evidence_chain(items)
    assert len(chain) == 3

    # Check genesis block linking
    assert chain[0]["chain_index"] == 0
    assert chain[0]["prev_hash"] == GENESIS_HASH
    assert chain[0]["chain_hash"] != ""

    # Check block 1 linked to block 0
    assert chain[1]["chain_index"] == 1
    assert chain[1]["prev_hash"] == chain[0]["chain_hash"]

    # Check block 2 linked to block 1
    assert chain[2]["chain_index"] == 2
    assert chain[2]["prev_hash"] == chain[1]["chain_hash"]

    # Verification passes
    valid, err = verify_evidence_chain(chain)
    assert valid is True
    assert err is None


def test_evidence_hash_chain_detects_tampering() -> None:
    """Any modification to chained content or links breaks verification."""
    items: list[dict[str, Any]] = [
        {"category": "test", "name": "test_auth", "outcome": "passed"},
        {"category": "artifact", "path": "build/app.bin", "size": 1024},
    ]
    chain = build_evidence_chain(items)

    # 1. Tamper with content in block 0
    tampered_chain = [dict(c) for c in chain]
    tampered_chain[0]["outcome"] = "failed"
    valid, err = verify_evidence_chain(tampered_chain)
    assert valid is False
    assert "content tampered" in str(err)

    # 2. Tamper with chain_hash in block 0
    tampered_chain2 = [dict(c) for c in chain]
    tampered_chain2[0]["chain_hash"] = "deadbeef" * 8
    valid2, err2 = verify_evidence_chain(tampered_chain2)
    assert valid2 is False
    assert "chain_hash mismatch" in str(err2)

    # 3. Tamper with prev_hash in block 1
    tampered_chain3 = [dict(c) for c in chain]
    tampered_chain3[1]["prev_hash"] = "0" * 64
    valid3, err3 = verify_evidence_chain(tampered_chain3)
    assert valid3 is False
    assert "prev_hash mismatch" in str(err3)


def test_evidence_hash_chain_empty() -> None:
    """Empty chain trivially verifies."""
    valid, err = verify_evidence_chain([])
    assert valid is True
    assert err is None
