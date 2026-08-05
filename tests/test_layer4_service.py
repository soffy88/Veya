"""Layer 4 service tests — merged Agent OS gateway (veya/server + veya/server/app).

The legacy ServiceManifest / AssembledEngine layer was removed when all gateway
endpoints migrated to the Agent OS master brain (server.coordinator_master).
This file now covers the surviving layer-4 surfaces:

- element registry tools (ELEMENT_ALIASES / resolve_element / element_status)
- decision-trail persistence + session ids
- IM gateways (feishu / slack) routed through the master brain
- unified gateway endpoints (agent run/stream/verify/swarm/steer/history,
  mcp/tools, kanban, sandbox) plus Agent OS surfaces (master/chat, automata)
"""

from __future__ import annotations

import pytest

from veya.im.pseudo import PseudoAnonymizer, anonymize_user_id
from veya.server.manifests import (
    ELEMENT_ALIASES,
    element_status,
    load_decision_trail,
    new_session_id,
    resolve_element,
    save_decision_trail,
)

# ---------------------------------------------------------------------------
# Element registry tools
# ---------------------------------------------------------------------------


def test_resolve_element_never_raises():
    for spec in (
        "oprim.llm_chat_call",
        "oskill.mcp_tool_route",
        "omodul.sandbox_execution_workflow",
        "obase.cost_tracker",
        "obase.pseudo_anonymizer",
    ):
        resolve_element(spec)  # must not raise


def test_resolve_element_unknown_spec_returns_none():
    assert resolve_element("oprim.does_not_exist") is None


def test_element_aliases_cover_oservi_specs():
    for spec in ("oservi.agentic_loop", "oservi.dag_orchestrator"):
        assert spec in ELEMENT_ALIASES


ULTIMATE_SPECS = [
    "obase.treesitter_indexer",
    "obase.hitl_signal_bus",
    "obase.mcp_server",
    "obase.zeromq_bus",
    "obase.durable_lease_queue",
    "obase.zero_egress_sandbox",
    "obase.livekit_webrtc",
    "obase.vad_pipeline",
    "obase.signed_token_session",
    "obase.media_scraper",
    "obase.debounced_memory_queue",
    "obase.harness_bridge",
    "oprim.ast_extract_symbols",
    "oprim.hitl_wait_approval",
    "oprim.mcp_register_tool",
    "oprim.agent_prompt_synthesize",
    "oprim.git_worktree_merge",
    "oprim.tmux_pane_create",
    "oprim.kanban_task_update",
    "oprim.stt_transcribe_stream",
    "oprim.tts_synthesize_stream",
    "oprim.frontend_tool_forward",
    "oprim.soul_config_rewrite",
    "oprim.replay_step_record",
    "oprim.media_content_parse",
    "oprim.media_publish_post",
    "oprim.support_bundle_pack",
    "oskill.repomap_gen",
    "oskill.hitl_instruction_steer",
    "oskill.mcp_schema_adapter",
    "oskill.deep_research_tree",
    "oskill.dag_visual_layout",
    "oskill.leader_worker_dispatch",
    "oskill.contextual_reschedule",
    "oskill.voice_interruption_handler",
    "oskill.soul_self_evolution",
    "oskill.worktree_conflict_resolve",
    "oskill.harness_uniform_route",
    "omodul.hitl_approval_workflow",
    "omodul.mcp_tool_export_workflow",
    "omodul.nl_agent_synthesis_workflow",
    "omodul.swarm_collaborative_workflow",
    "omodul.durable_lease_task_workflow",
    "omodul.realtime_voice_agent_workflow",
    "omodul.content_media_pipeline_workflow",
    "oservi.steerable_agentic_loop",
    "oservi.swarm_orchestrator",
    "oservi.realtime_media_loop",
]


def test_element_aliases_contains_all_ultimate_specs():
    for spec in ULTIMATE_SPECS:
        assert spec in ELEMENT_ALIASES, f"missing alias for {spec}"


def test_resolve_ultimate_specs_never_raises():
    for spec in ULTIMATE_SPECS:
        if spec.startswith("oservi."):
            continue
        resolve_element(spec)  # must never raise


def test_element_status_covers_all_specs():
    status = element_status()
    assert len(status) == len(ELEMENT_ALIASES)
    assert all(v in ("resolved", "unavailable") for v in status.values())


# ---------------------------------------------------------------------------
# Decision trails + session ids
# ---------------------------------------------------------------------------


def test_new_session_id_unique():
    assert new_session_id() != new_session_id()


def test_decision_trail_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("veya.server.manifests.Path.home", lambda: tmp_path)
    sid = new_session_id()
    save_decision_trail(sid, [{"event": "a"}, {"event": "b"}])
    steps = load_decision_trail(sid)
    assert [s["event"] for s in steps] == ["a", "b"]
    assert load_decision_trail("missing") == []


# ---------------------------------------------------------------------------
# Pseudo-anonymizer
# ---------------------------------------------------------------------------


def test_pseudo_anonymizer_stable_and_non_pii():
    anon = PseudoAnonymizer(secret="unit-test-secret")
    a1 = anon.anonymize("student-001")
    a2 = anon.anonymize("student-001")
    b = anon.anonymize("student-002")
    assert a1 == a2  # stable
    assert a1 != b  # distinct users → distinct tokens
    assert a1.startswith("u_")
    assert "student" not in a1 and "001" not in a1  # no PII leak


def test_anonymize_user_id_one_shot():
    assert anonymize_user_id("u-1") == anonymize_user_id("u-1")
    assert anonymize_user_id("u-1") != anonymize_user_id("u-2")


# ---------------------------------------------------------------------------
# IM gateways (routed through the master brain)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feishu_gateway_acks_and_anonymizes():
    from veya.im.feishu import FeishuGateway

    gw = FeishuGateway()
    ack = await gw.handle_event(
        {
            "event": {
                "message": {"content": '{"text":"你好"}'},
                "sender": {"sender_id": {"open_id": "ou_123"}},
                "chat_id": "oc_1",
            }
        }
    )
    assert ack["ok"] is True
    assert ack["user_ref"].startswith("u_")
    assert "ou_123" not in ack["user_ref"]


@pytest.mark.asyncio
async def test_slack_gateway_acks_and_challenge():
    from veya.im.slack import SlackGateway

    gw = SlackGateway()
    ack = await gw.handle_event(
        {"event": {"type": "message", "text": "hello", "user": "U123", "channel": "C1"}}
    )
    assert ack["ok"] is True
    assert ack["user_ref"].startswith("u_")
    # url_verification challenge passthrough
    ch = await gw.handle_event({"type": "url_verification", "challenge": "ch-abc"})
    assert ch == {"challenge": "ch-abc"}


# ---------------------------------------------------------------------------
# Unified gateway endpoints (merged Agent OS app)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gateway_client():
    from fastapi.testclient import TestClient

    from veya.server.app import app

    with TestClient(app) as client:
        yield client


def test_gateway_master_brain_endpoints(gateway_client):
    r = gateway_client.get("/health")
    assert r.status_code == 200
    r = gateway_client.get("/master/tools")
    assert r.status_code == 200
    tools = r.json()["tools"]
    assert len(tools) >= 5


def test_gateway_agent_run_text_and_task_contracts(gateway_client):
    r = gateway_client.post(
        "/api/v1/agent/run", json={"text": "你好", "session_id": "t1"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("success", "failed")
    assert body["session_id"]

    r2 = gateway_client.post(
        "/api/v1/agent/run", json={"task": "检查一下", "user_id": "user_99", "mode": "dry_run"}
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "dry_run"
    assert r2.json()["user_ref"].startswith("u_")

    r3 = gateway_client.post("/api/v1/agent/run", json={"task": "真实任务"})
    assert r3.status_code == 200
    assert r3.json()["session_id"]


def test_gateway_agent_stream_sse(gateway_client):
    with gateway_client.stream(
        "POST", "/api/v1/agent/stream", json={"text": "流式测试"}
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        frames = [ln for ln in resp.iter_lines() if ln.startswith("data: ")]
        assert frames, "expected SSE frames"
        assert frames[-1] == "data: [DONE]"


def test_gateway_agent_verify(gateway_client):
    r = gateway_client.post("/api/v1/agent/verify", json={"statement": "1+1=2"})
    assert r.status_code == 200
    body = r.json()
    assert body["statement"] == "1+1=2"
    assert body["session_id"]


def test_gateway_agent_swarm(gateway_client):
    r = gateway_client.post(
        "/api/v1/agent/swarm", json={"task": "做一个简单的调研", "max_workers": 1}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body["session_id"]


def test_gateway_agent_steer(gateway_client):
    r = gateway_client.post(
        "/api/v1/agent/steer",
        json={"session_id": "s1", "action": "steer", "instruction": "换个方向"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_gateway_mcp_tools(gateway_client):
    body = gateway_client.get("/api/v1/mcp/tools").json()
    assert body["jsonrpc"] == "2.0"
    assert body["result"]["tools"]
    assert body["meta"]["source"] == "Agent OS master_tools"


def test_gateway_kanban_persisted(gateway_client, tmp_path, monkeypatch):
    monkeypatch.setattr("veya.server.app.Path.home", lambda: tmp_path)
    r = gateway_client.post("/api/v1/kanban", json={"action": "create", "board_name": "B"})
    assert r.status_code == 200
    r2 = gateway_client.post("/api/v1/kanban", json={"action": "get", "board_id": "default"})
    assert r2.status_code == 200


def test_gateway_sandbox(gateway_client):
    r = gateway_client.post(
        "/api/v1/sandbox/execute", json={"command": "echo ok", "time_limit": 10}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "success"


def test_gateway_history_missing_returns_empty(gateway_client):
    r = gateway_client.get("/api/v1/agent/history/does-not-exist")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_gateway_automata(gateway_client):
    r = gateway_client.get("/automata/jobs")
    assert r.status_code == 200
    assert "jobs" in r.json()


def test_gateway_vault(gateway_client):
    r = gateway_client.get("/vault/secrets")
    assert r.status_code == 200
    assert "vault_ids" in r.json()


# ---------------------------------------------------------------------------
# CLI (master brain runner)
# ---------------------------------------------------------------------------


def test_cli_run_dry_run_exits_zero(capsys):
    from veya.cli.main import main

    rc = main(["run", "做一次简单的代码测试", "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "dry-run: OK" in captured.out


def test_cli_run_real_executes(capsys):
    from veya.cli.main import main

    rc = main(["run", "做一次简单的代码测试"])
    captured = capsys.readouterr()
    assert rc in (0, 1)
    assert "cost=" in captured.out


def test_cli_history_missing_session(tmp_path, monkeypatch):
    from veya.cli.main import main

    monkeypatch.setattr("veya.server.manifests.Path.home", lambda: tmp_path)
    rc = main(["history", "does-not-exist"])
    assert rc == 1
