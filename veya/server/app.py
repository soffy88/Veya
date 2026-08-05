"""veya.server.app — production FastAPI Web & SSE gateway (Layer 4).

Endpoints
---------
- ``POST /api/v1/agent/run``          — one-shot agent run (pseudonymized user)
- ``GET  /api/v1/agent/history/{id}`` — decision trail from the memory store
- ``POST /api/v1/agent/stream``       — SSE streaming run (on_step → data frames)

SSE contract
------------
Every engine ``on_step`` event is escaped to ``data: {"step": ..., "cost": ...}\\n\\n``.
On client disconnect (``CancelledError``) the accumulated decision trail is
saved through ``asyncio.shield`` — a dropped connection never loses the audit
log (SPEC §8 observability).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# ── Agent OS 单进程合并: 根 server.app(主脑/自动化/蜂群/金库/RAG/量化/防火墙/状态机) ──
from server.app import app as _agentos_app
from server.chat_stream import new_agent_stream_events
from veya.im.pseudo import anonymize_user_id
from veya.server.manifests import (
    build_agentic_loop_manifest,
    load_decision_trail,
    manifest_summary,
    new_session_id,
    save_decision_trail,
)
from veya.server.sse import stream_agent_run

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AgentRunRequest(BaseModel):
    """User task request.  ``student_id`` / ``user_id`` are pseudonymized.

    ``text`` (new Agent OS chat contract) routes to the master brain; ``task``
    (legacy contract) keeps the original engine path.
    """

    task: str | None = Field(None, description="Task description (legacy contract)")
    text: str | None = Field(None, description="Chat prompt (new Agent OS contract)")
    student_id: str | None = Field(None, description="Pseudonymized in flight")
    user_id: str | None = Field(None, description="Pseudonymized in flight")
    session_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["run", "dry_run"] = "run"


class AgentRunResponse(BaseModel):
    session_id: str
    status: str
    result: Any = None
    cost_usd: float = 0.0
    user_ref: str | None = None
    plan: dict[str, Any] | None = None




class TeamRequest(BaseModel):
    """Team lifecycle request (ClawTeam swarm)."""
    team_name: str = Field(..., min_length=1)
    goal: str = ""
    members: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class SetupRequest(BaseModel):
    """Agent setup/bootstrap request (DeerFlow interactive wizard)."""
    agent_name: str = Field(..., min_length=1)
    description: str = ""
    soul: str = ""
    skills: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class SupportBundleRequest(BaseModel):
    """Support bundle generation request."""
    include_doctor: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class ReplayRequest(BaseModel):
    """Replay session request."""
    thread_id: str = Field(..., min_length=1)
    max_steps: int = 1000


class EvolveRequest(BaseModel):
    """Agent self-evolution request."""
    agent_name: str = Field(..., min_length=1)
    current_soul: str = ""
    execution_feedback: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class SkillTeachRequest(BaseModel):
    """Skill teaching request."""
    description: str = Field(..., min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class SchedulerRequest(BaseModel):
    """Scheduler management request."""
    action: Literal["create", "list", "toggle", "delete"] = "list"
    id: str = ""
    name: str = ""
    prompt: str = ""
    cron: str = ""
    interval_ms: int = 0
    enabled: bool = True


class KnowledgeRequest(BaseModel):
    """Knowledge store request."""
    action: Literal["read", "write", "list", "stale", "delete", "skeleton"] = "list"
    id: str = ""
    type: str = "module"
    body: str = ""
    covers: list[str] = Field(default_factory=list)


class PluginActionRequest(BaseModel):
    action: Literal["install", "uninstall", "list", "toggle", "configure", "publish", "marketplace"] = "list"
    name: str = ""
    version: str = "1.0.0"
    capabilities: list[str] = Field(default_factory=list)
    source: str = "local"
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    author: str = ""
    tags: list[str] = Field(default_factory=list)

class AgentCreationRequest(BaseModel):
    """NL agent creation request (AutoAgent zero-code)."""
    task: str = Field(..., min_length=1, description="Natural-language agent description")
    config: dict[str, Any] = Field(default_factory=dict)


class OrchestratorRequest(BaseModel):
    """Multi-agent orchestrator creation request."""
    agent_name: str = Field(..., min_length=1)
    description: str = ""
    sub_agents: list[dict[str, Any]] = Field(default_factory=list)
    instructions: str = ""
    goal: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunRequest(BaseModel):
    """Event workflow execution request."""
    system_input: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class MemoryRequest(BaseModel):
    """Vector memory query/add request."""
    action: Literal["query", "add", "peek", "count", "reset"] = "query"
    query_texts: list[str] | None = None
    result: str | None = None
    collection: str = "default"
    n_results: int = 5

class AgentSteerRequest(BaseModel):
    """HITL interrupt control for a running agent session."""

    session_id: str = Field(..., description="Running agent session id")
    action: Literal["approve", "reject", "steer"] = Field(
        ..., description="approve / reject / steer_instruction"
    )
    instruction: str | None = Field(None, description="Steer instruction (action=steer)")
    approval_id: str | None = Field(None, description="Pending approval gate id")


class AgentSwarmRequest(BaseModel):
    """Leader-worker swarm dispatch request."""

    task: str = Field(..., min_length=1, description="Swarm goal")
    session_id: str | None = None
    max_workers: int | None = Field(None, ge=1, le=32, description="Parallel worker cap")
    config: dict[str, Any] = Field(default_factory=dict)


class AgentMediaRequest(BaseModel):
    """Realtime media loop request (one utterance)."""

    frames: list[dict[str, Any]] | None = Field(None, description="Audio frames")
    transcript: str | None = Field(None, description="Transcript passthrough (offline)")
    mode: Literal["converse", "transcribe"] = "converse"
    config: dict[str, Any] = Field(default_factory=dict)


class VerifyRequest(BaseModel):
    """Neuro-symbolic verification request."""

    statement: str = Field(..., min_length=1, description="Natural-language proposition")
    config: dict[str, Any] = Field(default_factory=dict)


class DiagnoseRequest(BaseModel):
    """Root-cause attribution request over a decision trail."""

    decision_trail: list[dict[str, Any]] = Field(
        ..., min_length=1, description="Decision-trail events to attribute"
    )
    max_counterfactuals: int | None = Field(None, ge=1, le=10)
    config: dict[str, Any] = Field(default_factory=dict)


class LongHorizonRequest(BaseModel):
    """Long-horizon task request (checkpoint snapshot restore)."""

    task: str = Field(..., min_length=1, description="Long-horizon goal")
    session_id: str | None = Field(None, description="Resume target (checkpoint key)")
    resume: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class GraphInvestigateRequest(BaseModel):
    """AST graph dependency investigation over code files."""

    files: list[dict[str, Any]] = Field(
        ..., min_length=1, description="[{path, content}, ...] code files"
    )
    seed_nodes: list[str] | None = Field(None, description="Impact-analysis seed nodes")
    query: str | None = Field(None, description="Skills-match query")
    config: dict[str, Any] = Field(default_factory=dict)


class VoiceTranscribeRequest(BaseModel):
    """Audio transcription request (G13 voice)."""

    audio_base64: str | None = Field(None, description="Base64-encoded audio bytes")
    audio_path: str | None = Field(None, description="Path to audio file on server")
    provider: str = "openai"
    language: str = "en"


class VoiceSynthesizeRequest(BaseModel):
    """Text-to-speech synthesis request (G13 voice)."""

    text: str = Field(..., min_length=1, description="Text to synthesize")
    provider: str = "openai"
    voice: str | None = None
    speed: float = 1.0
    format: str = "mp3"


class VisionAnalyzeRequest(BaseModel):
    """Vision analysis request (G13 vision)."""

    image_base64: str | None = Field(None, description="Base64-encoded image")
    image_path: str | None = Field(None, description="Path to image file on server")
    prompt: str = "Describe this image in detail."
    provider: str = "openai"
    model: str | None = None


class VoiceAgentSessionRequest(BaseModel):
    """Voice agent session request (G13)."""

    audio_base64: str = Field(..., description="Base64-encoded audio bytes")
    system_prompt: str = "You are a helpful voice assistant."
    stt_provider: str = "openai"
    tts_provider: str = "openai"
    tts_voice: str | None = None
    language: str = "en"
    max_turns: int = 20


class VisionAgentSessionRequest(BaseModel):
    """Vision agent session request (G13)."""

    image_paths: list[str] = Field(default_factory=list, description="Image file paths")
    image_base64_list: list[str] = Field(default_factory=list, description="Base64 images")
    prompt: str = "Describe this image in detail."
    provider: str = "openai"
    model: str | None = None
    media_type: str = "image"


class BrowserRunRequest(BaseModel):
    """Browser automation request (G14)."""

    url: str = Field(..., description="Starting URL")
    instruction: str = Field(..., description="Natural language task instruction")
    headless: bool = True
    timeout_ms: int = 30000
    max_steps: int = 20
    extract_schema: dict[str, Any] | None = None


class SpawnRunRequest(BaseModel):
    """External agent spawn request (G14)."""

    agent_name: str = Field(..., description="Agent name (claude-code, codex, aider, etc.)")
    prompt: str = Field(..., min_length=1, description="Task prompt")
    workdir: str = "."
    timeout_sec: float = 300.0
    use_worktree: bool = True


class SpawnInstallRequest(BaseModel):
    """Agent installation request (G14)."""

    agent_name: str = Field(..., description="Agent name to install")


class AccountBindRequest(BaseModel):
    """Account binding request (G14)."""

    user_id: str = Field(..., description="Real user ID (will be pseudo-anonymized)")
    platform: str = Field(..., description="Platform (openai, anthropic, discord, etc.)")
    credentials: dict[str, str] = Field(..., description="Credential key-value pairs")


class KanbanRequest(BaseModel):
    """Kanban board operation request (G14)."""

    action: Literal["create", "get", "move", "add_card", "ready"] = "get"
    board_id: str = ""
    board_name: str = "Default Board"
    card_id: str = ""
    card_title: str = ""
    card_description: str = ""
    to_status: str = ""


class InboxRequest(BaseModel):
    """Inbox operation request (G14)."""

    action: Literal["list", "mark_read", "archive"] = "list"
    user_id: str = Field(..., description="Pseudo-anonymized user ID")
    msg_id: str = ""
    unread_only: bool = False


class TemplateRequest(BaseModel):
    """Template operation request (G14)."""

    action: Literal["list", "get", "apply"] = "list"
    template_id: str = ""
    name: str = ""


class HistoryResponse(BaseModel):
    session_id: str
    steps: list[dict[str, Any]]
    count: int


class SandboxRunRequest(BaseModel):
    """Run a command or script inside the isolated sandbox."""

    command: str | None = None
    script: str | None = None
    memory_limit: int = 100 * 1024 * 1024  # 100MB
    time_limit: float = 30.0
    network_blocked: bool = True


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


def create_app() -> FastAPI:
    # Agent OS 单进程: 根 server.app 实例(含 lifespan: Infra + Automata 启停)
    app = _agentos_app
    # CORS 已由根 app 配置; 旧网关端点(IM/MCP/agent_*/spawn/vision/voice...)注册到同一实例

    # ── Static files + SPA frontend ──────────────────────────────
    from pathlib import Path as _Path

    from fastapi.staticfiles import StaticFiles
    _web_dir = _Path(__file__).parent.parent / "web"
    if _web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(_web_dir)), name="static")

    @app.get("/")
    async def root():
        """Serve the veya control panel SPA."""
        from fastapi.responses import HTMLResponse
        index_path = _web_dir / "index.html"
        if index_path.exists():
            return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>veya Gateway</h1><p>Frontend not found. API available at /api/v1/</p>")

    # ── Mount IM routers ─────────────────────────────────────────
    try:
        from veya.im.feishu import make_feishu_router
        app.include_router(make_feishu_router(), prefix="/im/feishu", tags=["IM"])
    except Exception:
        pass
    try:
        from veya.im.slack import make_slack_router
        app.include_router(make_slack_router(), prefix="/im/slack", tags=["IM"])
    except Exception:
        pass
    try:
        from veya.im.discord import make_discord_router
        app.include_router(make_discord_router(), prefix="/im/discord", tags=["IM"])
    except Exception:
        pass
    try:
        from veya.im.dingtalk import make_dingtalk_router
        app.include_router(make_dingtalk_router(), prefix="/im/dingtalk", tags=["IM"])
    except Exception:
        pass
    try:
        from veya.im.wechat import make_wechat_router
        app.include_router(make_wechat_router(), prefix="/im/wechat", tags=["IM"])
    except Exception:
        pass
    try:
        from veya.im.telegram import make_telegram_router
        app.include_router(make_telegram_router(), prefix="/im/telegram", tags=["IM"])
    except Exception:
        pass
    try:
        from veya.mcp_server import create_mcp_server
        mcp = create_mcp_server()
        app.include_router(mcp.as_fastapi_router(), prefix="/mcp", tags=["MCP"])
    except Exception:
        pass
    api = app  # single app instance; routers registered inline below

    # ------------------------------------------------------------------
    # POST /api/v1/agent/run
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/run", response_model=AgentRunResponse)
    async def agent_run(req: AgentRunRequest) -> AgentRunResponse:
        # 新 Agent OS 契约: text → 主脑 ReAct(同进程委托, 无网络跳转)
        if req.text is not None:
            from server.coordinator_master import master_coordinator

            result = await master_coordinator.chat_stream(
                req.text, session_id=req.session_id or None, max_rounds=3
            )
            return AgentRunResponse(
                session_id=result.get("session_id") or req.session_id or new_session_id(),
                status=result.get("status", "failed"),
                result=result.get("final_answer") or result.get("error", ""),
                cost_usd=result.get("cost_usd", 0.0),
            )

        session_id = req.session_id or new_session_id()
        user_ref = None
        raw_uid = req.student_id or req.user_id
        if raw_uid:
            user_ref = anonymize_user_id(raw_uid)

        manifest = build_agentic_loop_manifest(req.config)
        if req.mode == "dry_run":
            return AgentRunResponse(
                session_id=session_id,
                status="dry_run",
                plan=manifest_summary(manifest),
                user_ref=user_ref,
            )

        # Execute the agentic run through the assembled engine.
        from veya.server.manifests import (
            assemble_agentic_loop,
            register_running_engine,
            unregister_running_engine,
        )

        engine = assemble_agentic_loop(req.config)
        engine.run()
        register_running_engine(session_id, engine)
        try:
            result = await engine.invoke(
                {"goal": req.task, "session_id": session_id, "user_ref": user_ref}
            )
        finally:
            unregister_running_engine(session_id)
        turn = result.get("turn_result") or {}
        cost = float(result.get("cost_usd", 0.0))
        # persist a minimal decision trail for history lookup
        save_decision_trail(
            session_id,
            [
                {
                    "event": "session_start",
                    "session_id": session_id,
                    "user_ref": user_ref,
                    "task": req.task[:120],
                },
                {
                    "event": "session_done",
                    "session_id": session_id,
                    "status": result.get("status", "completed"),
                    "cost": cost,
                },
            ],
        )
        return AgentRunResponse(
            session_id=session_id,
            status=result.get("status", "completed"),
            result=turn.get("content") or turn,
            cost_usd=cost,
            user_ref=user_ref,
        )

    # ------------------------------------------------------------------
    # GET /api/v1/agent/history/{session_id}
    # ------------------------------------------------------------------
    @api.get("/api/v1/agent/history/{session_id}", response_model=HistoryResponse)
    async def agent_history(session_id: str) -> HistoryResponse:
        steps = load_decision_trail(session_id)
        if not steps:
            raise HTTPException(status_code=404, detail=f"no decision trail for {session_id}")
        return HistoryResponse(session_id=session_id, steps=steps, count=len(steps))

    # ------------------------------------------------------------------
    # POST /api/v1/agent/stream  (SSE)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/stream")
    async def agent_stream(req: AgentRunRequest, request: Request) -> StreamingResponse:
        # 新 Agent OS 契约: text → 主脑 SSE 事件流(text_delta / tool_call / master_done)
        if req.text is not None:
            return StreamingResponse(
                new_agent_stream_events(req.text, req.session_id),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        session_id = req.session_id or new_session_id()
        user_ref = None
        raw_uid = req.student_id or req.user_id
        if raw_uid:
            user_ref = anonymize_user_id(raw_uid)
        config = dict(req.config)
        if user_ref:
            config["user_ref"] = user_ref

        async def event_source():
            # meta frame first so clients can bind the session id
            yield f"data: {json.dumps({'event': 'session', 'session_id': session_id, 'user_ref': user_ref})}\n\n"
            if req.mode == "dry_run":
                # honor dry_run over SSE: emit the assembled plan, then close
                plan = manifest_summary(build_agentic_loop_manifest(req.config))
                yield f"data: {json.dumps({'event': 'step', 'step': {'action': 'manifest_dry_run', 'detail': plan.get('name', 'agentic_loop')}})}\n\n"
                yield f"data: {json.dumps({'event': 'session_done', 'session_id': session_id, 'status': 'dry_run', 'cost': 0.0})}\n\n"
                yield "data: [DONE]\n\n"
                return
            try:
                async for frame in stream_agent_run(req.task, session_id=session_id, config=config):
                    if await request.is_disconnected():
                        break
                    yield frame
            except asyncio.CancelledError:
                # Persist trail through shield (sse.stream_agent_run already
                # shields its own persistence; re-raise to signal disconnect).
                raise
            finally:
                pass

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------
    # POST /api/v1/agent/steer  (HITL interrupt control)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/steer")
    async def agent_steer(req: AgentSteerRequest) -> dict[str, Any]:
        """Real-time HITL control of a running task.

        ``action`` is ``approve`` / ``reject`` / ``steer``.  With
        ``approval_id`` the signal resolves a pending approval gate; a
        ``steer`` without one is queued and merged into the next turn.
        """
        from veya.server.manifests import get_running_engine

        engine = get_running_engine(req.session_id)
        if engine is None:
            raise HTTPException(status_code=404, detail=f"no running agent for {req.session_id}")
        result = await engine.steer(
            req.action, instruction=req.instruction, approval_id=req.approval_id
        )
        result["session_id"] = req.session_id
        return result

    # ------------------------------------------------------------------
    # GET /api/v1/mcp/tools  (MCP Server tool export, JSON-RPC shape)
    # ------------------------------------------------------------------
    @api.get("/api/v1/mcp/tools")
    async def mcp_tools() -> dict[str, Any]:
        """Expose the registered 3O skill list as a standard MCP ``tools/list``
        result: ``{"jsonrpc": "2.0", "result": {"tools": [...]}}``."""
        from veya.server.manifests import ELEMENT_ALIASES, resolve_element

        tools: list[dict[str, Any]] = []
        unavailable: list[str] = []
        for spec in sorted(ELEMENT_ALIASES):
            element = resolve_element(spec)
            if element is None:
                unavailable.append(spec)
                continue
            lib = ELEMENT_ALIASES[spec][0]
            tools.append(
                {
                    "name": spec.replace(".", "_"),
                    "description": f"3O element {spec} (mounted via {lib})",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "arguments": {"type": "object", "description": "element kwargs"}
                        },
                    },
                }
            )
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": tools},
            "meta": {
                "registered": len(tools),
                "unavailable": len(unavailable),
                "specs": sorted(ELEMENT_ALIASES),
            },
        }

    # ------------------------------------------------------------------
    # POST /api/v1/agent/swarm  (leader-worker swarm dispatch)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/swarm")
    async def agent_swarm(req: AgentSwarmRequest) -> dict[str, Any]:
        """Trigger a swarm Leader-Worker dispatch: split the goal, then run
        each worker branch in isolation (parallel cap from config)."""
        from veya.server.manifests import (
            assemble_swarm_orchestrator,
            register_running_engine,
            unregister_running_engine,
        )

        session_id = req.session_id or new_session_id()
        config = dict(req.config or {})
        config.setdefault("max_workers", req.max_workers or 4)
        engine = assemble_swarm_orchestrator(config)
        engine.run()
        register_running_engine(session_id, engine)
        try:
            result = await engine.dispatch(req.task, context={"session_id": session_id})
        finally:
            unregister_running_engine(session_id)
        result["session_id"] = session_id
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/media  (realtime media loop entry)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/media")
    async def agent_media(req: AgentMediaRequest) -> dict[str, Any]:
        """Run one realtime media utterance through vad → stt → llm → tts.
        Accepts ``frames`` (audio chunks) or ``transcript`` passthrough."""
        from veya.server.manifests import assemble_realtime_media

        frames = req.frames or [{"audio": req.transcript or "", "utterance_id": "u1"}]
        if req.transcript:
            frames[0]["transcript_override"] = req.transcript
        engine = assemble_realtime_media(dict(req.config or {}))
        engine.run()
        result = await engine.run_media_stream(frames, context={"media_loop_mode": req.mode})
        result["session_id"] = new_session_id()
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/verify  (neuro-symbolic verification)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/verify")
    async def agent_verify(req: VerifyRequest) -> dict[str, Any]:
        """Neuro-symbolic (SMT) judgement of a natural-language proposition.

        Pipeline: ``oprim.fol_translate`` → ``obase.smt_solver_adapter`` →
        ``oskill.neuro_symbolic_verify``.  Offline the stages degrade to
        deterministic ``inconclusive`` — the pipeline shape is always exercised.
        """
        from veya.server.manifests import assemble_neuro_symbolic

        engine = assemble_neuro_symbolic(dict(req.config or {}))
        engine.run()
        result = await engine.run_verify(req.statement, context={})
        result["session_id"] = new_session_id()
        result["statement"] = req.statement
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/diagnose  (root-cause attribution & counterfactual)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/diagnose")
    async def agent_diagnose(req: DiagnoseRequest) -> dict[str, Any]:
        """Root-cause attribution over a decision trail.

        Pipeline: ``oprim.causal_graph_build`` →
        ``omodul.root_cause_analysis_workflow`` →
        ``oskill.counterfactual_reasoning`` (what-if report).
        """
        from veya.server.manifests import assemble_root_cause_analysis

        engine = assemble_root_cause_analysis(dict(req.config or {}))
        engine.run()
        context = {}
        if req.max_counterfactuals:
            context["max_counterfactuals"] = req.max_counterfactuals
        result = await engine.run_diagnose(list(req.decision_trail), context=context)
        result["session_id"] = new_session_id()
        result["events_analyzed"] = len(req.decision_trail)
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/long_horizon  (checkpoint + compression)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/long_horizon")
    async def agent_long_horizon(req: LongHorizonRequest) -> dict[str, Any]:
        """Long-horizon task with breakpoint snapshot restore and incremental
        context compression.  Reusing ``session_id`` resumes from the stored
        checkpoint (``obase.checkpoint_store`` + ``oskill.long_context_compress``)."""
        from veya.server.manifests import assemble_long_horizon

        session_id = req.session_id or new_session_id()
        engine = assemble_long_horizon(dict(req.config or {}))
        engine.run()
        result = await engine.run_long_horizon(
            {"goal": req.task, "session_id": session_id},
            context={"resume": req.resume},
        )
        result["session_id"] = session_id
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/graph_investigate  (AST graph + impact)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/graph_investigate")
    async def agent_graph_investigate(req: GraphInvestigateRequest) -> dict[str, Any]:
        """AST code-graph dependency resolution + impact analysis over code
        files/nodes (``oprim.code_graph_parse`` + ``oskill.graph_impact_analysis``
        + ``obase.skills_registry`` rule matching)."""
        from veya.server.manifests import assemble_graph_skills

        engine = assemble_graph_skills(dict(req.config or {}))
        engine.run()
        result = await engine.run_graph_investigate(
            list(req.files),
            seed_nodes=req.seed_nodes,
            context={"task": req.query or ""},
        )
        result["session_id"] = new_session_id()
        result["files_analyzed"] = len(req.files)
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/create  (AutoAgent NL → agent creation)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/create")
    async def agent_create(req: AgentCreationRequest) -> dict[str, Any]:
        from veya.server.manifests import assemble_agent_creation

        engine = assemble_agent_creation(dict(req.config or {}))
        engine.run()
        result = await engine.run_agent_create({"goal": req.task}, context={})
        result["session_id"] = new_session_id()
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/orchestrator  (multi-agent orchestrator)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/orchestrator")
    async def agent_orchestrator(req: OrchestratorRequest) -> dict[str, Any]:
        from pathlib import Path

        from veya.platform import load
        load("omodul")

        from omodul.orchestrator_creation_workflow import orchestrator_creation_workflow

        result = orchestrator_creation_workflow(
            dict(req.config or {}),
            {
                "agent_name": req.agent_name,
                "description": req.description,
                "sub_agents": req.sub_agents,
                "instructions": req.instructions,
                "goal": req.goal,
            },
            Path.home() / ".veya" / "runs",
        )
        result["session_id"] = new_session_id()
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/workflow/run  (event workflow drive)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/workflow/run")
    async def agent_workflow_run(req: WorkflowRunRequest) -> dict[str, Any]:

        from oservi.event_workflow_engine import EventWorkflowEngine

        eng = EventWorkflowEngine(name="api")
        events = req.events or [
            {"name": "start", "body": "return {'result': str(system_input)}"},
            {"name": "finish", "body": "return {'final': True}"},
        ]
        for ev in events:
            eng.make_event(name=ev.get("name"), func=None)
            # register the body as async function
            @eng.make_event(name=ev.get("name"))
            async def _fn(event_input, global_ctx, body=ev.get("body", "pass")):
                locs = {}
                exec(f"async def _inner(event_input, global_ctx):\n    {body.replace(chr(10), chr(10)+'    ')}", globals(), locs)
                return await locs["_inner"](event_input, global_ctx)
        eng.listen_start(events[0]["name"])
        for i in range(len(events) - 1):
            sid = eng.get_event(events[i]["name"])
            fid = eng.get_event(events[i + 1]["name"])
            eng._listeners.setdefault(sid, []).append(f"grp_{i}")
            eng._groups[f"grp_{i}"] = [fid]
            eng._retrigger[f"grp_{i}"] = "all"
        result = await eng.drive(req.system_input)
        result["session_id"] = new_session_id()
        return result

    # ------------------------------------------------------------------
    # GET /api/v1/agent/registry  (list agents/tools/workflows)
    # ------------------------------------------------------------------
    @api.get("/api/v1/agent/registry")
    async def agent_registry_list(type: str = "") -> dict[str, Any]:
        try:
            from obase.agent_registry import registry
            entries = registry.list(type or None)
        except Exception:
            entries = []
        return {"entries": entries, "count": len(entries), "type_filter": type or "all"}

    # ------------------------------------------------------------------
    # POST /api/v1/agent/memory  (vector memory)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/memory")
    async def agent_memory(req: MemoryRequest) -> dict[str, Any]:
        from obase.vector_memory import VectorMemory

        mem = VectorMemory()
        if req.action == "add":
            rid = mem.add_query(req.query_texts[0] if req.query_texts else "", req.result or "", req.collection)
            return {"status": "added", "record_id": rid}
        elif req.action == "peek":
            return {"records": mem.peek(req.collection or None, req.n_results)}
        elif req.action == "count":
            return {"count": mem.count(req.collection or None)}
        elif req.action == "reset":
            mem.reset()
            return {"status": "reset"}
        else:
            hits = mem.query(req.query_texts or [], req.collection or None, req.n_results)
            return {"hits": hits, "count": len(hits)}

    # ------------------------------------------------------------------
    # POST /api/v1/agent/team  (ClawTeam swarm lifecycle)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/team")
    async def agent_team(req: TeamRequest) -> dict[str, Any]:
        """Full team lifecycle: create team → plan → route tasks → dispatch."""
        from pathlib import Path

        from veya.platform import load
        load("omodul")  # inject sys.path for submodule import
        from omodul.team_lifecycle_workflow import team_lifecycle_workflow

        result = team_lifecycle_workflow(
            dict(req.config or {}),
            {
                "team_name": req.team_name,
                "goal": req.goal,
                "members": req.members,
            },
            Path.home() / ".veya" / "runs",
        )
        result["session_id"] = new_session_id()
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/setup  (DeerFlow agent bootstrap)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/setup")
    async def agent_setup(req: SetupRequest) -> dict[str, Any]:
        from pathlib import Path

        from omodul.agent_setup_workflow import agent_setup_workflow

        result = agent_setup_workflow(
            dict(req.config or {}),
            {"agent_name": req.agent_name, "description": req.description, "soul": req.soul, "skills": req.skills},
            Path.home() / ".veya" / "runs",
        )
        result["session_id"] = new_session_id()
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/support-bundle  (redacted diagnostic ZIP)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/support-bundle")
    async def agent_support_bundle(req: SupportBundleRequest) -> dict[str, Any]:
        from obase.support_bundle_pack import support_bundle_pack

        result = support_bundle_pack(include_doctor=req.include_doctor, context=req.config)
        result["session_id"] = new_session_id()
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/replay  (step replay & analysis)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/replay")
    async def agent_replay(req: ReplayRequest) -> dict[str, Any]:
        from oprim.replay_step_record import load_replay, replay_analysis

        loaded = load_replay(req.thread_id, max_steps=req.max_steps)
        if loaded["status"] == "loaded":
            analysis = replay_analysis(loaded["steps"])
            loaded["analysis"] = analysis
        return loaded

    # ------------------------------------------------------------------
    # POST /api/v1/agent/evolve  (agent self-evolution)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/evolve")
    async def agent_evolve(req: EvolveRequest) -> dict[str, Any]:
        from obase.debounced_memory_queue import DebouncedMemoryQueue
        from oskill.soul_self_evolution import soul_self_evolution

        result = soul_self_evolution(req.agent_name, req.current_soul, dict(req.config or {}), req.execution_feedback)
        queue = DebouncedMemoryQueue()
        queue.enqueue(f"agent:{req.agent_name}", {"last_evolution": result, "ts": __import__("time").time()})
        result["session_id"] = new_session_id()
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/deep-research  (AutoAgent Deep Research)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/deep-research")
    async def agent_deep_research(req: AgentRunRequest) -> dict[str, Any]:
        """Branching deep research with citation tracing & structured report."""
        from oskill.deep_research_tree import deep_research_tree

        result = deep_research_tree(req.task, context={"max_depth": 3, "max_branches": 5, **(req.config or {})})
        result["session_id"] = new_session_id()
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/skill/teach  (Cindy skill teach)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/skill/teach")
    async def skill_teach_ep(req: SkillTeachRequest) -> dict[str, Any]:
        from veya.platform import load
        load("oskill")
        from oskill.skill_teach import skill_teach
        result = skill_teach(req.description)
        result["session_id"] = new_session_id()
        return result

    # ------------------------------------------------------------------
    # GET /api/v1/agent/skill/list  (list taught skills)
    # ------------------------------------------------------------------
    @api.get("/api/v1/agent/skill/list")
    async def skill_list_ep() -> dict[str, Any]:
        from veya.platform import load
        load("oskill")
        from oskill.skill_teach import skill_list
        return {"skills": skill_list(), "count": len(skill_list())}

    # ------------------------------------------------------------------
    # POST /api/v1/sandbox/execute  (isolated command/script execution)
    # ------------------------------------------------------------------
    @api.post("/api/v1/sandbox/execute")
    async def sandbox_execute(req: SandboxRunRequest) -> dict[str, Any]:
        """Run a shell command (or Python script) in the safe sandbox:
        resource-limited, network-blocked by default, audit-logged."""
        from veya.sandbox import SandboxConfig, create_safe_executor

        if not req.command and not req.script:
            raise HTTPException(status_code=400, detail="command or script required")

        config = SandboxConfig(
            memory_limit=req.memory_limit,
            time_limit=req.time_limit,
            network_blocked=req.network_blocked,
            audit_enabled=True,
        )
        executor = create_safe_executor(config)
        await executor.start()
        try:
            if req.script:
                result = await executor.run_script(req.script)
            else:
                result = await executor.execute(req.command)
            return {
                "status": "success" if result["exit_code"] == 0 else "failed",
                "output": result.get("stdout", ""),
                "error": result.get("stderr", ""),
                "exit_code": result["exit_code"],
                "duration": result.get("duration", 0),
                "sandboxed": True,
            }
        finally:
            await executor.stop()

    # ------------------------------------------------------------------
    # POST /api/v1/scheduler  (Cindy scheduler CRUD)
    # ------------------------------------------------------------------
    @api.post("/api/v1/scheduler")
    async def scheduler_ep(req: SchedulerRequest) -> dict[str, Any]:
        from oskill.recurring_scheduler import RecurringScheduler
        sched = RecurringScheduler()
        if req.action == "list":
            return {"schedules": [{"id": s.id, "name": s.name, "enabled": s.enabled, "phase": s.phase, "run_count": s.run_count} for s in sched.list_all()]}
        elif req.action == "create":
            s = sched.create(req.id or f"sched_{len(sched.list_all())}", req.name or req.id, req.prompt, req.cron, req.interval_ms)
            return {"status": "created", "id": s.id}
        elif req.action == "toggle":
            ok = sched.update(req.id, enabled=req.enabled) is not None
            return {"status": "toggled" if ok else "not_found", "id": req.id}
        elif req.action == "delete":
            return {"status": "deleted" if sched.delete(req.id) else "not_found"}
        return {"status": "failed", "error": f"unknown action: {req.action}"}

    # ------------------------------------------------------------------
    # POST /api/v1/knowledge  (Cindy knowledge store)
    # ------------------------------------------------------------------
    @api.post("/api/v1/knowledge")
    async def knowledge_ep(req: KnowledgeRequest) -> dict[str, Any]:
        from obase.knowledge_store import KnowledgeStore
        store = KnowledgeStore()
        if req.action == "list":
            return {"entries": store.list_all(req.type or None), "count": len(store.list_all(req.type or None))}
        elif req.action == "read":
            doc = store.read(req.id)
            return doc if doc else {"status": "not_found", "id": req.id}
        elif req.action == "write":
            store.write(req.id, req.type, req.body, covers=req.covers)
            return {"status": "written", "id": req.id}
        elif req.action == "stale":
            return {"stale": store.list_stale(), "count": len(store.list_stale())}
        elif req.action == "delete":
            return {"status": "deleted" if store.delete(req.id) else "not_found"}
        elif req.action == "skeleton":
            return {"skeleton": store.skeleton(req.id, req.type)}
        return {"status": "failed", "error": f"unknown action: {req.action}"}

    # ------------------------------------------------------------------
    # GET /api/v1/mcp/categories  (Cindy MCP progressive discovery)
    # ------------------------------------------------------------------
    @api.get("/api/v1/mcp/categories")
    async def mcp_categories_ep() -> dict[str, Any]:
        from omodul.cindy_mcp_server import build_memory_mcp_server, build_scheduler_mcp_server
        mem = build_memory_mcp_server()
        sch = build_scheduler_mcp_server()
        return {"servers": {"cindy_memory": mem.categories(), "cindy_scheduler": sch.categories()}}

    # ------------------------------------------------------------------
    # POST /api/v1/plugin/*  (Cindy plugin marketplace)
    # ------------------------------------------------------------------
    @api.post("/api/v1/plugin/manage")
    async def plugin_manage(req: PluginActionRequest) -> dict[str, Any]:
        from veya.platform import load as _load_3o
        _load_3o("obase")
        from obase.plugin_registry import PluginRegistry
        reg = PluginRegistry()
        if req.action == "install":
            return reg.install(req.name, req.version, req.capabilities, req.source)
        elif req.action == "uninstall":
            return reg.uninstall(req.name)
        elif req.action == "list":
            return {"plugins": reg.list_installed(), "count": reg.count()}
        elif req.action == "toggle":
            return reg.toggle(req.name, req.enabled)
        elif req.action == "configure":
            return reg.configure(req.name, req.config)
        elif req.action == "publish":
            return reg.publish_to_marketplace(req.name, req.description, req.author, req.tags)
        elif req.action == "marketplace":
            return {"marketplace": reg.list_marketplace(), "installed": reg.list_installed()}
        return {"status": "failed", "error": f"unknown action: {req.action}"}

    # ------------------------------------------------------------------
    # POST /api/v1/agent/skills-inject  (dynamic skill injection)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/skills-inject")
    async def skills_inject_ep(req: SkillTeachRequest) -> dict[str, Any]:
        from oskill.skills_dynamic_inject import skills_dynamic_inject

        ctx = {"system_prompt": "", "tools": [], "config": {}}
        result = skills_dynamic_inject(ctx, context=req.config or {})
        result["session_id"] = new_session_id()
        return result

    # ==================================================================
    # G13 — Voice & Vision Endpoints
    # ==================================================================

    # ------------------------------------------------------------------
    # POST /api/v1/voice/transcribe  (STT)
    # ------------------------------------------------------------------
    @api.post("/api/v1/voice/transcribe")
    async def voice_transcribe(req: VoiceTranscribeRequest) -> dict[str, Any]:
        """Transcribe speech audio to text."""
        import base64

        from veya.oskill.stt import speech_to_text, transcribe_file

        if req.audio_path:
            result = await transcribe_file(
                req.audio_path, provider=req.provider, language=req.language,
            )
        elif req.audio_base64:
            audio_bytes = base64.b64decode(req.audio_base64)
            result = await speech_to_text(
                audio_bytes, provider=req.provider, language=req.language,
            )
        else:
            raise HTTPException(status_code=400, detail="audio_base64 or audio_path required")

        return {
            "text": result.text,
            "words": [{"text": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms}
                      for w in result.words],
            "language": result.language,
            "confidence": result.confidence,
            "duration_ms": result.duration_ms,
        }

    # ------------------------------------------------------------------
    # POST /api/v1/voice/synthesize  (TTS)
    # ------------------------------------------------------------------
    @api.post("/api/v1/voice/synthesize")
    async def voice_synthesize(req: VoiceSynthesizeRequest) -> dict[str, Any]:
        """Synthesize text to speech audio."""
        import base64

        from veya.oskill.tts import text_to_speech

        try:
            audio = await text_to_speech(
                req.text,
                provider=req.provider,
                voice=req.voice,
                speed=req.speed,
                format=req.format,
            )
            return {
                "audio_base64": base64.b64encode(audio).decode("utf-8"),
                "format": req.format,
                "size_bytes": len(audio),
                "text": req.text,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ------------------------------------------------------------------
    # POST /api/v1/vision/analyze  (Vision analysis)
    # ------------------------------------------------------------------
    @api.post("/api/v1/vision/analyze")
    async def vision_analyze(req: VisionAnalyzeRequest) -> dict[str, Any]:
        """Analyze an image using a vision-capable LLM."""
        import base64

        from veya.oskill.vision import analyze_image, analyze_image_file

        if req.image_path:
            result = await analyze_image_file(
                req.image_path, provider=req.provider, prompt=req.prompt, model=req.model,
            )
        elif req.image_base64:
            image_bytes = base64.b64decode(req.image_base64)
            result = await analyze_image(
                image_bytes, provider=req.provider, prompt=req.prompt, model=req.model,
            )
        else:
            raise HTTPException(status_code=400, detail="image_base64 or image_path required")

        return {
            "description": result.description,
            "objects": result.objects,
            "text_in_image": result.text_in_image,
            "model": result.model,
            "processing_time_ms": result.processing_time_ms,
        }

    # ------------------------------------------------------------------
    # POST /api/v1/agent/voice  (Voice agent session)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/voice")
    async def agent_voice(req: VoiceAgentSessionRequest) -> dict[str, Any]:
        """Run a complete voice agent conversation session."""
        import base64
        from pathlib import Path
        from types import SimpleNamespace

        from veya.omodul.voice_agent import run_voice_conversation

        audio_bytes = base64.b64decode(req.audio_base64)

        config = SimpleNamespace(
            sample_rate=16000,
            language=req.language,
            stt_provider=req.stt_provider,
            tts_provider=req.tts_provider,
            tts_voice=req.tts_voice,
            max_turns=req.max_turns,
        )
        input_data = SimpleNamespace(
            audio_input=audio_bytes,
            system_prompt=req.system_prompt,
        )

        result = await run_voice_conversation(config, input_data, Path("/tmp/veya/voice"))
        return result

    # ------------------------------------------------------------------
    # POST /api/v1/agent/vision  (Vision agent session)
    # ------------------------------------------------------------------
    @api.post("/api/v1/agent/vision")
    async def agent_vision(req: VisionAgentSessionRequest) -> dict[str, Any]:
        """Run a vision agent analysis session."""
        from pathlib import Path
        from types import SimpleNamespace

        from veya.omodul.vision_agent import run_vision_analysis

        config = SimpleNamespace(
            provider=req.provider,
            model=req.model,
        )
        input_data = SimpleNamespace(
            prompt=req.prompt,
            media_type=req.media_type,
        )

        if req.media_type == "images" and req.image_paths:
            input_data.image_paths = req.image_paths
        elif req.image_paths:
            input_data.image_path = req.image_paths[0]
        elif req.image_base64_list:
            import base64
            input_data.image_data = base64.b64decode(req.image_base64_list[0])

        result = await run_vision_analysis(config, input_data, Path("/tmp/veya/vision"))
        return result

    # ------------------------------------------------------------------
    # GET /api/v1/voice/stream  (SSE voice streaming session)
    # ------------------------------------------------------------------
    @api.get("/api/v1/voice/stream")
    async def voice_stream_sse(request: Request) -> StreamingResponse:
        """SSE endpoint for streaming voice agent events."""
        from veya.streaming import create_voice_stream_manager

        vstream = create_voice_stream_manager()

        async def event_source():
            await vstream.start()
            yield f"data: {json.dumps({'event': 'session_start', 'mode': 'voice'})}\n\n"

            async for event in vstream.get_events():
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(event.to_dict())}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ==================================================================
    # G14 — Browser Automation Endpoints
    # ==================================================================

    @api.post("/api/v1/browser/run")
    async def browser_run(req: BrowserRunRequest) -> dict[str, Any]:
        """Execute a browser automation task using Playwright."""
        from pathlib import Path
        from types import SimpleNamespace

        from veya.omodul.browser_agent import run_browser_automation

        config = SimpleNamespace(
            headless=req.headless,
            timeout_ms=req.timeout_ms,
            max_steps=req.max_steps,
        )
        input_data = SimpleNamespace(
            url=req.url,
            instruction=req.instruction,
            extract_schema=req.extract_schema,
        )

        result = await run_browser_automation(config, input_data, Path("/tmp/veya/browser"))
        return result

    @api.get("/api/v1/browser/status")
    async def browser_status() -> dict[str, Any]:
        """Check browser automation availability."""
        try:
            import importlib.util as _ilu  # noqa: F401

            from veya.oskill.browser import BrowserSession  # noqa: F401
        except ImportError:
            return {"status": "unavailable", "reason": "playwright not installed"}

        try:
            import subprocess
            result = subprocess.run(["playwright", "--version"], capture_output=True, text=True, timeout=5)
            version = result.stdout.strip() or "installed"
        except Exception:
            version = "unknown"
        return {"status": "available", "playwright_version": version}

    # ==================================================================
    # G14 — Agent Spawn Endpoints
    # ==================================================================

    @api.get("/api/v1/spawn/agents")
    async def spawn_list_agents() -> dict[str, Any]:
        """List all registered external agents and their availability."""
        from veya.oskill.spawn import list_agents
        return {"agents": list_agents()}

    @api.post("/api/v1/spawn/run")
    async def spawn_run(req: SpawnRunRequest) -> dict[str, Any]:
        """Spawn an external AI coding agent to execute a task."""
        from pathlib import Path

        from veya.oskill.spawn import AgentSpawner

        spawner = AgentSpawner()
        result = await spawner.spawn(
            req.agent_name,
            req.prompt,
            workdir=Path(req.workdir or "."),
            timeout_sec=req.timeout_sec,
            use_worktree=req.use_worktree,
        )
        return {
            "agent_name": result.agent_name,
            "success": result.success,
            "exit_code": result.exit_code,
            "stdout": result.stdout[:5000],
            "stderr": result.stderr[:1000],
            "duration_sec": result.duration_sec,
            "error": result.error,
        }

    @api.post("/api/v1/spawn/install")
    async def spawn_install(req: SpawnInstallRequest) -> dict[str, Any]:
        """Install an external agent CLI tool."""
        from veya.oskill.spawn import AgentSpawner

        spawner = AgentSpawner()
        success, message = await spawner.install(req.agent_name)
        return {"success": success, "message": message}

    # ==================================================================
    # G14 — Account Binding Endpoints
    # ==================================================================

    @api.post("/api/v1/account/bind")
    async def account_bind(req: AccountBindRequest) -> dict[str, Any]:
        """Bind a user's account credentials for a platform."""
        from veya.im.account_binding import bind_account

        binding = bind_account(req.user_id, req.platform, req.credentials)
        return {
            "status": "bound",
            "platform": binding.platform,
            "is_active": binding.is_active,
        }

    @api.get("/api/v1/account/list")
    async def account_list(user_id: str) -> dict[str, Any]:
        """List a user's account bindings (no credentials exposed)."""
        from veya.im.account_binding import list_user_bindings
        return {"bindings": list_user_bindings(user_id)}

    @api.delete("/api/v1/account/unbind")
    async def account_unbind(user_id: str, platform: str) -> dict[str, Any]:
        """Remove a user's account binding."""
        from veya.im.account_binding import unbind_account
        success = unbind_account(user_id, platform)
        return {"status": "unbound" if success else "not_found"}

    # ==================================================================
    # G14 — Kanban + Inbox + Templates Endpoints
    # ==================================================================

    @api.post("/api/v1/kanban")
    async def kanban_ops(req: KanbanRequest) -> dict[str, Any]:
        """Kanban board operations."""
        from veya.kanban import CardStatus, KanbanBoard, KanbanCard

        board_path = Path.home() / ".veya" / "kanban" / f"{req.board_id or 'default'}.json"
        board_path.parent.mkdir(parents=True, exist_ok=True)

        if req.action == "create":
            board = KanbanBoard.create_default(req.board_name)
            board_path.write_text(json.dumps(board.to_dict(), indent=2))
            return {"status": "created", "board": board.to_dict()}

        # Load existing board
        if board_path.exists():
            board = KanbanBoard.from_dict(json.loads(board_path.read_text()))
        else:
            board = KanbanBoard.create_default(req.board_name or "Default")

        if req.action == "get":
            return {"board": board.to_dict(), "ready_cards": len(board.get_ready_cards())}

        elif req.action == "add_card":
            card = KanbanCard(title=req.card_title, description=req.card_description)
            board.add_card(card)
            board_path.write_text(json.dumps(board.to_dict(), indent=2))
            return {"status": "added", "card": card.to_dict()}

        elif req.action == "move":
            ok = board.move_card(req.card_id, CardStatus(req.to_status))
            if ok:
                board_path.write_text(json.dumps(board.to_dict(), indent=2))
            return {"status": "moved" if ok else "not_found", "card_id": req.card_id}

        elif req.action == "ready":
            ready = board.get_ready_cards()
            return {"ready": [c.to_dict() for c in ready], "count": len(ready)}

        return {"status": "error", "error": f"Unknown action: {req.action}"}

    @api.get("/api/v1/kanban/graph")
    async def kanban_graph(board_id: str = "default") -> dict[str, Any]:
        """Get kanban dependency graph."""
        from veya.kanban import KanbanBoard
        board_path = Path.home() / ".veya" / "kanban" / f"{board_id}.json"
        if not board_path.exists():
            return {"error": "Board not found"}
        board = KanbanBoard.from_dict(json.loads(board_path.read_text()))
        return {"dependency_graph": board.get_dependency_graph()}

    @api.post("/api/v1/inbox")
    async def inbox_ops(req: InboxRequest) -> dict[str, Any]:
        """Inbox message operations."""
        from veya.kanban import Inbox

        inbox = Inbox(req.user_id)
        if req.action == "list":
            msgs = inbox.list(unread_only=req.unread_only)
            return {"messages": [m.to_dict() for m in msgs], "unread": inbox.count_unread()}
        elif req.action == "mark_read":
            inbox.mark_read(req.msg_id)
            return {"status": "read"}
        elif req.action == "archive":
            inbox.archive(req.msg_id)
            return {"status": "archived"}
        return {"status": "error"}

    @api.post("/api/v1/templates")
    async def templates_ops(req: TemplateRequest) -> dict[str, Any]:
        """Project template operations."""
        from veya.kanban import apply_template, get_template, list_templates

        if req.action == "list":
            return {"templates": list_templates()}
        elif req.action == "get":
            t = get_template(req.template_id)
            return {"template": t} if t else {"error": f"Unknown: {req.template_id}"}
        elif req.action == "apply":
            result = apply_template(req.template_id, name=req.name)
            return result
        return {"status": "error"}

    # ==================================================================
    # G14 — MCP Server Endpoint
    # ==================================================================

    @api.post("/api/v1/mcp/jsonrpc")
    async def mcp_jsonrpc(request: Request) -> dict[str, Any]:
        """MCP JSON-RPC 2.0 endpoint — compatible with Claude Desktop, Cursor, etc."""
        from veya.mcp_server import create_mcp_server

        server = create_mcp_server()
        body = await request.body()
        response = await server.handle_request(body.decode())
        return json.loads(response)

    @api.get("/api/v1/mcp/health")
    async def mcp_health():
        from veya.mcp_server import create_mcp_server
        server = create_mcp_server()
        return {
            "status": "ok",
            "server": server.name,
            "version": server.version,
            "tools_count": len(server.tools.list_tools()),
        }

    # ==================================================================
    # G14 — Tool Manager Endpoints
    # ==================================================================

    @api.get("/api/v1/tools/status")
    async def tools_status() -> dict[str, Any]:
        """Check status of all registered CLI tools."""
        from veya.oskill.tool_manager import ToolManager

        tm = ToolManager()
        statuses = tm.check_all()
        return {
            "tools": [
                {"name": s.name, "installed": s.installed, "version": s.version, "path": s.path}
                for s in statuses
            ],
            "installed_count": sum(1 for s in statuses if s.installed),
            "total": len(statuses),
        }

    @api.post("/api/v1/tools/install")
    async def tools_install(tool_name: str) -> dict[str, Any]:
        """Install a CLI tool."""
        from veya.oskill.tool_manager import ToolManager

        tm = ToolManager()
        result = tm.install(tool_name)
        return {"name": result.name, "success": result.success, "message": result.message, "error": result.error}

    @api.get("/api/v1/tools/required")
    async def tools_required(capability: str = "veya.core") -> dict[str, Any]:
        """Check tools required for a specific capability."""
        from veya.oskill.tool_manager import ToolManager

        tm = ToolManager()
        statuses = tm.check_required_by(capability)
        return {
            "capability": capability,
            "tools": [{"name": s.name, "installed": s.installed, "version": s.version} for s in statuses],
        }

    # ==================================================================
    # G14 — Mobile PWA Endpoints
    # ==================================================================

    @api.get("/mobile/manifest.json")
    async def mobile_manifest():
        """PWA manifest for mobile shell."""
        from veya.oprim.mobile import build_pwa_manifest
        return build_pwa_manifest()

    @api.get("/mobile/shell")
    async def mobile_shell():
        """Mobile PWA shell HTML."""
        from fastapi.responses import HTMLResponse

        from veya.oprim.mobile import build_mobile_shell_html
        return HTMLResponse(content=build_mobile_shell_html())

    @api.get("/mobile/sw.js")
    async def mobile_service_worker():
        """PWA service worker."""
        from fastapi.responses import Response

        from veya.oprim.mobile import build_service_worker_js
        return Response(content=build_service_worker_js(), media_type="application/javascript")

    @api.get("/api/v1/mobile/devices")
    async def mobile_devices():
        """List connected mobile devices."""
        from veya.oskill.mobile import MobileBridge
        bridge = MobileBridge()
        return {"devices": [{"id": d.device_id, "ua": d.user_agent[:100], "screen": f"{d.screen_width}x{d.screen_height}"} for d in bridge.list_devices()]}

    return app


app = create_app()


def main(argv: list[str] | None = None) -> None:
    """Run the Layer 4 gateway on 127.0.0.1:8765 (the desktop app's default).

    ``--port`` overrides the listen port (e.g. when 8765 is occupied on a dev
    box; the desktop app then points at it via ``VITE_VEYA_ENDPOINT``).
    """
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="veya-gateway")
    parser.add_argument("--port", type=int, default=8765, help="listen port (default 8765)")
    parser.add_argument("--host", default="127.0.0.1", help="listen host (default 127.0.0.1)")
    args = parser.parse_args(argv)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()


__all__ = [
    "AccountBindRequest",
    "AgentMediaRequest",
    "AgentRunRequest",
    "AgentRunResponse",
    "AgentSteerRequest",
    "AgentSwarmRequest",
    "BrowserRunRequest",
    "DiagnoseRequest",
    "GraphInvestigateRequest",
    "HistoryResponse",
    "InboxRequest",
    "KanbanRequest",
    "LongHorizonRequest",
    "SpawnInstallRequest",
    "SpawnRunRequest",
    "TemplateRequest",
    "VerifyRequest",
    "VisionAgentSessionRequest",
    "VisionAnalyzeRequest",
    "VoiceAgentSessionRequest",
    "VoiceSynthesizeRequest",
    "VoiceTranscribeRequest",
    "app",
    "create_app",
    "main",
]
