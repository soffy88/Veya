"""Archived Hicode tests, split out of `tests/remote/test_direct_fast_path.py` when Hicode was retired.

Kept for historical reference only; not collected by the product suite.
"""

async def test_direct_hicode_worker_identity_and_progress(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    monkeypatch.setenv("HICODE_REASONIX_MODEL", "test-model")
    monkeypatch.setenv("HICODE_REASONIX_BASE_URL", "http://opencode.test/v1")
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))

    async def fake_execute_hicode_core(
        task,
        workspace=None,
        max_steps=0,
        timeout_sec=0,
        session_id=None,
        continue_=False,
        on_event=None,
        force_cli=False,
        on_process=None,
    ):
        if on_event:
            on_event({"stage": "planning", "tool": None, "detail": "planning"})
            on_event({"stage": "executing", "tool": "write_file", "detail": "write_file brief"})
            on_event(
                {"stage": "executing", "tool": "write_file", "detail": "write_file 完成 (3ms)"}
            )
            on_event({"stage": "stats", "tool": None, "detail": "tokens in=1 out=2"})
        return "done: created file"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake_execute_hicode_core)
    auth = RemoteAuth()
    _, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(tmp_path)])
    audit = RemoteAudit()
    adapter = RemoteToolAdapter(None, redact=audit.redact, execution_store=ExecutionStore(None))
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=4),
        audit=audit,
        adapter=adapter,
    )
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "hicode.execute", {"task": "x"})
    assert envelope["ok"] is True, envelope
    assert envelope["execution_id"].startswith("ex_")
    assert envelope["result"]["accepted"] is True
    assert envelope["result"]["execution_mode"] == "direct_hicode"
    assert envelope["result"]["worker_type"] == "HICODE"
    assert envelope["result"]["orchestrator"] == "none"
    final = await wait_for_phase(
        gateway, secret, session, envelope["execution_id"], {"COMPLETED"}, timeout=15
    )
    assert final["worker_type"] == "HICODE"
    assert final["model"] == "test-model"
    assert final["model_provider"] == "opencode-go"
    assert final["model_request_count"] >= 1
    assert final["tool_call_count"] >= 1
    kinds = [event["kind"] for event in final["recent_events"]]
    assert "MODEL_REQUEST_STARTED" in kinds
    assert "MODEL_REQUEST_COMPLETED" in kinds
    assert "TOOL_STARTED" in kinds
    assert "TOOL_COMPLETED" in kinds
    assert "CHECKPOINT" in kinds
    assert final["worker_workspace"] and ".veya/worktrees/task-" in final["worker_workspace"]
    assert final["worker_heartbeat"] in {"HEALTHY", "STALE", "UNKNOWN"}
    assert final["status"] == "COMPLETED"
async def test_direct_hicode_rejects_auto_router(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    gateway, secret, _ = make_hicode_gateway(tmp_path, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway, secret, session, "hicode.execute", {"task": "x", "execution_mode": "auto"}
    )
    assert envelope["ok"] is False
    assert envelope["error_code"] == "INVALID_ARGUMENT"
async def test_hicode_cancel_propagates_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    cancelled = asyncio.Event()

    async def fake(task, workspace=None, on_event=None, **kw):
        if on_event:
            on_event({"stage": "planning", "tool": None, "detail": "planning"})
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "done"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway, secret, _ = make_hicode_gateway(
        tmp_path, ExecutionStore(None), heartbeat_interval_s=0.05
    )
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "hicode.execute", {"task": "x"})
    execution_id = envelope["execution_id"]
    for _ in range(300):
        current = await status(gateway, secret, session, execution_id)
        if current["model_in_flight"] is True:
            break
        await asyncio.sleep(0.02)
    assert current["model_in_flight"] is True
    first = await call_tool(
        gateway, secret, session, "process.cancel", {"execution_id": execution_id}
    )
    assert first["result"]["phase"] == "CANCELLED"
    assert cancelled.is_set()
    second = await call_tool(
        gateway, secret, session, "process.cancel", {"execution_id": execution_id}
    )
    assert second["ok"] is True
    assert second["result"]["phase"] == "CANCELLED"
async def test_hicode_llm_inflight_heartbeat(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    started = asyncio.Event()

    async def fake(task, workspace=None, on_event=None, **kw):
        if on_event:
            on_event({"stage": "planning", "tool": None, "detail": "planning"})
        started.set()
        await asyncio.sleep(2)
        return "done"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway, secret, _ = make_hicode_gateway(
        tmp_path, ExecutionStore(None), heartbeat_interval_s=0.05
    )
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "hicode.execute", {"task": "x"})
    execution_id = envelope["execution_id"]
    await asyncio.wait_for(started.wait(), timeout=5)
    first = await status(gateway, secret, session, execution_id)
    await asyncio.sleep(0.25)
    second = await status(gateway, secret, session, execution_id)
    assert second["model_in_flight"] is True
    assert second["status"] == "RUNNING"
    assert second["worker_heartbeat"] == "HEALTHY"
    assert second["heartbeat_at"] > first["heartbeat_at"]
    await wait_for_phase(gateway, secret, session, execution_id, {"COMPLETED"}, timeout=10)
async def test_hicode_disconnect_reconnect(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))
    store = ExecutionStore(tmp_path / "exec")
    auth = RemoteAuth()
    _, secret = auth.issue("tester", permissions=PERMS, workspaces=[str(tmp_path)])
    release = asyncio.Event()

    async def fake(task, workspace=None, on_event=None, **kw):
        if on_event:
            on_event({"stage": "planning", "tool": None, "detail": "planning"})
        await release.wait()
        return "persisted hicode result"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway_a, _, _ = make_hicode_gateway(
        tmp_path, store, auth=auth, secret=secret, heartbeat_interval_s=0.05
    )
    session_a = await initialize(gateway_a, secret)
    envelope = await call_tool(gateway_a, secret, session_a, "hicode.execute", {"task": "x"})
    execution_id = envelope["execution_id"]
    gateway_b, _, _ = make_hicode_gateway(
        tmp_path, store, auth=auth, secret=secret, heartbeat_interval_s=0.05
    )
    session_b = await initialize(gateway_b, secret)
    observed = await status(gateway_b, secret, session_b, execution_id)
    assert observed["execution_id"] == execution_id
    assert observed["execution_mode"] == "direct_hicode"
    release.set()
    final = await wait_for_phase(
        gateway_b, secret, session_b, execution_id, {"COMPLETED"}, timeout=10
    )
    assert "persisted hicode result" in (final["result_summary"] or "")
async def test_hicode_worker_workspace_identity(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))

    async def fake(task, workspace=None, on_event=None, **kw):
        return f"workspace={workspace}"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway, secret, _ = make_hicode_gateway(tmp_path, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "hicode.execute", {"task": "x"})
    final = await wait_for_phase(
        gateway, secret, session, envelope["execution_id"], {"COMPLETED"}, timeout=10
    )
    assert final["worker_workspace"] and ".veya/worktrees/task-" in final["worker_workspace"]
    assert final["resolved_repo_root"] == str(tmp_path.resolve())
    assert final["worktree_repo_root"] == str(tmp_path.resolve())
    assert f"workspace={final['worker_workspace']}" in (final["result_summary"] or "")
async def test_unvalidated_path_cannot_reach_hicode(tmp_path: Path, monkeypatch) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", "/")
    calls: list[Any] = []

    async def fake(task, workspace=None, on_event=None, **kw):
        calls.append(workspace)
        return "should not run"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway, secret, _ = make_hicode_gateway(tmp_path, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway, secret, session, "hicode.execute", {"task": "x", "workspace": "/etc"}
    )
    assert envelope["ok"] is False
    assert envelope["error_code"] in {"WORKSPACE_DENIED", "AUTH_DENIED"}
    assert calls == []
async def test_hicode_empty_model_response_preserves_raw_failure_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))

    async def fake(*args, **kwargs):
        raise hicode_agent.HicodeExecutionError(
            "EMPTY_MODEL_RESPONSE",
            "provider returned an empty assistant response with no tool calls",
            raw_evidence={
                "response": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [],
                },
                "http_status": 200,
            },
        )

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway, secret, _ = make_hicode_gateway(tmp_path, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "hicode.execute", {"task": "x"})
    final = await wait_for_phase(
        gateway, secret, session, envelope["execution_id"], {"BLOCKED", "FAILED"}, timeout=10
    )
    assert final["failure_class"] == "EMPTY_MODEL_RESPONSE"
    assert final["provider_error_code"] == "EMPTY_MODEL_RESPONSE"
    assert "empty assistant response" in final["failure_message"]
    assert final["raw_failure_evidence"]["response"]["content"] is None
    assert final["raw_failure_evidence"]["response"]["tool_calls"] == []
    assert final["failure_history"][0]["failure_class"] == "EMPTY_MODEL_RESPONSE"
async def test_hicode_recovered_round_failure_keeps_history_and_truthful_progress(
    tmp_path: Path, monkeypatch
) -> None:
    make_workspace(tmp_path)
    from server import hicode_agent

    monkeypatch.setattr(hicode_agent, "DEFAULT_WORKSPACE", str(tmp_path))

    async def fake(task, workspace=None, on_event=None, **kwargs):
        assert on_event is not None
        on_event(
            {
                "stage": "provider_failure",
                "code": "ROUND_PROVIDER_ERROR",
                "detail": "round 0 provider failure",
                "round_index": 0,
                "raw_evidence": {"kind": "round_failed", "round": 0},
            }
        )
        on_event({"stage": "planning", "tool": None, "detail": "round 1"})
        for index in range(91):
            on_event(
                {
                    "stage": "executing",
                    "tool": "bash",
                    "detail": f"run {index}",
                }
            )
            on_event(
                {
                    "stage": "executing",
                    "tool": "bash",
                    "detail": "bash 完成",
                }
            )
        return "recovered successfully"

    monkeypatch.setattr(hicode_agent, "_execute_hicode_core", fake)
    gateway, secret, _ = make_hicode_gateway(tmp_path, ExecutionStore(None))
    session = await initialize(gateway, secret)
    envelope = await call_tool(gateway, secret, session, "hicode.execute", {"task": "x"})
    final = await wait_for_phase(
        gateway, secret, session, envelope["execution_id"], {"COMPLETED"}, timeout=10
    )
    assert final["status"] == "COMPLETED"
    assert final["failure_class"] is None
    assert final["failure_message"] is None
    assert final["provider_error_code"] is None
    assert final["tool_call_count"] == 91
    assert final["current_step"] == 91
    assert final["progress"]["unit"] == "tool_calls"
    assert final["progress"]["current"] == 91
    assert final["failure_history"][0]["failure_class"] == "ROUND_PROVIDER_ERROR"
    assert final["failure_history"][0]["recovered"] is True
    assert final["round_history"][0]["round_index"] == 0
    assert final["round_history"][0]["recovered"] is True
    assert all(item.get("detail") != "bash 完成" for item in final["failure_history"])
def test_hicode_result_classifier_detects_content_null_without_narration() -> None:
    from server.hicode_agent import _hicode_result_error

    result = {
        "type": "result",
        "is_error": False,
        "result": "",
        "num_turns": 1,
        "tool_calls": [],
        "response": {
            "role": "assistant",
            "content": None,
            "tool_calls": [],
        },
    }
    error = _hicode_result_error(result, raw_events=[], stderr_tail="", exit_code=0)
    assert error is not None
    assert error.code == "EMPTY_MODEL_RESPONSE"
    assert error.raw_evidence["result"]["response"]["content"] is None
    assert error.raw_evidence["result"]["response"]["tool_calls"] == []
def test_hicode_structured_failure_detector_rejects_narration() -> None:
    from server.hicode_agent import _structured_failure_event

    assert (
        _structured_failure_event(
            {"kind": "tool_result", "message": "bash 完成; assistant says failure"}
        )
        is None
    )
    failure = _structured_failure_event(
        {
            "kind": "round_failed",
            "round": 0,
            "error": {"code": "UPSTREAM_502", "message": "provider unavailable"},
        }
    )
    assert failure is not None
    assert failure["code"] == "UPSTREAM_502"
    assert failure["round_index"] == 0
