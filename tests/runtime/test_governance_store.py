import pytest

from server.governance_store import ApprovalRecord, GovernanceStore


@pytest.fixture
async def gov_store():
    import asyncio

    store = GovernanceStore()
    await store._ensure_started()
    repo = store._runtime.repository
    if repo.backend == "sqlite":

        def op(conn):
            conn.execute("DELETE FROM session_governance")
            conn.execute("DELETE FROM approval_records")

        await asyncio.to_thread(repo._sqlite_tx, op)
    else:

        async def op_pg(conn):
            await conn.execute("DELETE FROM session_governance")
            await conn.execute("DELETE FROM approval_records")

        await repo._pg_tx(op_pg)
    yield store


@pytest.mark.asyncio
async def test_session_governance_crud(gov_store: GovernanceStore):
    # test init
    state = await gov_store.init_state("test_sess2", "agent", True)
    assert state.session_id == "test_sess2"
    assert state.mode == "agent"
    assert state.require_approval is True

    # test update
    state.mode = "plan"
    state.require_approval = False
    success = await gov_store.set_state(state)
    assert success is True

    # test retrieve
    updated = await gov_store.get_state("test_sess2")
    assert updated.mode == "plan"
    assert updated.require_approval is False
    assert updated.revision == 2

    # test CAS concurrency failure
    state.revision = 1  # stale
    success = await gov_store.set_state(state)
    assert success is False  # should fail CAS


@pytest.mark.asyncio
async def test_approval_record_crud(gov_store: GovernanceStore):
    import time

    record = ApprovalRecord(
        request_id="req124",
        session_id="test_sess2",
        tool="write_file",
        tool_args={"path": "/tmp/a"},
        status="pending",
        decision_reason=None,
        request_hash="hash124",
        created_at=time.time(),
        updated_at=time.time(),
    )

    await gov_store.create_approval(record)

    fetched = await gov_store.get_approval("req124")
    assert fetched.status == "pending"
    assert fetched.tool == "write_file"
    assert fetched.request_hash == "hash124"

    success = await gov_store.resolve_approval("req124", "approved", "User clicked approve")
    assert success is True

    fetched_resolved = await gov_store.get_approval("req124")
    assert fetched_resolved.status == "approved"
    assert fetched_resolved.decision_reason == "User clicked approve"
