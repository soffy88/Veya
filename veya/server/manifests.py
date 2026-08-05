"""veya.server.manifests — Layer-4 element registry & decision trails.

Retains the non-engine utilities of the legacy gateway: 3O element alias
resolution (ELEMENT_ALIASES / resolve_element / element_status) and JSONL
decision-trail persistence. The ServiceManifest / AssembledEngine engine
layer was removed when all gateway endpoints migrated to the Agent OS
master brain (server.coordinator_master).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from veya.platform import load

ELEMENT_ALIASES: dict[str, tuple[str, tuple[str, ...]]] = {
    # oprim — LLM chat call / streaming
    "oprim.llm_chat_call": ("oprim", ("llm_complete", "llm_stream")),
    "oprim.llm_stream": ("oprim", ("llm_stream", "llm_complete")),
    # oskill — MCP tool routing + task dag split
    "oskill.mcp_tool_route": ("oskill", ("mcp_tool_invoke",)),
    "oskill.task_dag_split": (
        "oskill",
        ("plan_decompose", "expand_tasks_from_note", "plan_to_todos"),
    ),
    # omodul — execution / investigation / multi-agent / memory workflows
    "omodul.sandbox_execution_workflow": ("omodul", ("execute_tool", "run_and_fix")),
    "omodul.code_investigation_workflow": (
        "omodul",
        ("explain_codebase", "code_review", "index_codebase"),
    ),
    "omodul.multi_agent_worktree_workflow": ("omodul", ("run_subagent_task", "run_subagent")),
    "omodul.long_task_memory_workflow": (
        "omodul",
        ("compact_session", "summarize_session", "store_memory"),
    ),
    # obase — cost / decision trail / git worktree / pseudo-anonymizer
    "obase.cost_tracker": ("obase", ("CostTracker",)),
    "obase.decision_logger": ("obase", ("Trail",)),
    "obase.git_worktree": ("oprim", ("git_worktree_add", "git_worktree_remove")),
    "obase.pseudo_anonymizer": ("obase", ("PseudoAnonymizer",)),
    # oservi — engines
    "oservi.agentic_loop": ("oservi", ("AgenticLoopEngine",)),
    "oservi.dag_orchestrator": ("oservi", ("SubagentOrchestratorEngine",)),
    # ------------------------------------------------------------------
    # Ultimate assembly — 48 additional elements (12 obase + 15 oprim +
    # 11 oskill + 7 omodul + 3 oservi).  Spec names are Layer-4 plan names;
    # each resolves to the closest mounted 3O symbol (lazy, graceful None
    # when the element or its heavy deps are not installed).  Specs with an
    # empty candidate tuple have no mounted 3O symbol yet — a Layer-4
    # fallback adapter stands in (3O-first, graceful-degrade principle).
    # ------------------------------------------------------------------
    # obase — infra / signal / media elements
    "obase.treesitter_indexer": ("oskill", ("repo_map_build", "build_repo_context", "chunk_code")),
    "obase.hitl_signal_bus": ("obase", ()),
    "obase.mcp_server": ("oprim", ("mcp_server", "create_mcp_server")),
    "obase.zeromq_bus": ("obase", ()),
    "obase.durable_lease_queue": ("obase", ("DistributedLock",)),
    "obase.zero_egress_sandbox": ("oprim", ("url_fetch_ssrf_safe", "check_path_allowed")),
    "obase.livekit_webrtc": ("obase", ()),
    "obase.vad_pipeline": ("obase", ()),
    "obase.signed_token_session": ("oprim", ("ed25519_sign", "hmac_sha256")),
    "obase.media_scraper": ("oprim", ("media_extract", "media_probe")),
    "debounced_memory_queue": ("obase", ("DebouncedMemoryQueue", "compress_context", "context_compact")),
    "obase.debounced_memory_queue": ("obase", ("DebouncedMemoryQueue", "compress_context", "context_compact")),
    "obase.harness_bridge": ("obase", ()),
    # oprim — agent-enhancement primitives
    "oprim.ast_extract_symbols": ("oskill", ("extract_symbols",)),
    "oprim.hitl_wait_approval": ("oprim", ()),
    "oprim.mcp_register_tool": ("oprim", ("register_tool",)),
    "oprim.agent_prompt_synthesize": ("oskill", ("prompt_assemble", "build_subagent_prompt")),
    "oprim.git_worktree_merge": ("oskill", ("three_way_merge", "resolve_conflict")),
    "oprim.tmux_pane_create": ("oprim", ("tmux_pane_create", "spawn_pty", "bash_exec")),
    "oprim.kanban_task_update": ("oprim", ("kanban_task_update", "apply_todo_update", "plan_to_todos")),
    "oprim.stt_transcribe_stream": ("oprim", ("transcribe_audio", "speech_to_text")),
    "oprim.tts_synthesize_stream": ("oprim", ("tts_synthesize", "text_to_speech")),
    "oprim.frontend_tool_forward": ("oprim", ("http_post_webhook",)),
    "soul_config_rewrite": ("oprim", ("soul_config_rewrite",)),
    "oprim.soul_config_rewrite": ("oprim", ("soul_config_rewrite",)),
    "replay_step_record": ("oprim", ("replay_step_record", "serialize_event", "write_event")),
    "oprim.replay_step_record": ("oprim", ("replay_step_record", "serialize_event", "write_event")),
    "oprim.media_content_parse": ("oprim", ("extract_main_content", "media_extract")),
    "oprim.media_publish_post": ("oprim", ("http_post_webhook", "http_post")),
    "obase.support_bundle_pack": ("obase", ("support_bundle_pack", "dir_archive_to_targz", "archive_to_targz")),
    "support_bundle_pack": ("obase", ("support_bundle_pack", "dir_archive_to_targz", "archive_to_targz")),
    "oprim.support_bundle_pack": ("obase", ("support_bundle_pack", "dir_archive_to_targz", "archive_to_targz")),
    # oskill — research / dispatch / evolution skills
    "oskill.repomap_gen": ("oskill", ("repo_map_build", "build_repo_context")),
    "oskill.hitl_instruction_steer": ("oskill", ()),
    "oskill.mcp_schema_adapter": ("oprim", ("mcp_tool_to_schema", "build_tool_schema")),
    "oskill.deep_research_tree": ("oskill", ("deep_research_tree", "deep_read", "web_research", "researcher_workflow")),
    "oskill.dag_visual_layout": ("oskill", ("dag_visual_layout", "generate_svg_diagram")),
    "oskill.leader_worker_dispatch": ("oskill", ("leader_worker_dispatch", "subagent_dispatch", "plan_decompose")),
    "oskill.contextual_reschedule": (
        "oskill",
        ("escalate_thinking_budget", "regime_dynamic_weight_adjustment"),
    ),
    "oskill.voice_interruption_handler": ("oskill", ()),
    "soul_self_evolution": ("oskill", ("soul_self_evolution",)),
    "oskill.soul_self_evolution": ("oskill", ("soul_self_evolution",)),
    "oskill.worktree_conflict_resolve": ("oskill", ("worktree_conflict_resolve", "conflict_resolution", "resolve_conflict")),
    "oskill.harness_uniform_route": ("oprim", ("invoke",)),
    # omodul — approval / export / synthesis / swarm / media workflows
    "omodul.hitl_approval_workflow": ("omodul", ("parent_review",)),
    "omodul.mcp_tool_export_workflow": ("omodul", ()),
    "omodul.nl_agent_synthesis_workflow": ("omodul", ("process_prompt",)),
    "omodul.swarm_collaborative_workflow": ("omodul", ("run_subagent_task", "run_subagent")),
    "omodul.durable_lease_task_workflow": ("omodul", ("create_checkpoint", "rewind_to_checkpoint")),
    "omodul.realtime_voice_agent_workflow": ("omodul", ()),
    "omodul.content_media_pipeline_workflow": ("oprim", ("media_extract", "media_probe")),
    # oservi — steerable / swarm / realtime engines
    "oservi.steerable_agentic_loop": ("oservi", ("AgenticLoopEngine", "ActionPlannerEngine")),
    "oservi.swarm_orchestrator": ("oservi", ("SubagentOrchestratorEngine",)),
    "oservi.realtime_media_loop": ("oservi", ("AgenticLoopEngine",)),
    # ------------------------------------------------------------------
    # Frontier elements — 10 newly landed specs (short-name + full-name
    # aliases, both resolve through the same single lazy channel).  Mapped
    # to the closest mounted 3O symbol; empty candidate tuples mean the
    # element has no mounted 3O counterpart yet and a Layer-4 fallback
    # adapter stands in (deterministic offline behaviour).
    # ------------------------------------------------------------------
    # obase — SMT / formal verification infrastructure
    "smt_solver_adapter": ("oprim", ("solve_function", "solve_trig", "solve_sequence")),
    "obase.smt_solver_adapter": ("oprim", ("solve_function", "solve_trig", "solve_sequence")),
    "lean_formal_prover": ("oskill", ("formal_proof_verify", "theorem_verify_3way")),
    "obase.lean_formal_prover": ("oskill", ("formal_proof_verify", "theorem_verify_3way")),
    # oprim — FOL / causality / invariant primitives
    "fol_translate": ("oprim", ()),
    "oprim.fol_translate": ("oprim", ()),
    "causal_graph_build": (
        "oskill",
        ("causal_discovery", "pcmci_causal_discovery", "structural_causal_model_fit"),
    ),
    "oprim.causal_graph_build": (
        "oskill",
        ("causal_discovery", "pcmci_causal_discovery", "structural_causal_model_fit"),
    ),
    "invariant_extract": ("oprim", ()),
    "oprim.invariant_extract": ("oprim", ()),
    # oskill — neuro-symbolic / counterfactual / formal proof skills
    "neuro_symbolic_verify": ("oskill", ("formal_proof_verify", "theorem_verify_3way")),
    "oskill.neuro_symbolic_verify": ("oskill", ("formal_proof_verify", "theorem_verify_3way")),
    "counterfactual_reasoning": ("oskill", ("counterfactual_generator",)),
    "oskill.counterfactual_reasoning": ("oskill", ("counterfactual_generator",)),
    "formal_code_proof": ("oskill", ("formal_proof_verify", "syntax_check")),
    "oskill.formal_code_proof": ("oskill", ("formal_proof_verify", "syntax_check")),
    # omodul — root-cause workflow
    "root_cause_analysis_workflow": ("oskill", ("ic_root_cause_decompose",)),
    "omodul.root_cause_analysis_workflow": ("oskill", ("ic_root_cause_decompose",)),
    # oservi — mechanism game loop engine
    "mechanism_game_loop": ("oservi", ("StateMachineEngine", "AgenticLoopEngine")),
    "oservi.mechanism_game_loop": ("oservi", ("StateMachineEngine", "AgenticLoopEngine")),
    # ------------------------------------------------------------------
    # 30 freshly landed elements (short-name + full-name aliases).  The
    # parenthesised private names (e.g. ``_tdd_test_run``) are the canonical
    # mounted symbols once the main libraries ship them; until then the next
    # candidates (closest real primitives) resolve, else a Layer-4 fallback
    # adapter stands in — 3O-first, graceful degrade.
    # ------------------------------------------------------------------
    # obase — persistence / browser / network / skills infrastructure
    "checkpoint_store": ("obase", ("CheckpointStore", "make_checkpoint", "restore_from_checkpoint")),
    "obase.checkpoint_store": ("obase", ("CheckpointStore", "make_checkpoint", "restore_from_checkpoint")),
    "browser_vision_runner": ("oprim", ("image_understand", "vlm_video_analyze")),
    "obase.browser_vision_runner": ("oprim", ("image_understand", "vlm_video_analyze")),
    "ssrf_safe_network": ("oprim", ("url_fetch_ssrf_safe", "url_safety_check", "http_fetch")),
    "obase.ssrf_safe_network": ("oprim", ("url_fetch_ssrf_safe", "url_safety_check", "http_fetch")),
    "skills_registry": ("obase", ("tool_registry", "ToolRegistry")),
    "obase.skills_registry": ("obase", ("tool_registry", "ToolRegistry")),
    "adaptive_scraper": ("obase", ("adaptive_scraper", "extract_main_content", "media_extract", "fetch_rss")),
    "obase.adaptive_scraper": ("obase", ("adaptive_scraper", "extract_main_content", "media_extract", "fetch_rss")),
    "dlt_pipeline_store": ("oprim", ("write_rows", "open_meta_db")),
    "obase.dlt_pipeline_store": ("oprim", ("write_rows", "open_meta_db")),
    # oprim — TDD / checkpoint / browser / search / graph primitives
    "tdd_test_run": ("oprim", ("tdd_test_run", "_tdd_test_run", "bash_exec", "bash_exec_stream")),
    "oprim.tdd_test_run": ("oprim", ("tdd_test_run", "_tdd_test_run", "bash_exec", "bash_exec_stream")),
    "git_checkpoint_commit": (
        "oprim",
        ("_git_checkpoint_commit", "git_snapshot", "git_commit", "git_restore_snapshot"),
    ),
    "oprim.git_checkpoint_commit": (
        "oprim",
        ("_git_checkpoint_commit", "git_snapshot", "git_commit", "git_restore_snapshot"),
    ),
    "browser_element_interact": (
        "oprim",
        ("browser_element_interact", "_browser_element_interact", "http_post", "http_request_once"),
    ),
    "oprim.browser_element_interact": (
        "oprim",
        ("browser_element_interact", "_browser_element_interact", "http_post", "http_request_once"),
    ),
    "web_search_fetch": ("oprim", ("_web_search_fetch", "web_search", "searxng_search")),
    "oprim.web_search_fetch": ("oprim", ("_web_search_fetch", "web_search", "searxng_search")),
    "code_graph_parse": (
        "oprim",
        ("code_graph_parse", "_code_graph_parse", "repo_map_build", "scan_project_structure", "trace_dependency"),
    ),
    "oprim.code_graph_parse": (
        "oprim",
        ("code_graph_parse", "_code_graph_parse", "repo_map_build", "scan_project_structure", "trace_dependency"),
    ),
    "adaptive_node_extract": (
        "oskill",
        ("_adaptive_node_extract", "extract_symbols", "repo_map_build"),
    ),
    "oprim.adaptive_node_extract": (
        "oskill",
        ("_adaptive_node_extract", "extract_symbols", "repo_map_build"),
    ),
    "domain_rule_check": (
        "oskill",
        ("_domain_rule_check", "dsl_rule_evaluate", "dsl_rule_validate"),
    ),
    "oprim.domain_rule_check": (
        "oskill",
        ("_domain_rule_check", "dsl_rule_evaluate", "dsl_rule_validate"),
    ),
    "dlt_schema_normalize": ("oskill", ("_dlt_schema_normalize", "extract_json", "merge_config")),
    "oprim.dlt_schema_normalize": (
        "oskill",
        ("_dlt_schema_normalize", "extract_json", "merge_config"),
    ),
    # oskill — review gates / compression / VLM / skills / graph skills
    "code_review_gate": ("omodul", ("_code_review_gate", "code_review", "parent_review")),
    "oskill.code_review_gate": ("omodul", ("_code_review_gate", "code_review", "parent_review")),
    "long_context_compress": (
        "oskill",
        ("_long_context_compress", "compress_context", "context_compact"),
    ),
    "oskill.long_context_compress": (
        "oskill",
        ("_long_context_compress", "compress_context", "context_compact"),
    ),
    "vlm_page_nav": ("oprim", ("_vlm_page_nav", "vlm_video_analyze", "image_understand")),
    "oskill.vlm_page_nav": ("oprim", ("_vlm_page_nav", "vlm_video_analyze", "image_understand")),
    "proactive_deep_reach": (
        "oskill",
        ("_proactive_deep_reach", "deep_read", "web_research", "researcher_workflow"),
    ),
    "oskill.proactive_deep_reach": (
        "oskill",
        ("_proactive_deep_reach", "deep_read", "web_research", "researcher_workflow"),
    ),
    "skills_dynamic_inject": (
        "oskill",
        ("_skills_dynamic_inject", "select_skill", "load_skill_progressive"),
    ),
    "oskill.skills_dynamic_inject": (
        "oskill",
        ("_skills_dynamic_inject", "select_skill", "load_skill_progressive"),
    ),
    "graph_impact_analysis": (
        "oprim",
        ("graph_impact_analysis", "_graph_impact_analysis", "graph_expand_retrieval", "trace_dependency"),
    ),
    "oskill.graph_impact_analysis": (
        "oprim",
        ("graph_impact_analysis", "_graph_impact_analysis", "graph_expand_retrieval", "trace_dependency"),
    ),
    "smart_web_scraping": ("oprim", ("_smart_web_scraping", "extract_main_content", "parse_html")),
    "oskill.smart_web_scraping": (
        "oprim",
        ("_smart_web_scraping", "extract_main_content", "parse_html"),
    ),
    "auto_data_curation": ("oskill", ("_auto_data_curation", "dedup_prefilter", "extract_json")),
    "oskill.auto_data_curation": (
        "oskill",
        ("_auto_data_curation", "dedup_prefilter", "extract_json"),
    ),
    # omodul — workflows
    "tdd_programming_workflow": ("omodul", ("run_and_fix", "generate_tests")),
    "omodul.tdd_programming_workflow": ("omodul", ("run_and_fix", "generate_tests")),
    "long_horizon_checkpoint_workflow": (
        "omodul",
        ("create_checkpoint", "rewind_to_checkpoint", "compact_session"),
    ),
    "omodul.long_horizon_checkpoint_workflow": (
        "omodul",
        ("create_checkpoint", "rewind_to_checkpoint", "compact_session"),
    ),
    "active_web_research_workflow": ("omodul", ("web_research_task",)),
    "omodul.active_web_research_workflow": ("omodul", ("web_research_task",)),
    "skills_guided_coding_workflow": ("omodul", ("run_and_fix", "code_review")),
    "omodul.skills_guided_coding_workflow": ("omodul", ("run_and_fix", "code_review")),
    "graph_codebase_investigation_workflow": ("omodul", ("explain_codebase", "index_codebase")),
    "omodul.graph_codebase_investigation_workflow": (
        "omodul",
        ("explain_codebase", "index_codebase"),
    ),
    "auto_etl_research_workflow": ("omodul", ("web_research_task", "export_substrate_markdown")),
    "omodul.auto_etl_research_workflow": (
        "omodul",
        ("web_research_task", "export_substrate_markdown"),
    ),
    # oservi — long-horizon / graph-skills engines
    "long_horizon_agentic_loop": ("oservi", ("AgenticLoopEngine", "CronSchedulerEngine")),
    "oservi.long_horizon_agentic_loop": ("oservi", ("AgenticLoopEngine", "CronSchedulerEngine")),
    "graph_skills_agentic_loop": ("oservi", ("AgenticLoopEngine", "ActionPlannerEngine")),
    "oservi.graph_skills_agentic_loop": ("oservi", ("AgenticLoopEngine", "ActionPlannerEngine")),
    # --- AutoAgent capabilities (agent registry / creation / workflow / memory) ---
    "agent_registry": ("obase", ("AgentRegistry", "registry")),
    "obase.agent_registry": ("obase", ("AgentRegistry", "registry")),
    "vector_memory": ("obase", ("VectorMemory",)),
    "obase.vector_memory": ("obase", ("VectorMemory",)),
    "agent_codegen": ("oprim", ("agent_codegen",)),
    "oprim.agent_codegen": ("oprim", ("agent_codegen",)),
    "agent_form_synthesize": ("oskill", ("agent_form_synthesize",)),
    "oskill.agent_form_synthesize": ("oskill", ("agent_form_synthesize",)),
    "meta_self_develop_loop": ("oskill", ("meta_self_develop_loop",)),
    "oskill.meta_self_develop_loop": ("oskill", ("meta_self_develop_loop",)),
    "agent_creation_workflow": ("omodul", ("agent_creation_workflow",)),
    "omodul.agent_creation_workflow": ("omodul", ("agent_creation_workflow",)),
    "orchestrator_creation_workflow": ("omodul", ("orchestrator_creation_workflow",)),
    "omodul.orchestrator_creation_workflow": ("omodul", ("orchestrator_creation_workflow",)),
    "event_workflow_engine": ("oservi", ("EventWorkflowEngine",)),
    "oservi.event_workflow_engine": ("oservi", ("EventWorkflowEngine",)),
    # --- ClawTeam swarm capabilities ---
    "team_registry": ("obase", ("TeamRegistry",)),
    "obase.team_registry": ("obase", ("TeamRegistry",)),
    "p2p_mailbox": ("oprim", ("P2PMailbox",)),
    "oprim.p2p_mailbox": ("oprim", ("P2PMailbox",)),
    "task_router": ("oprim", ("route_tasks", "dispatch_decision")),
    "oprim.task_router": ("oprim", ("route_tasks", "dispatch_decision")),
    "team_plan_gen": ("oskill", ("team_plan_gen",)),
    "oskill.team_plan_gen": ("oskill", ("team_plan_gen",)),
    "team_lifecycle_workflow": ("omodul", ("team_lifecycle_workflow",)),
    "omodul.team_lifecycle_workflow": ("omodul", ("team_lifecycle_workflow",)),    # --- Cindy capabilities (knowledge store / skill teach / scheduler / MCP) ---
    "knowledge_store": ("obase", ("KnowledgeStore",)),
    "obase.knowledge_store": ("obase", ("KnowledgeStore",)),
    "skill_teach": ("oskill", ("skill_teach",)),
    "oskill.skill_teach": ("oskill", ("skill_teach",)),
    "recurring_scheduler": ("oskill", ("RecurringScheduler", "recurring_scheduler")),
    "oskill.recurring_scheduler": ("oskill", ("RecurringScheduler", "recurring_scheduler")),
    "cindy_mcp_server": ("omodul", ("CindyMcpServer", "build_memory_mcp_server", "build_scheduler_mcp_server")),
    "omodul.cindy_mcp_server": ("omodul", ("CindyMcpServer", "build_memory_mcp_server", "build_scheduler_mcp_server")),    # --- plugin registry + skills inject ---
    "plugin_registry": ("obase", ("PluginRegistry",)),
    "obase.plugin_registry": ("obase", ("PluginRegistry",)),
    "scheduler_attempt_lifecycle": ("oskill", ("transition_attempt", "monthly_clamp", "retry_execute", "pre_run_knowledge_hook")),
    "oskill.scheduler_attempt_lifecycle": ("oskill", ("transition_attempt", "monthly_clamp", "retry_execute", "pre_run_knowledge_hook")),
}



def resolve_element(spec: str) -> Any | None:
    """Resolve a Layer-4 element spec name to the real 3O symbol, or None.

    Never raises: missing library / heavy optional dep → ``None`` (the caller
    falls back to a Layer-4 adapter or reports the element as unavailable).
    """
    if spec not in ELEMENT_ALIASES:
        return None
    lib, candidates = ELEMENT_ALIASES[spec]
    try:
        mod = load(lib)
    except Exception:
        return None
    for name in candidates:
        try:
            obj = getattr(mod, name)
        except Exception:
            # Lazy loaders raise ModuleNotFoundError / ImportError for elements
            # whose heavy optional deps are missing — treat as unresolvable.
            continue
        if callable(obj):
            return obj
    return None



def element_status() -> dict[str, str]:
    """Probe every spec element and report resolution status (for dry-run UI)."""
    out: dict[str, str] = {}
    for spec in ELEMENT_ALIASES:
        out[spec] = "resolved" if resolve_element(spec) is not None else "unavailable"
    return out



def save_decision_trail(session_id: str, steps: list[dict], *, out_dir: Path | None = None) -> Path:
    """Persist a decision trail (JSONL).  ``~/.veya/trails`` is the canonical
    Layer-4 store; ``obase.Trail`` is additionally used when mounted so the
    3O decision-logger element stays in the loop (best-effort, non-fatal)."""
    out_dir = out_dir or Path.home() / ".veya" / "trails"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{session_id}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for step in steps:
            fh.write(json.dumps(step, ensure_ascii=False, default=str) + "\n")
    trail = resolve_element("obase.decision_logger")
    if trail is not None:
        try:
            t = trail(run_id=session_id[:8])
            for step in steps:
                t.emit(step.get("event", "step"), **{k: v for k, v in step.items() if k != "event"})
        except Exception:
            pass  # best-effort only — canonical store already written
    return path



def load_decision_trail(session_id: str, *, trail_dir: Path | None = None) -> list[dict]:
    """Load a persisted decision trail (JSONL under ``~/.veya/trails``)."""
    trail_dir = trail_dir or Path.home() / ".veya" / "trails"
    path = trail_dir / f"{session_id}.jsonl"
    if not path.exists():
        return []
    steps: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                steps.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return steps



def new_session_id() -> str:
    """Fresh short session id (uuid7-style when obase available, else uuid4)."""
    try:
        obase_mod = load("obase")
        gen = getattr(obase_mod, "uuid7", None)
        if gen is not None:
            return str(gen())
    except Exception:
        pass
    return uuid.uuid4().hex[:12]



_G13_ALIASES: dict[str, tuple[str, tuple[str, ...]]] = {
    # oprim — voice/vision atomic operations
    "oprim.vad_frame": ("oprim", ("vad_frame", "vad_energy")),
    "oprim.encode_image_base64": ("oprim", ("encode_image_base64", "encode_image_bytes_base64")),
    "oprim.validate_image": ("oprim", ("validate_image",)),
    # oskill — composite voice/vision pipelines
    "oskill.speech_to_text": ("oskill", ("speech_to_text",)),
    "oskill.text_to_speech": ("oskill", ("text_to_speech",)),
    "oskill.analyze_image": ("oskill", ("analyze_image",)),
    "oskill.turn_detection": ("oskill", ("TurnDetector", "detect_turn_end")),
    "oskill.audio_pipeline": ("oskill", ("AudioPipeline", "create_audio_pipeline")),
    # omodul — end-to-end voice/vision modules
    "omodul.run_voice_conversation": ("omodul", ("run_voice_conversation",)),
    "omodul.run_vision_analysis": ("omodul", ("run_vision_analysis",)),
    "omodul.run_multimodal_session": ("omodul", ("run_multimodal_session",)),
    # oservi — voice/vision engine skeletons
    "oservi.voice_agent": ("oservi", ("VoiceAgentEngine",)),
    "oservi.vision_agent": ("oservi", ("VisionAgentEngine",)),
}



_G14_ALIASES: dict[str, tuple[str, tuple[str, ...]]] = {
    # oprim — browser atomic operations
    "oprim.browser_navigate": ("oprim", ("action_navigate",)),
    "oprim.browser_click": ("oprim", ("action_click",)),
    "oprim.browser_screenshot": ("oprim", ("action_screenshot",)),
    "oprim.browser_extract_text": ("oprim", ("action_extract_text",)),
    "oprim.browser_build_selector": ("oprim", ("build_selector",)),
    # oskill — browser pipeline + agent spawner
    "oskill.browser_session": ("oskill", ("BrowserSession",)),
    "oskill.browser_pipeline": ("oskill", ("BrowserPipeline", "run_browser_task")),
    "oskill.agent_spawner": ("oskill", ("AgentSpawner", "spawn")),
    "oskill.agent_discovery": ("oskill", ("discover_agents", "list_agents")),
    # omodul — browser agent module
    "omodul.run_browser_automation": ("omodul", ("run_browser_automation",)),
    # oservi — spawn orchestration
    "oservi.spawn_orchestrator": ("oservi", ("SubagentOrchestratorEngine",)),
}



ELEMENT_ALIASES.update(_G13_ALIASES)

ELEMENT_ALIASES.update(_G14_ALIASES)
