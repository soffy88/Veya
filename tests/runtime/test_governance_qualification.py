import asyncio

import pytest

from runtime.execution.durable import DurableExecutionError
from server.governance_store import governance_store
from server.user_control import (
    activate,
    deactivate,
    resolve_approval,
    set_freeze,
    user_control_policy,
)


@pytest.fixture
async def active_session():
    import uuid

    sid = "test_qual_" + uuid.uuid4().hex[:8]
    tokens = activate(mode="agent", require_approval=True, session_id=sid)
    await governance_store.init_state(sid, "agent", True)
    yield sid
    deactivate(tokens)


@pytest.mark.asyncio
async def test_action_gateway_revalidation(active_session):
    # Start policy evaluation (which waits for approval)
    kwargs = {"path": "/tmp/outside"}

    # Task to simulate user control evaluation
    eval_task = asyncio.create_task(user_control_policy("write_file", kwargs, "test"))

    # Allow some time for it to register pending
    await asyncio.sleep(0.1)

    # During the wait, we freeze the session to an unrelated directory
    await set_freeze(active_session, allow="some_other_dir", root="/tmp")

    # We then approve the request!
    # Get request ID
    state = await governance_store.get_state(active_session)
    assert state is not None

    # We will just fetch using the new by-hash method
    import hashlib
    import json

    from server.user_control import _safe_args

    args_json = json.dumps(_safe_args(kwargs), sort_keys=True)
    req_hash = hashlib.sha256(f"{active_session}:write_file:{args_json}".encode()).hexdigest()
    record = await governance_store.get_approval_by_hash(active_session, req_hash)

    # Resolve
    await resolve_approval(record.request_id, approved=True)

    # Gather evaluation task
    result = await eval_task
    # Should be blocked by freeze even though approved!
    assert "freeze applied during approval wait" in result or "writes locked" in result


@pytest.mark.asyncio
async def test_fail_closed_on_db_failure(active_session, monkeypatch):
    kwargs = {"path": "/tmp/x"}

    # Mock DB failure
    async def mock_get_state(*args, **kwargs):
        raise DurableExecutionError("DB_DOWN", "Simulated failure")

    monkeypatch.setattr(governance_store, "get_state", mock_get_state)
    monkeypatch.setattr(governance_store, "get_approval_by_hash", mock_get_state)

    with pytest.raises(DurableExecutionError):
        await user_control_policy("write_file", kwargs, "test")


@pytest.mark.asyncio
async def test_hash_canonicalization_and_binding(active_session):
    # Test identical dicts with different insertion orders
    kwargs1 = {"a": 1, "b": 2, "path": "/tmp/a"}
    kwargs2 = {"path": "/tmp/a", "b": 2, "a": 1}

    import hashlib
    import json

    from server.user_control import _safe_args

    args_json1 = json.dumps(_safe_args(kwargs1), sort_keys=True)
    args_json2 = json.dumps(_safe_args(kwargs2), sort_keys=True)
    assert args_json1 == args_json2

    req_hash1 = hashlib.sha256(f"{active_session}:write_file:{args_json1}".encode()).hexdigest()

    # Approve kwargs1
    eval_task1 = asyncio.create_task(user_control_policy("write_file", kwargs1, "test"))
    await asyncio.sleep(0.1)
    record = await governance_store.get_approval_by_hash(active_session, req_hash1)
    await resolve_approval(record.request_id, approved=True)
    res1 = await eval_task1
    assert res1 is None  # Allowed

    # Replay kwargs1 immediately allowed without wait
    res_replay = await user_control_policy("write_file", kwargs1, "test")
    assert res_replay is None

    # kwargs2 is identical in meaning, should also be allowed instantly
    res_replay2 = await user_control_policy("write_file", kwargs2, "test")
    assert res_replay2 is None

    # Different args -> not allowed
    kwargs3 = {"a": 1, "path": "/tmp/b"}
    eval_task3 = asyncio.create_task(user_control_policy("write_file", kwargs3, "test"))
    await asyncio.sleep(0.1)
    # the task is pending, not allowed immediately
    assert not eval_task3.done()
    eval_task3.cancel()
