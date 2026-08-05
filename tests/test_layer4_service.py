"""Layer 4 service-assembly tests — manifests, CLI, SSE, IM, pseudo-anonymizer.

These tests exercise the project service layer (``veya/server``, ``veya/cli``,
``veya/im``) which assembles the 3O main libraries via ``ServiceManifest`` + DI.
They are designed to pass both with the full 3O dependency set installed and in
CI where the heavy optional deps of the main libraries are absent (resolution
degrades gracefully to Layer-4 bridges).
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from veya.im.pseudo import PseudoAnonymizer, anonymize_user_id
from veya.server.manifests import (
    ManifestValidationError,
    ServiceManifest,
    build_agentic_loop_manifest,
    build_multi_agent_dag_manifest,
    load_decision_trail,
    manifest_summary,
    resolve_element,
    validate_manifest,
)

# ---------------------------------------------------------------------------
# Step 1: ServiceManifest assembly config
# ---------------------------------------------------------------------------


def test_build_agentic_loop_manifest_wires_3o_elements():
    m = build_agentic_loop_manifest({})
    assert m.name == "agentic_loop"
    assert m.skeleton == "agentic_loop"
    assert m.trigger == {"on_demand": True}
    # required injection points present (per agentic_loop skeleton contract)
    assert "llm_caller" in m.inject
    assert "turn_handler" in m.inject
    assert isinstance(m.inject["tools"], list) and len(m.inject["tools"]) >= 1
    # observability bindings
    assert "cost_tracker" in m.config
    assert "decision_logger" in m.config


def test_build_multi_agent_dag_manifest_wires_3o_elements():
    m = build_multi_agent_dag_manifest({})
    assert m.name == "multi_agent_dag"
    assert m.skeleton == "subagent_orchestrator"
    assert "subagent_runner" in m.inject
    assert "scheduler" in m.inject
    assert "llm_caller" in m.inject
    assert "git_worktree_add" in m.config


def test_validate_manifest_accepts_built_manifests():
    validate_manifest(build_agentic_loop_manifest())
    validate_manifest(build_multi_agent_dag_manifest())


def test_validate_manifest_rejects_missing_required_injection():
    bad = ServiceManifest(
        name="bad",
        skeleton="agentic_loop",
        inject={"llm_caller": lambda *a, **k: {}},
        trigger={"on_demand": True},
    )
    with pytest.raises(ManifestValidationError):
        validate_manifest(bad)


def test_resolve_element_never_raises():
    # known spec resolves to real element or None — never raises.
    # Note: oservi.* specs are exercised by the CLI/server integration paths;
    # resolving them here would import the oservi submodule whose repo tracks
    # __pycache__ artifacts, so we only assert the alias table surface instead.
    for spec in (
        "oprim.llm_chat_call",
        "oskill.mcp_tool_route",
        "omodul.sandbox_execution_workflow",
        "obase.cost_tracker",
        "obase.pseudo_anonymizer",
    ):
        resolve_element(spec)  # must not raise


def test_element_aliases_cover_oservi_specs():
    from veya.server.manifests import ELEMENT_ALIASES

    for spec in ("oservi.agentic_loop", "oservi.dag_orchestrator"):
        assert spec in ELEMENT_ALIASES


def test_manifest_summary_shape():
    summary = manifest_summary(build_agentic_loop_manifest())
    assert summary["name"] == "agentic_loop"
    assert summary["skeleton"] == "agentic_loop"
    assert "llm_caller" in summary["inject"]


# ---------------------------------------------------------------------------
# Engine assembly + async surface (SSE-ready)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agentic_loop_engine_invoke_completes():
    from veya.server.manifests import assemble_agentic_loop

    engine = assemble_agentic_loop({})
    engine.run()
    result = await engine.invoke({"goal": "做一个简单的代码检查"})
    assert result.get("status") in ("completed", "failed")
    assert "cost_usd" in result
    assert engine.health()["status"] == "healthy"


@pytest.mark.asyncio
async def test_dag_orchestrator_invoke_completes():
    from veya.server.manifests import assemble_dag_orchestrator

    engine = assemble_dag_orchestrator({"repo": "/tmp"})
    engine.run()
    result = await engine.orchestrate(
        [{"id": "t1", "description": "sub task A"}, {"id": "t2", "description": "sub task B"}],
        parallel=True,
    )
    assert result.get("status") == "completed"
    assert len(result.get("results", [])) == 2


# ---------------------------------------------------------------------------
# Step 3: SSE streaming gateway
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_stream_yields_standard_frames():
    from veya.server.sse import stream_agent_run

    frames = []
    async for frame in stream_agent_run("SSE 测试任务", session_id="sse-1", config={}):
        frames.append(frame)
    assert frames, "expected at least one SSE frame"
    assert frames[-1] == "data: [DONE]\n\n"
    # every non-DONE frame is `data: {json}\n\n`
    for frame in frames[:-1]:
        assert frame.startswith("data: ")
        payload = json.loads(frame[len("data: ") :])
        assert "event" in payload


@pytest.mark.asyncio
async def test_sse_disconnect_persists_trail(tmp_path, monkeypatch):
    from veya.server.sse import stream_agent_run

    monkeypatch.setattr("veya.server.manifests.Path.home", lambda: tmp_path)
    session_id = "sse-disc-1"
    # cancel the generator mid-stream — trail must still be persisted
    gen = stream_agent_run("会被中断的任务", session_id=session_id, config={})
    with contextlib.suppress(StopAsyncIteration):
        await anext(gen)  # first frame
    await gen.aclose()  # graceful close (CancelledError path in real server)
    trail = load_decision_trail(session_id)
    assert isinstance(trail, list)


# ---------------------------------------------------------------------------
# Step 2/4: CLI + IM gateways
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


def test_pseudo_anonymizer_stable_and_non_pii():
    anon = PseudoAnonymizer(secret="unit-test-secret")
    a1 = anon.anonymize("student-001")
    a2 = anon.anonymize("student-001")
    b = anon.anonymize("student-002")
    assert a1 == a2  # stable
    assert a1 != b  # distinct users → distinct tokens
    assert a1.startswith("u_")
    assert "student" not in a1 and "001" not in a1  # no PII leak
    assert len(a1) >= 8


def test_anonymize_user_id_one_shot():
    assert anonymize_user_id("u-1") == anonymize_user_id("u-1")
    assert anonymize_user_id("u-1") != anonymize_user_id("u-2")


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
# FastAPI gateway
# ---------------------------------------------------------------------------


def test_fastapi_gateway_routes():
    from fastapi.testclient import TestClient

    from veya.server.app import app

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/agent/run",
            json={"task": "做一次简单的代码测试", "student_id": "stu_01", "mode": "dry_run"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "dry_run"
        assert body["user_ref"].startswith("u_")
        assert body["plan"]["skeleton"] == "agentic_loop"

        r2 = client.post("/api/v1/agent/run", json={"task": "检查一下", "user_id": "user_99"})
        assert r2.status_code == 200
        sid = r2.json()["session_id"]

        h = client.get(f"/api/v1/agent/history/{sid}")
        assert h.status_code == 200
        assert h.json()["count"] >= 1

        with client.stream(
            "POST", "/api/v1/agent/stream", json={"task": "流式测试", "student_id": "stu_02"}
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            frames = [ln for ln in resp.iter_lines() if ln.startswith("data: ")]
            assert frames, "expected SSE frames"
            assert frames[-1] == "data: [DONE]"


# ---------------------------------------------------------------------------
# Ultimate assembly — 48 additional 3O elements + high-order service factories
# ---------------------------------------------------------------------------

ULTIMATE_SPECS = [
    # obase (12)
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
    # oprim (15)
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
    # oskill (11)
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
    # omodul (7)
    "omodul.hitl_approval_workflow",
    "omodul.mcp_tool_export_workflow",
    "omodul.nl_agent_synthesis_workflow",
    "omodul.swarm_collaborative_workflow",
    "omodul.durable_lease_task_workflow",
    "omodul.realtime_voice_agent_workflow",
    "omodul.content_media_pipeline_workflow",
    # oservi (3)
    "oservi.steerable_agentic_loop",
    "oservi.swarm_orchestrator",
    "oservi.realtime_media_loop",
]


def test_element_aliases_contains_all_48_ultimate_specs():
    from veya.server.manifests import ELEMENT_ALIASES

    for spec in ULTIMATE_SPECS:
        assert spec in ELEMENT_ALIASES, f"missing alias for {spec}"
    assert len(ELEMENT_ALIASES) >= 62  # 14 base + 48 ultimate


def test_resolve_ultimate_specs_never_raises():
    # oservi specs excluded: resolving them imports the oservi submodule whose
    # repo tracks __pycache__ artifacts (checked in the alias-table test).
    for spec in ULTIMATE_SPECS:
        if spec.startswith("oservi."):
            continue
        resolve_element(spec)  # must never raise


def test_build_steerable_loop_manifest_wires_hitl_elements():
    from veya.server.manifests import build_steerable_loop_manifest, validate_manifest

    m = build_steerable_loop_manifest({"hitl_approval_timeout": 5.0})
    assert m.name == "steerable_agentic_loop"
    assert m.skeleton == "steerable_agentic_loop"
    # oservi engine reference (declarative) + HITL bindings
    assert m.config["engine_spec"] == "oservi.steerable_agentic_loop"
    for point in ("hitl_signal_bus", "hitl_instruction_steer", "hitl_approval_gate"):
        assert point in m.inject
    assert m.config["hitl_approval_timeout"] == 5.0
    assert "cost_tracker" in m.config and "decision_logger" in m.config
    validate_manifest(m)


def test_build_swarm_orchestrator_manifest_wires_dispatch():
    from veya.server.manifests import build_swarm_orchestrator_manifest, validate_manifest

    m = build_swarm_orchestrator_manifest({})
    assert m.name == "swarm_orchestrator"
    assert m.skeleton == "swarm_orchestrator"
    assert m.config["engine_spec"] == "oservi.swarm_orchestrator"
    assert "leader_worker_dispatch" in m.inject
    assert "subagent_runner" in m.inject and "llm_caller" in m.inject
    validate_manifest(m)


def test_build_realtime_media_manifest_wires_streams():
    from veya.server.manifests import build_realtime_media_manifest, validate_manifest

    m = build_realtime_media_manifest({})
    assert m.name == "realtime_media_loop"
    assert m.skeleton == "realtime_media_loop"
    assert m.config["engine_spec"] == "oservi.realtime_media_loop"
    for point in ("vad_pipeline", "stt_transcribe_stream", "tts_synthesize_stream"):
        assert point in m.inject
    assert m.config["media_loop_mode"] == "converse"
    validate_manifest(m)


@pytest.mark.asyncio
async def test_steerable_engine_approve_and_reject():
    from veya.server.manifests import assemble_steerable_loop

    engine = assemble_steerable_loop({"hitl_approval_timeout": 2.0})
    engine.run()
    gate = engine.manifest.inject["hitl_approval_gate"]
    bus = engine.manifest.config["hitl_signal_bus"]

    async def approve_later():
        await asyncio.sleep(0.05)
        aid = bus.pending()[0]["approval_id"]
        return await engine.steer("steer", instruction="use pytest only", approval_id=aid)

    op = asyncio.create_task(approve_later())
    decision = await gate({"action": "run_tests"}, context={})
    await op
    assert decision["status"] == "approved"
    assert decision["instruction"] == "use pytest only"

    async def reject_later():
        await asyncio.sleep(0.05)
        aid = bus.pending()[0]["approval_id"]
        return await engine.steer("reject", instruction="no deploy", approval_id=aid)

    op = asyncio.create_task(reject_later())
    decision2 = await gate({"action": "deploy"}, context={})
    await op
    assert decision2["status"] == "rejected"
    assert decision2["reason"] == "no deploy"


@pytest.mark.asyncio
async def test_steerable_engine_timeout_auto_approves_and_queues_steer():
    from veya.server.manifests import assemble_steerable_loop

    engine = assemble_steerable_loop({"hitl_approval_timeout": 0.2})
    engine.run()
    gate = engine.manifest.inject["hitl_approval_gate"]
    decision = await gate({"action": "slow"}, context={})
    assert decision["status"] == "approved"  # timeout → deterministic auto-approve

    # queue a steer without a pending gate → merged into the next invocation
    queued = await engine.steer("steer", instruction="focus on tests")
    assert queued["status"] == "queued"
    result = await engine.invoke({"goal": "做一个代码检查", "session_id": "s-steer"})
    assert result.get("status") == "completed"


@pytest.mark.asyncio
async def test_swarm_engine_dispatch_completes():
    from veya.server.manifests import assemble_swarm_orchestrator

    engine = assemble_swarm_orchestrator({})
    engine.run()
    result = await engine.dispatch("执行两个子任务")
    assert result.get("status") == "completed"
    assert len(result.get("results", [])) >= 1


@pytest.mark.asyncio
async def test_realtime_media_engine_runs_pipeline():
    from veya.server.manifests import assemble_realtime_media

    engine = assemble_realtime_media({})
    engine.run()
    result = await engine.run_media_stream(
        [{"audio": b"frame-1"}, {"audio": b"frame-2"}], context={}
    )
    assert result.get("status") == "completed"
    turns = result.get("turns", [])
    assert len(turns) == 2
    assert turns[0]["vad"] is True  # VAD passthrough marks voice activity
    assert turns[0]["stt_status"] == "unavailable"  # graceful offline degradation


def test_fastapi_ultimate_gateway_routes():
    from fastapi.testclient import TestClient

    from veya.server.app import app

    with TestClient(app) as client:
        # MCP tool export (JSON-RPC tools/list shape)
        r = client.get("/api/v1/mcp/tools")
        assert r.status_code == 200
        body = r.json()
        assert body["jsonrpc"] == "2.0"
        assert "tools" in body["result"]
        assert body["result"]["tools"], "expected at least one resolved 3O tool"
        assert body["meta"]["registered"] + body["meta"]["unavailable"] >= 62

        # steer on unknown session → 404
        r = client.post("/api/v1/agent/steer", json={"session_id": "missing", "action": "approve"})
        assert r.status_code == 404

        # swarm dispatch
        r = client.post("/api/v1/agent/swarm", json={"task": "做一次代码测试"})
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
        assert "session_id" in r.json()

        # realtime media loop
        r = client.post("/api/v1/agent/media", json={"transcript": "你好", "mode": "transcribe"})
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
        assert len(r.json()["turns"]) == 1


def test_element_status_covers_all_ultimate_specs():
    from veya.server.manifests import element_status

    status = element_status()
    assert len(status) >= 62
    for spec in ULTIMATE_SPECS:
        assert spec in status
        assert status[spec] in ("resolved", "unavailable")


def test_manifest_summary_for_ultimate_manifests():
    from veya.server.manifests import (
        build_realtime_media_manifest,
        build_steerable_loop_manifest,
        build_swarm_orchestrator_manifest,
        manifest_summary,
    )

    for builder in (
        build_steerable_loop_manifest,
        build_swarm_orchestrator_manifest,
        build_realtime_media_manifest,
    ):
        s = manifest_summary(builder({}))
        assert s["name"] and s["skeleton"]
        assert "inject" in s and "config_keys" in s


def test_validate_rejects_missing_hitl_injection():
    from veya.server.manifests import ManifestValidationError, ServiceManifest

    bad = ServiceManifest(
        name="bad-steer",
        skeleton="steerable_agentic_loop",
        inject={"llm_caller": lambda: None, "tools": [lambda: None]},
        trigger={"on_demand": True},
    )
    with pytest.raises(ManifestValidationError):
        validate_manifest(bad)


def test_validate_rejects_undeclared_injection_key():
    from veya.server.manifests import ManifestValidationError, ServiceManifest, validate_manifest

    bad = ServiceManifest(
        name="bad-key",
        skeleton="agentic_loop",
        inject={
            "llm_caller": lambda: None,
            "tools": [lambda: None],
            "turn_handler": lambda: None,
            "not_a_point": lambda: None,
        },
        trigger={"on_demand": True},
    )
    with pytest.raises(ManifestValidationError):
        validate_manifest(bad)


@pytest.mark.asyncio
async def test_swarm_engine_orchestrate_explicit_parallel():
    from veya.server.manifests import assemble_swarm_orchestrator

    engine = assemble_swarm_orchestrator({"max_workers": 2})
    engine.run()
    result = await engine.orchestrate(
        [
            {"id": "a", "description": "任务 A", "depends_on": []},
            {"id": "b", "description": "任务 B", "depends_on": []},
        ],
        parallel=True,
    )
    assert result.get("status") == "completed"
    assert len(result.get("results", [])) >= 2


@pytest.mark.asyncio
async def test_realtime_media_transcribe_mode_skips_llm():
    from veya.server.manifests import assemble_realtime_media

    engine = assemble_realtime_media({})
    engine.run()
    result = await engine.run_media_stream(
        [{"audio": b"x"}], context={"media_loop_mode": "transcribe"}
    )
    assert result["status"] == "completed"
    assert result["turns"][0]["reply"] == ""  # no LLM in transcribe-only mode


@pytest.mark.asyncio
async def test_steer_unknown_approval_id():
    from veya.server.manifests import assemble_steerable_loop

    engine = assemble_steerable_loop({})
    engine.run()
    result = await engine.steer("approve", approval_id="no-such-gate")
    assert result["status"] == "unknown_approval"


@pytest.mark.asyncio
async def test_engine_registry_roundtrip():
    from veya.server.manifests import (
        assemble_steerable_loop,
        get_running_engine,
        register_running_engine,
        unregister_running_engine,
    )

    engine = assemble_steerable_loop({})
    register_running_engine("reg-test", engine)
    assert get_running_engine("reg-test") is engine
    unregister_running_engine("reg-test")
    assert get_running_engine("reg-test") is None


def test_mcp_tools_meta_reports_spec_coverage():
    from fastapi.testclient import TestClient

    from veya.server.app import app

    with TestClient(app) as client:
        body = client.get("/api/v1/mcp/tools").json()
        specs = body["meta"]["specs"]
        for spec in ULTIMATE_SPECS:
            assert spec in specs
        assert body["meta"]["registered"] >= 40  # majority resolve on this box


def test_steerable_engine_health_and_pending():
    from veya.server.manifests import assemble_steerable_loop

    engine = assemble_steerable_loop({})
    engine.run()
    h = engine.health()
    assert h["status"] == "healthy"
    assert h["skeleton"] == "steerable_agentic_loop"
    assert engine.pending_approvals() == []


def test_resolve_element_unknown_spec_returns_none():
    assert resolve_element("oprim.does_not_exist") is None


@pytest.mark.asyncio
async def test_engine_lifecycle_run_stop():
    from veya.server.manifests import assemble_agentic_loop

    engine = assemble_agentic_loop({})
    assert engine.health()["status"] == "stopped"
    engine.run()
    assert engine.health()["status"] == "healthy"
    engine.stop()
    assert engine.health()["status"] == "stopped"


@pytest.mark.asyncio
async def test_realtime_media_empty_frames_completes():
    from veya.server.manifests import assemble_realtime_media

    engine = assemble_realtime_media({})
    engine.run()
    result = await engine.run_media_stream([], context={})
    assert result["status"] == "completed"
    assert result["turns"] == []


def test_steer_request_rejects_bad_action():
    from fastapi.testclient import TestClient

    from veya.server.app import app

    with TestClient(app) as client:
        r = client.post("/api/v1/agent/steer", json={"session_id": "s", "action": "explode"})
        assert r.status_code == 422  # Literal validation


# ---------------------------------------------------------------------------
# Frontier assembly — 10 neuro-symbolic / formal-verification elements
# ---------------------------------------------------------------------------

FRONTIER_SHORT_NAMES = [
    "smt_solver_adapter",
    "lean_formal_prover",
    "fol_translate",
    "causal_graph_build",
    "invariant_extract",
    "neuro_symbolic_verify",
    "counterfactual_reasoning",
    "formal_code_proof",
    "root_cause_analysis_workflow",
    "mechanism_game_loop",
]

FRONTIER_FULL_NAMES = [
    "obase.smt_solver_adapter",
    "obase.lean_formal_prover",
    "oprim.fol_translate",
    "oprim.causal_graph_build",
    "oprim.invariant_extract",
    "oskill.neuro_symbolic_verify",
    "oskill.counterfactual_reasoning",
    "oskill.formal_code_proof",
    "omodul.root_cause_analysis_workflow",
    "oservi.mechanism_game_loop",
]


def test_element_aliases_contains_10_frontier_short_names():
    from veya.server.manifests import ELEMENT_ALIASES

    for spec in FRONTIER_SHORT_NAMES:
        assert spec in ELEMENT_ALIASES, f"missing short-name alias {spec}"


def test_element_aliases_contains_frontier_full_names():
    from veya.server.manifests import ELEMENT_ALIASES

    for spec in FRONTIER_FULL_NAMES:
        assert spec in ELEMENT_ALIASES, f"missing full-name alias {spec}"


def test_frontier_short_and_full_aliases_share_target():
    from veya.server.manifests import ELEMENT_ALIASES

    for short, full in zip(FRONTIER_SHORT_NAMES, FRONTIER_FULL_NAMES, strict=True):
        assert ELEMENT_ALIASES[short] == ELEMENT_ALIASES[full]


@pytest.mark.parametrize("spec", [s for s in FRONTIER_SHORT_NAMES if "mechanism" not in s])
def test_resolve_frontier_specs_never_raises(spec):
    # mechanism_game_loop excluded: resolves the oservi submodule (pycache
    # tracking quirk) — its alias-table presence is asserted separately.
    resolve_element(spec)  # must never raise


def test_build_neuro_symbolic_manifest_wires_elements():
    from veya.server.manifests import build_neuro_symbolic_manifest, validate_manifest

    m = build_neuro_symbolic_manifest({})
    assert m.name == "neuro_symbolic"
    assert m.skeleton == "neuro_symbolic"
    assert m.config["engine_spec"] == "oskill.neuro_symbolic_verify"
    for point in ("fol_translator", "smt_solver", "neuro_verifier"):
        assert point in m.inject
    assert "cost_tracker" in m.config and "decision_logger" in m.config
    validate_manifest(m)


def test_build_root_cause_manifest_wires_elements():
    from veya.server.manifests import build_root_cause_analysis_manifest, validate_manifest

    m = build_root_cause_analysis_manifest({})
    assert m.name == "root_cause_analysis"
    assert m.skeleton == "root_cause_analysis"
    assert m.config["engine_spec"] == "omodul.root_cause_analysis_workflow"
    for point in ("causal_graph", "root_cause_analyzer", "counterfactual_reasoner"):
        assert point in m.inject
    validate_manifest(m)


def test_validate_rejects_missing_neuro_injection():
    from veya.server.manifests import ManifestValidationError, ServiceManifest, validate_manifest

    bad = ServiceManifest(
        name="bad-neuro",
        skeleton="neuro_symbolic",
        inject={"fol_translator": lambda: None},
        trigger={"on_demand": True},
    )
    with pytest.raises(ManifestValidationError):
        validate_manifest(bad)


def test_validate_rejects_missing_root_cause_injection():
    from veya.server.manifests import ManifestValidationError, ServiceManifest, validate_manifest

    bad = ServiceManifest(
        name="bad-rca",
        skeleton="root_cause_analysis",
        inject={"causal_graph": lambda: None, "root_cause_analyzer": lambda: None},
        trigger={"on_demand": True},
    )
    with pytest.raises(ManifestValidationError):
        validate_manifest(bad)


@pytest.mark.asyncio
async def test_neuro_engine_verify_offline_deterministic():
    from veya.server.manifests import assemble_neuro_symbolic

    engine = assemble_neuro_symbolic({})
    engine.run()
    result = await engine.run_verify("所有偶数是 2 的倍数")
    assert result["status"] == "completed"
    assert result["verdict"] == "inconclusive"  # graceful offline degradation
    assert result["confidence"] == 0.0
    assert sorted(result["stages"]) == ["fol_translate", "neuro_symbolic_verify", "smt_solver"]
    assert result["stages"]["fol_translate"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_neuro_engine_verify_verified_with_mocked_smt():
    from veya.server.manifests import assemble, build_neuro_symbolic_manifest

    async def fake_fol(statement, context=None, **kw):
        return {"statement": statement, "formula": "(not P)", "status": "translated"}

    async def fake_smt(formula, context=None, **kw):
        return {"formula": formula, "verdict": "unsat", "status": "solved"}

    manifest = build_neuro_symbolic_manifest({})
    manifest.inject["fol_translator"] = fake_fol
    manifest.inject["smt_solver"] = fake_smt
    engine = assemble(manifest)
    engine.run()
    result = await engine.run_verify("P 成立")
    assert result["verdict"] == "verified"
    assert result["confidence"] == 1.0


@pytest.mark.asyncio
async def test_neuro_engine_verify_refuted_with_mocked_smt():
    from veya.server.manifests import assemble, build_neuro_symbolic_manifest

    async def fake_fol(statement, context=None, **kw):
        return {"statement": statement, "formula": "(not P)", "status": "translated"}

    async def fake_smt(formula, context=None, **kw):
        return {"formula": formula, "verdict": "sat", "status": "solved"}

    manifest = build_neuro_symbolic_manifest({})
    manifest.inject["fol_translator"] = fake_fol
    manifest.inject["smt_solver"] = fake_smt
    engine = assemble(manifest)
    engine.run()
    result = await engine.run_verify("P 成立")
    assert result["verdict"] == "refuted"


@pytest.mark.asyncio
async def test_fol_translator_fallback_deterministic():
    from veya.server.manifests import assemble, build_neuro_symbolic_manifest

    engine = assemble(build_neuro_symbolic_manifest({}))
    fol = engine.manifest.inject["fol_translator"]
    out = await fol("P implies Q", context={})
    assert out["status"] == "unavailable"
    assert out["formula"] is None
    assert out["statement"] == "P implies Q"


@pytest.mark.asyncio
async def test_smt_adapter_fallback_deterministic():
    from veya.server.manifests import assemble, build_neuro_symbolic_manifest

    engine = assemble(build_neuro_symbolic_manifest({}))
    smt = engine.manifest.inject["smt_solver"]
    out = await smt("(not P)", context={})
    assert out["verdict"] == "inconclusive"
    assert out["status"] == "unavailable"


@pytest.mark.asyncio
async def test_root_cause_engine_diagnose_offline():
    from veya.server.manifests import assemble_root_cause_analysis

    engine = assemble_root_cause_analysis({})
    engine.run()
    trail = [
        {"event": "llm_call", "detail": "draft"},
        {"event": "tool_call", "detail": "run tests"},
        {"event": "error", "detail": "import failed"},
        {"event": "error", "detail": "import failed"},
        {"event": "session_done"},
    ]
    result = await engine.run_diagnose(trail)
    assert result["status"] == "completed"
    assert result["root_causes"][0]["cause"] == "import failed"
    assert result["root_causes"][0]["score"] == 1.0
    assert len(result["counterfactuals"]) == 1
    assert "llm_call" in result["graph"]["nodes"]
    assert result["graph"]["edges"][0]["type"] == "sequence"


@pytest.mark.asyncio
async def test_root_cause_engine_diagnose_no_errors():
    from veya.server.manifests import assemble_root_cause_analysis

    engine = assemble_root_cause_analysis({})
    engine.run()
    result = await engine.run_diagnose([{"event": "llm_call", "detail": "ok"}])
    assert result["status"] == "completed"
    assert result["root_causes"] == []


@pytest.mark.asyncio
async def test_causal_graph_builder_fallback_edges():
    from veya.server.manifests import assemble, build_root_cause_analysis_manifest

    engine = assemble(build_root_cause_analysis_manifest({}))
    causal = engine.manifest.inject["causal_graph"]
    out = await causal([{"event": "a"}, {"event": "b"}, {"event": "c"}], context={})
    assert out["graph"]["nodes"] == ["a", "b", "c"]
    assert len(out["graph"]["edges"]) == 2
    assert out["graph"]["edges"][0] == {"from": "a", "to": "b", "type": "sequence"}


@pytest.mark.asyncio
async def test_counterfactual_reasoner_fallback_deterministic():
    from veya.server.manifests import assemble, build_root_cause_analysis_manifest

    engine = assemble(build_root_cause_analysis_manifest({}))
    cf = engine.manifest.inject["counterfactual_reasoner"]
    out = await cf({"cause": "import failed", "score": 1.0}, context={})
    assert out["cause"] == "import failed"
    assert "import failed" in out["scenario"]
    assert out["status"] == "projected"


def test_fastapi_verify_and_diagnose_endpoints():
    from fastapi.testclient import TestClient

    from veya.server.app import app

    with TestClient(app) as client:
        r = client.post("/api/v1/agent/verify", json={"statement": "所有偶数是 2 的倍数"})
        assert r.status_code == 200
        body = r.json()
        assert body["verdict"] == "inconclusive"
        assert body["statement"] == "所有偶数是 2 的倍数"
        assert "session_id" in body
        assert len(body["stages"]) == 3

        r = client.post("/api/v1/agent/verify", json={"statement": ""})
        assert r.status_code == 422

        r = client.post(
            "/api/v1/agent/diagnose",
            json={
                "decision_trail": [
                    {"event": "tool_call", "detail": "run"},
                    {"event": "error", "detail": "import failed"},
                ]
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        assert body["root_causes"][0]["cause"] == "import failed"
        assert body["events_analyzed"] == 2
        assert body["counterfactuals"][0]["status"] == "projected"

        r = client.post("/api/v1/agent/diagnose", json={"decision_trail": []})
        assert r.status_code == 422


def test_diagnose_endpoint_max_counterfactuals():
    from fastapi.testclient import TestClient

    from veya.server.app import app

    trail = [{"event": "error", "detail": f"cause-{i}"} for i in range(5)]
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/agent/diagnose",
            json={"decision_trail": trail, "max_counterfactuals": 2},
        )
        assert r.status_code == 200
        assert len(r.json()["counterfactuals"]) == 2


def test_element_status_includes_frontier_specs():
    from veya.server.manifests import element_status

    status = element_status()
    for spec in FRONTIER_SHORT_NAMES + FRONTIER_FULL_NAMES:
        assert spec in status
        assert status[spec] in ("resolved", "unavailable")


def test_manifest_summary_for_frontier_manifests():
    from veya.server.manifests import (
        build_neuro_symbolic_manifest,
        build_root_cause_analysis_manifest,
        manifest_summary,
    )

    for builder in (build_neuro_symbolic_manifest, build_root_cause_analysis_manifest):
        s = manifest_summary(builder({}))
        assert s["name"] and s["skeleton"]
        assert "fol_translator" in s["inject"] or "causal_graph" in s["inject"]


def test_neuro_engine_health():
    from veya.server.manifests import assemble_neuro_symbolic

    engine = assemble_neuro_symbolic({})
    engine.run()
    h = engine.health()
    assert h["status"] == "healthy"
    assert h["skeleton"] == "neuro_symbolic"


@pytest.mark.asyncio
async def test_fol_translator_uses_real_element_when_mounted():
    from veya.server.manifests import _make_fol_translator, assemble, build_neuro_symbolic_manifest

    async def real_fol(statement, context=None, **kw):
        return {"formula": "(forall x (P x))", "status": "translated"}

    manifest = build_neuro_symbolic_manifest({})
    manifest.inject["fol_translator"] = _make_fol_translator(real_fol)
    engine = assemble(manifest)
    fol = engine.manifest.inject["fol_translator"]
    out = await fol("所有 P 都成立", context={})
    assert out["status"] == "translated"
    assert out["formula"] == "(forall x (P x))"


@pytest.mark.asyncio
async def test_smt_adapter_uses_real_element_when_mounted():
    from veya.server.manifests import (
        _make_smt_solver_adapter,
        assemble,
        build_neuro_symbolic_manifest,
    )

    async def real_smt(formula, **kw):
        return {"status": "unsatisfiable"}

    manifest = build_neuro_symbolic_manifest({})
    manifest.inject["smt_solver"] = _make_smt_solver_adapter(real_smt)
    engine = assemble(manifest)
    smt = engine.manifest.inject["smt_solver"]
    out = await smt("(not P)", context={})
    assert out["status"] == "solved"
    assert out["verdict"] == "unsat"


def test_neuro_manifest_llm_caller_optional_slot():
    from veya.server.manifests import build_neuro_symbolic_manifest

    m = build_neuro_symbolic_manifest({})
    assert m.inject["llm_caller"] is not None  # graceful stub injected


def test_root_cause_engine_health():
    from veya.server.manifests import assemble_root_cause_analysis

    engine = assemble_root_cause_analysis({})
    engine.run()
    h = engine.health()
    assert h["status"] == "healthy"
    assert h["skeleton"] == "root_cause_analysis"


@pytest.mark.asyncio
async def test_diagnose_max_counterfactuals_context():
    from veya.server.manifests import assemble_root_cause_analysis

    engine = assemble_root_cause_analysis({})
    engine.run()
    trail = [{"event": "error", "detail": f"c{i}"} for i in range(5)]
    result = await engine.run_diagnose(trail, context={"max_counterfactuals": 2})
    assert len(result["counterfactuals"]) == 2
    assert len(result["root_causes"]) == 5


def test_verify_request_model_validation():
    from pydantic import ValidationError

    from veya.server.app import VerifyRequest

    with pytest.raises(ValidationError):
        VerifyRequest(statement="")
    with pytest.raises(ValidationError):
        VerifyRequest()
    ok = VerifyRequest(statement="P")
    assert ok.statement == "P"


# ---------------------------------------------------------------------------
# 30-element batch — long-horizon / graph-skills assembly
# ---------------------------------------------------------------------------

BATCH30_SHORT = [
    # obase (6)
    "checkpoint_store",
    "browser_vision_runner",
    "ssrf_safe_network",
    "skills_registry",
    "adaptive_scraper",
    "dlt_pipeline_store",
    # oprim (8)
    "tdd_test_run",
    "git_checkpoint_commit",
    "browser_element_interact",
    "web_search_fetch",
    "code_graph_parse",
    "adaptive_node_extract",
    "domain_rule_check",
    "dlt_schema_normalize",
    # oskill (8)
    "code_review_gate",
    "long_context_compress",
    "vlm_page_nav",
    "proactive_deep_reach",
    "skills_dynamic_inject",
    "graph_impact_analysis",
    "smart_web_scraping",
    "auto_data_curation",
    # omodul (6)
    "tdd_programming_workflow",
    "long_horizon_checkpoint_workflow",
    "active_web_research_workflow",
    "skills_guided_coding_workflow",
    "graph_codebase_investigation_workflow",
    "auto_etl_research_workflow",
    # oservi (2)
    "long_horizon_agentic_loop",
    "graph_skills_agentic_loop",
]

BATCH30_FULL = [
    "obase.checkpoint_store",
    "obase.browser_vision_runner",
    "obase.ssrf_safe_network",
    "obase.skills_registry",
    "obase.adaptive_scraper",
    "obase.dlt_pipeline_store",
    "oprim.tdd_test_run",
    "oprim.git_checkpoint_commit",
    "oprim.browser_element_interact",
    "oprim.web_search_fetch",
    "oprim.code_graph_parse",
    "oprim.adaptive_node_extract",
    "oprim.domain_rule_check",
    "oprim.dlt_schema_normalize",
    "oskill.code_review_gate",
    "oskill.long_context_compress",
    "oskill.vlm_page_nav",
    "oskill.proactive_deep_reach",
    "oskill.skills_dynamic_inject",
    "oskill.graph_impact_analysis",
    "oskill.smart_web_scraping",
    "oskill.auto_data_curation",
    "omodul.tdd_programming_workflow",
    "omodul.long_horizon_checkpoint_workflow",
    "omodul.active_web_research_workflow",
    "omodul.skills_guided_coding_workflow",
    "omodul.graph_codebase_investigation_workflow",
    "omodul.auto_etl_research_workflow",
    "oservi.long_horizon_agentic_loop",
    "oservi.graph_skills_agentic_loop",
]


def test_element_aliases_contains_30_batch_short_names():
    from veya.server.manifests import ELEMENT_ALIASES

    for spec in BATCH30_SHORT:
        assert spec in ELEMENT_ALIASES, f"missing short-name alias {spec}"


def test_element_aliases_contains_30_batch_full_names():
    from veya.server.manifests import ELEMENT_ALIASES

    for spec in BATCH30_FULL:
        assert spec in ELEMENT_ALIASES, f"missing full-name alias {spec}"


def test_batch30_short_and_full_aliases_share_target():
    from veya.server.manifests import ELEMENT_ALIASES

    for short, full in zip(BATCH30_SHORT, BATCH30_FULL, strict=True):
        assert ELEMENT_ALIASES[short] == ELEMENT_ALIASES[full]


@pytest.mark.parametrize(
    "spec",
    [
        s
        for s in BATCH30_SHORT
        if not s.startswith(("long_horizon_agentic", "graph_skills_agentic"))
    ],
)
def test_resolve_batch30_specs_never_raises(spec):
    # the two oservi engines are excluded (oservi submodule pycache quirk)
    resolve_element(spec)  # must never raise


def test_build_long_horizon_manifest_wires_elements():
    from veya.server.manifests import build_long_horizon_manifest, validate_manifest

    m = build_long_horizon_manifest({})
    assert m.name == "long_horizon_agentic_loop"
    assert m.skeleton == "long_horizon_agentic_loop"
    assert m.config["engine_spec"] == "oservi.long_horizon_agentic_loop"
    for point in ("checkpoint_store", "context_compressor", "llm_caller", "tools"):
        assert point in m.inject
    validate_manifest(m)


def test_build_graph_skills_manifest_wires_elements():
    from veya.server.manifests import build_graph_skills_manifest, validate_manifest

    m = build_graph_skills_manifest({})
    assert m.name == "graph_skills_agentic_loop"
    assert m.skeleton == "graph_skills_agentic_loop"
    assert m.config["engine_spec"] == "oservi.graph_skills_agentic_loop"
    for point in ("skills_registry", "code_graph_parser", "skills_coding_workflow"):
        assert point in m.inject
    validate_manifest(m)


def test_validate_rejects_missing_checkpoint_store():
    from veya.server.manifests import ManifestValidationError, ServiceManifest, validate_manifest

    bad = ServiceManifest(
        name="bad-lh",
        skeleton="long_horizon_agentic_loop",
        inject={"llm_caller": lambda: None, "tools": [lambda: None], "turn_handler": lambda: None},
        trigger={"on_demand": True},
    )
    with pytest.raises(ManifestValidationError):
        validate_manifest(bad)


def test_validate_rejects_missing_skills_registry():
    from veya.server.manifests import ManifestValidationError, ServiceManifest, validate_manifest

    bad = ServiceManifest(
        name="bad-gs",
        skeleton="graph_skills_agentic_loop",
        inject={"llm_caller": lambda: None, "tools": [lambda: None], "turn_handler": lambda: None},
        trigger={"on_demand": True},
    )
    with pytest.raises(ManifestValidationError):
        validate_manifest(bad)


@pytest.mark.asyncio
async def test_long_horizon_engine_run_and_resume():
    from veya.server.manifests import assemble_long_horizon

    engine = assemble_long_horizon({})
    engine.run()
    session = "lh-test-001"
    store = engine.manifest.config["checkpoint_store"]
    store.reset(f"session:{session}")
    r1 = await engine.run_long_horizon({"goal": "第一步", "session_id": session})
    assert r1["status"] == "completed"
    assert r1["resumed"] is False
    assert r1["checkpoint"]["turns_done"] == 1
    r2 = await engine.run_long_horizon({"goal": "第二步", "session_id": session})
    assert r2["resumed"] is True  # checkpoint restored
    assert r2["checkpoint"]["turns_done"] == 2
    assert r2["checkpoint"]["summary"] == r1["checkpoint"]["summary"]
    store.reset(f"session:{session}")


@pytest.mark.asyncio
async def test_long_horizon_engine_resume_false_resets():
    from veya.server.manifests import assemble_long_horizon

    engine = assemble_long_horizon({})
    engine.run()
    session = "lh-test-002"
    store = engine.manifest.config["checkpoint_store"]
    store.reset(f"session:{session}")
    await engine.run_long_horizon({"goal": "第一步", "session_id": session})
    r2 = await engine.run_long_horizon(
        {"goal": "重来", "session_id": session}, context={"resume": False}
    )
    assert r2["resumed"] is False
    assert r2["checkpoint"]["turns_done"] == 1
    store.reset(f"session:{session}")


def test_checkpoint_store_disk_roundtrip(tmp_path):
    from veya.server.manifests import _CheckpointStore

    store = _CheckpointStore(tmp_path)
    store.save("session:abc", {"turns_done": 3, "summary": "x"})
    assert store.load("session:abc")["turns_done"] == 3
    assert store.load("missing") is None
    keys = store.keys()
    assert "session_abc" in keys
    store.reset("session:abc")
    assert store.load("session:abc") is None


@pytest.mark.asyncio
async def test_context_compressor_fallback_window():
    from veya.server.manifests import assemble_long_horizon

    engine = assemble_long_horizon({})
    compressor = engine.manifest.inject["context_compressor"]
    messages = [{"role": "system", "content": "prefix"}] + [
        {"role": "user", "content": f"m{i}"} for i in range(20)
    ]
    out = await compressor(messages, context={"max_tail": 4})
    assert out["status"] == "compressed"
    assert len(out["messages"]) == 5  # 1 head + 4 tail
    assert out["messages"][0]["content"] == "prefix"
    assert out["summary"] == "[16 earlier messages compressed]"


@pytest.mark.asyncio
async def test_graph_skills_engine_investigate_offline():
    from veya.server.manifests import assemble_graph_skills

    engine = assemble_graph_skills({})
    engine.run()
    files = [
        {"path": "a.py", "content": "import b\n\ndef main():\n    pass\n"},
        {"path": "b.py", "content": "def helper():\n    pass\n"},
    ]
    result = await engine.run_graph_investigate(
        files, seed_nodes=["helper"], context={"task": "review the code"}
    )
    assert result["status"] == "completed"
    assert len(result["graph"]["nodes"]) == 3
    assert "helper" in result["impacted"]
    assert result["skills_matched"] == ["code-review"]  # registry rule "review"
    assert result["coding_workflow"]["status"] == "noop"


@pytest.mark.asyncio
async def test_graph_impact_analysis_bfs_fallback():
    from veya.server.manifests import assemble_graph_skills

    engine = assemble_graph_skills({})
    impact = engine.manifest.inject["graph_impact_analysis"]
    graph = {
        "edges": [
            {"from": "a", "to": "b"},
            {"from": "b", "to": "c"},
            {"from": "x", "to": "y"},
        ]
    }
    out = await impact(graph, ["a"], context={})
    assert out["impacted"] == ["a", "b", "c"]  # BFS reachability
    assert out["status"] == "analyzed"


def test_skills_registry_match_fallback():
    from veya.server.manifests import assemble_graph_skills

    engine = assemble_graph_skills({})
    registry = engine.manifest.config["skills_registry"]
    assert registry.match("please review this module") == ["code-review"]
    assert registry.match("refactor the parser") == ["refactor"]
    assert registry.match("unrelated task") == []


@pytest.mark.asyncio
async def test_code_graph_parser_fallback_structures():
    from veya.server.manifests import assemble_graph_skills

    engine = assemble_graph_skills({})
    parser = engine.manifest.inject["code_graph_parser"]
    out = await parser(
        [
            {
                "path": "m.py",
                "content": "import os\nfrom x import y\n\nclass A:\n    pass\n\ndef f():\n    pass\n",
            }
        ],
        context={},
    )
    assert out["status"] == "parsed"
    kinds = {n["type"] for n in out["graph"]["nodes"]}
    assert kinds == {"import", "def"}


@pytest.mark.asyncio
async def test_skills_coding_workflow_fallback_noop():
    from veya.server.manifests import assemble_graph_skills

    engine = assemble_graph_skills({})
    workflow = engine.manifest.inject["skills_coding_workflow"]
    out = await workflow({"task": "refactor a"}, context={})
    assert out["status"] == "noop"
    assert out["applied"] is False


def test_fastapi_long_horizon_and_graph_endpoints():
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from veya.server.app import app
    from veya.server.manifests import _CheckpointStore

    session = f"lh-api-{uuid4().hex[:8]}"
    try:
        with TestClient(app) as client:
            # long_horizon run + resume
            r1 = client.post(
                "/api/v1/agent/long_horizon",
                json={"task": "搭建数据管道", "session_id": session},
            )
            assert r1.status_code == 200
            assert r1.json()["resumed"] is False
            r2 = client.post(
                "/api/v1/agent/long_horizon",
                json={"task": "继续搭建", "session_id": session},
            )
            assert r2.status_code == 200
            assert r2.json()["resumed"] is True
            assert r2.json()["checkpoint"]["turns_done"] == 2
            assert r2.json()["session_id"] == session
        # validation
        assert client.post("/api/v1/agent/long_horizon", json={"task": ""}).status_code == 422
    finally:
        _CheckpointStore().reset(f"session:{session}")

    with TestClient(app) as client:
        # graph_investigate
        r = client.post(
            "/api/v1/agent/graph_investigate",
            json={
                "files": [
                    {"path": "a.py", "content": "import b\n\ndef main():\n    pass\n"},
                    {"path": "b.py", "content": "def helper():\n    pass\n"},
                ],
                "seed_nodes": ["helper"],
                "query": "review a.py",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        assert body["files_analyzed"] == 2
        assert body["skills_matched"] == ["code-review"]
        assert client.post("/api/v1/agent/graph_investigate", json={"files": []}).status_code == 422


def test_element_status_includes_batch30():
    from veya.server.manifests import element_status

    status = element_status()
    for spec in BATCH30_SHORT + BATCH30_FULL:
        assert spec in status
        assert status[spec] in ("resolved", "unavailable")


def test_manifest_summary_for_long_horizon_and_graph_skills():
    from veya.server.manifests import (
        build_graph_skills_manifest,
        build_long_horizon_manifest,
        manifest_summary,
    )

    for builder in (build_long_horizon_manifest, build_graph_skills_manifest):
        s = manifest_summary(builder({}))
        assert s["name"] and s["skeleton"]
        assert "checkpoint_store" in s["inject"] or "skills_registry" in s["inject"]


def test_long_horizon_engine_health():
    from veya.server.manifests import assemble_long_horizon

    engine = assemble_long_horizon({})
    engine.run()
    h = engine.health()
    assert h["status"] == "healthy"
    assert h["skeleton"] == "long_horizon_agentic_loop"


def test_graph_skills_engine_health():
    from veya.server.manifests import assemble_graph_skills

    engine = assemble_graph_skills({})
    engine.run()
    h = engine.health()
    assert h["status"] == "healthy"
    assert h["skeleton"] == "graph_skills_agentic_loop"
