"""veya.server.manifests — ServiceManifest assembly config (Layer 4, SPEC §8).

This is the **assembly point** that injects 3O main-library elements into the
``oservi`` scheduling engines via declarative ``ServiceManifest`` + dependency
injection (DI).  No business logic lives here: every injection is a reference
to a 3O element (resolved lazily through ``veya.platform``) or a thin Layer-4
adapter bridging the engine calling convention to the element signature.

Element spec names referenced by the Layer-4 plan (e.g. ``oprim.llm_chat_call``)
are resolved to the **real mounted 3O symbols** (``oprim.llm_complete`` /
``oprim.llm_stream``).  Resolution is lazy and graceful: when a main library or
one of its heavy optional dependencies is not installed, ``resolve_element``
returns ``None`` and the manifest still builds with a Layer-4 fallback element,
so CLI / server / IM surfaces work offline and in CI.

Two assembly entry points are provided:

- ``build_agentic_loop_manifest`` — the *agentic loop* engine (think → act →
  observe) wired with ``llm_chat_call`` as the LLM caller, ``mcp_tool_route``
  as the tool router, sandbox / code-investigation workflows as execution
  nodes, and ``cost_tracker`` + ``decision_logger`` bound for observability.
- ``build_multi_agent_dag_manifest`` — the *multi-agent DAG* orchestrator that
  splits a task with ``task_dag_split`` and runs the sub-tasks concurrently in
  isolated git worktrees (``multi_agent_worktree_workflow`` + ``git_worktree``).

The result of ``assemble`` is an engine exposing the canonical async surface
(``invoke`` / ``run_turn`` / ``orchestrate``) — all of which can be bridged to
SSE streaming by the server gateway.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veya.platform import load

# ---------------------------------------------------------------------------
# Element spec → real 3O symbol resolution
# ---------------------------------------------------------------------------
# spec name (as referenced by the Layer 4 plan) → (main library, candidates)
# Candidates are tried in order; the first resolvable symbol wins.
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


# ---------------------------------------------------------------------------
# ServiceManifest — declarative assembly plan (oservi-compatible contract)
# ---------------------------------------------------------------------------


@dataclass
class ServiceManifest:
    """Declarative service definition (mirrors ``oservi.ServiceManifest``).

    Args:
        name: Unique service identifier.
        skeleton: Engine skeleton name (e.g. ``"agentic_loop"``).
        inject: Injection dict — key = skeleton injection point, value =
            callable or list of callables (3O element or Layer-4 adapter).
        trigger: Trigger configuration (``{"on_demand": True}`` for request/
            response engines).
        config: Business instance parameters (cost tracker, decision logger,
            model, budget, ...).
        depends_on: Declarative dependency topology (other services/resources).
    """

    name: str
    skeleton: str
    inject: dict[str, Any] = field(default_factory=dict)
    trigger: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ServiceManifest.name cannot be empty")
        if not self.skeleton:
            raise ValueError("ServiceManifest.skeleton cannot be empty")
        if not isinstance(self.inject, dict):
            raise TypeError("ServiceManifest.inject must be dict")
        if not isinstance(self.trigger, dict):
            raise TypeError("ServiceManifest.trigger must be dict")


class ManifestValidationError(Exception):
    """Manifest validation failure (pre-assembly)."""


def _required_injections(skeleton: str) -> dict[str, str]:
    """Injection points declared by a skeleton: name → cardinality."""
    table: dict[str, dict[str, str]] = {
        "agentic_loop": {
            "llm_caller": "1",
            "tools": "1..n",
            "retrieval": "0..1",
            "turn_handler": "1",
            "layer4_ui": "0..1",
        },
        "subagent_orchestrator": {
            "subagent_runner": "1",
            "llm_caller": "1",
            "scheduler": "0..1",
        },
        "steerable_agentic_loop": {
            "llm_caller": "1",
            "tools": "1..n",
            "retrieval": "0..1",
            "turn_handler": "1",
            "layer4_ui": "0..1",
            # HITL wiring: signal bus + instruction steer + approval gate
            "hitl_signal_bus": "1",
            "hitl_instruction_steer": "1",
            "hitl_approval_gate": "1",
        },
        "swarm_orchestrator": {
            "leader_worker_dispatch": "1",
            "subagent_runner": "1",
            "llm_caller": "1",
            "scheduler": "0..1",
        },
        "realtime_media_loop": {
            "vad_pipeline": "1",
            "stt_transcribe_stream": "1",
            "tts_synthesize_stream": "1",
            "llm_caller": "1",
            "turn_handler": "0..1",
        },
        "neuro_symbolic": {
            "fol_translator": "1",
            "smt_solver": "1",
            "neuro_verifier": "1",
            "llm_caller": "0..1",
        },
        "root_cause_analysis": {
            "causal_graph": "1",
            "root_cause_analyzer": "1",
            "counterfactual_reasoner": "1",
            "llm_caller": "0..1",
        },
        "long_horizon_agentic_loop": {
            "llm_caller": "1",
            "tools": "1..n",
            "retrieval": "0..1",
            "turn_handler": "1",
            "layer4_ui": "0..1",
            # long-horizon wiring: checkpoint snapshots + incremental compression
            "checkpoint_store": "1",
            "context_compressor": "1",
        },
        "graph_skills_agentic_loop": {
            "llm_caller": "1",
            "tools": "1..n",
            "retrieval": "0..1",
            "turn_handler": "1",
            "layer4_ui": "0..1",
            # graph-skills wiring: registry + AST graph parse + coding workflow
            "skills_registry": "1",
            "code_graph_parser": "1",
            "skills_coding_workflow": "1",
            "graph_impact_analysis": "0..1",
        },
        "agent_creation_workflow": {
            "agent_form_synthesize": "1",
            "agent_codegen": "1",
            "agent_registry": "1",
            "llm_caller": "0..1",
        },
        "event_workflow": {
            "event_engine": "1",
            "llm_caller": "0..1",
        },
        "team_lifecycle": {
            "team_registry": "1",
            "task_router": "1",
            "llm_caller": "0..1",
        },
    }
    return table.get(skeleton, {})


def validate_manifest(manifest: ServiceManifest) -> None:
    """Validate cardinality of a manifest against the skeleton contract."""
    points = _required_injections(manifest.skeleton)
    for point, cardinality in points.items():
        refs = manifest.inject.get(point)
        if refs is None:
            if not cardinality.startswith("0"):
                raise ManifestValidationError(
                    f"required injection '{point}' ({cardinality}) not provided "
                    f"for skeleton '{manifest.skeleton}'"
                )
            continue
        count = len(refs) if isinstance(refs, list) else 1
        if cardinality == "1" and count != 1:
            raise ManifestValidationError(
                f"injection '{point}' requires cardinality=1, got {count}"
            )
        if cardinality == "1..n" and count < 1:
            raise ManifestValidationError(
                f"injection '{point}' requires cardinality=1..n, got {count}"
            )
        if cardinality == "0..1" and count > 1:
            raise ManifestValidationError(
                f"injection '{point}' requires cardinality=0..1, got {count}"
            )
    # No undeclared injection keys (anti-typo)
    for point in manifest.inject:
        if point not in points:
            raise ManifestValidationError(
                f"injection '{point}' not declared in skeleton "
                f"'{manifest.skeleton}'. Declared: {sorted(points)}"
            )


# ---------------------------------------------------------------------------
# Layer-4 adapters (thin bridges; no business logic)
#
# The engines call injection points with a uniform convention (e.g.
# ``llm_caller(messages, tools, config)`` / ``subagent_runner(task, config)``),
# while the 3O elements use their own signatures (e.g. omodul's
# ``(config, input_data, output_dir)`` triplet).  These adapters bridge the two,
# preferring the real 3O element when it can be driven, and degrading to a
# deterministic Layer-4 fallback offline (no API key / heavy dep missing).
# ---------------------------------------------------------------------------


def _async(fn: Callable) -> Callable[..., Awaitable[Any]]:
    """Wrap a sync callable into an async one (engine contract tolerance)."""

    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        out = fn(*args, **kwargs)
        if hasattr(out, "__await__"):
            return await out
        return out

    return _wrapped


def _mark(fn: Callable, module: str, name: str) -> Callable:
    """Mark a Layer-4 bridge with the 3O element provenance it stands in for."""
    fn.__module__ = module
    fn.__name__ = name
    return fn


def _run_output_dir() -> Path:
    """Dedicated Layer-4 run dir so omodul trails never pollute the cwd."""
    d = Path.home() / ".veya" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_turn_handler() -> Callable | None:
    """Resolve omodul process_prompt without hard-importing heavy deps."""
    try:
        import omodul  # type: ignore[import-not-found]

        return getattr(omodul, "process_prompt", None)
    except Exception:
        return None


def _resolve_retrieval() -> Callable | None:
    """Resolve oskill code_search without hard-importing heavy deps."""
    try:
        import oskill  # type: ignore[import-not-found]

        return getattr(oskill, "code_search", None)
    except Exception:
        return None


def _identity_turn_handler() -> Callable:
    """Default turn handler: pass messages through unchanged."""

    async def turn_handler(messages: list[dict], context: dict | None = None) -> dict[str, Any]:
        return {"messages": list(messages)}

    return _mark(turn_handler, "omodul.process_prompt", "process_prompt")


def _make_llm_caller(real_llm: Callable | None) -> Callable:
    """Bridge the engine ``(messages, tools, config)`` convention to the LLM.

    Primary: the real ``oprim.llm_chat_call`` element (``llm_complete``) when a
    ``caller`` is configured.  Fallback: ``veya.llm.llm_call`` which stubs
    gracefully when no API key is set (keeps offline / CI runs deterministic).
    """

    async def llm_caller(
        messages: list[dict], tools: list | None = None, config: dict | None = None
    ) -> dict[str, Any]:
        config = config or {}
        caller = config.get("caller")
        if real_llm is not None and caller is not None:
            try:
                resp = await real_llm(messages, caller=caller, tools=tools)
                # oprim.llm_complete returns an LLMResponse dataclass
                text = getattr(resp, "text", None)
                if text is None and isinstance(resp, dict):
                    text = resp.get("content", "")
                return {
                    "content": text or "",
                    "tool_calls": getattr(resp, "tool_calls", []) or [],
                    "cost_usd": float(getattr(resp, "cost_usd", 0.0) or 0.0),
                }
            except Exception:
                pass  # fall through to the graceful stub path
        from veya.llm import llm_call

        out = await llm_call(messages, tools=tools, config=config)
        if isinstance(out, dict):
            choices = out.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices else {}
            return {
                "content": msg.get("content") or "",
                "tool_calls": msg.get("tool_calls") or [],
                "cost_usd": float(out.get("cost_usd", 0.0) or 0.0),
            }
        return {"content": str(out), "tool_calls": [], "cost_usd": 0.0}

    return _mark(llm_caller, "oprim.llm_complete", "llm_chat_call")


def _make_turn_handler(real_turn: Callable | None) -> Callable:
    """Bridge the engine ``(messages, context)`` convention to an omodul turn."""

    async def turn_handler(messages: list[dict], context: dict | None = None) -> dict[str, Any]:
        if real_turn is not None:
            try:
                from omodul.process_prompt import Config as TurnConfig
                from omodul.process_prompt import InputData as TurnInput

                result = await real_turn(
                    TurnConfig(),
                    TurnInput(messages=list(messages), tools=[], llm_caller=None),
                    _run_output_dir(),
                )
                if isinstance(result, dict) and result.get("status") == "completed":
                    return {"messages": messages, "response": result.get("result", "")}
            except Exception:
                pass  # fall back to pass-through
        return {"messages": list(messages)}

    return _mark(turn_handler, "omodul.process_prompt", "process_prompt")


def _stub_tool_router() -> Callable:
    """Layer-4 tool router stub: routes an MCP-style call or reports absence."""

    async def tool_router(name: str, args: dict | None = None, **kwargs: Any) -> dict:
        return {"tool": name, "args": args or {}, "status": "unavailable"}

    return _mark(tool_router, "oskill.mcp_tool_invoke", "mcp_tool_route")


def _make_tool_router(real_router: Callable | None) -> Callable:
    """Bridge the engine tool-route convention to ``oskill.mcp_tool_route``."""

    async def tool_router(name: str, args: dict | None = None, **kwargs: Any) -> dict:
        if real_router is not None:
            try:
                # oskill.mcp_tool_invoke(session, *, name, args) needs an MCP
                # session — without one we report availability instead.
                return {
                    "tool": name,
                    "args": args,
                    "router": "oskill.mcp_tool_invoke",
                    "status": "ready",
                }
            except Exception:
                pass
        return {"tool": name, "args": args or {}, "status": "unavailable"}

    return _mark(tool_router, "oskill.mcp_tool_invoke", "mcp_tool_route")


def _stub_executor() -> Callable:
    """Layer-4 sandbox execution stub (real: ``omodul.execute_tool``)."""

    async def sandbox_execution(config: Any, input_data: Any, output_dir: Path) -> dict:
        return {"status": "noop", "reason": "omodul.sandbox_execution_workflow unavailable"}

    return _mark(sandbox_execution, "omodul.execute_tool", "sandbox_execution_workflow")


def _make_sandbox_executor(real_node: Callable | None) -> Callable:
    """Bridge the execution-node convention to ``omodul.sandbox_execution_workflow``."""

    async def sandbox_execution(
        config: Any = None, input_data: Any = None, output_dir: Path | None = None
    ) -> dict:
        if real_node is not None and output_dir is not None:
            try:
                return await real_node(config or {}, input_data or {}, Path(output_dir))
            except Exception:
                pass
        return {"status": "noop", "reason": "omodul.sandbox_execution_workflow unavailable"}

    return _mark(sandbox_execution, "omodul.execute_tool", "sandbox_execution_workflow")


def _stub_investigator() -> Callable:
    """Layer-4 code-investigation stub (real: ``omodul.explain_codebase``)."""

    async def code_investigation(config: Any, input_data: Any, output_dir: Path) -> dict:
        return {"status": "noop", "reason": "omodul.code_investigation_workflow unavailable"}

    return _mark(code_investigation, "omodul.explain_codebase", "code_investigation_workflow")


def _make_investigator(real_node: Callable | None) -> Callable:
    """Bridge the investigation-node convention to ``omodul.code_investigation_workflow``."""

    async def code_investigation(
        config: Any = None, input_data: Any = None, output_dir: Path | None = None
    ) -> dict:
        if real_node is not None and output_dir is not None:
            try:
                return await real_node(config or {}, input_data or {}, Path(output_dir))
            except Exception:
                pass
        return {"status": "noop", "reason": "omodul.code_investigation_workflow unavailable"}

    return _mark(code_investigation, "omodul.explain_codebase", "code_investigation_workflow")


def _stub_dag_split() -> Callable:
    """Layer-4 task DAG split stub (real: ``oskill.plan_decompose``)."""

    async def task_dag_split(task: str, **kwargs: Any) -> list[dict]:
        # Keep the unit of work meaningful: single sub-task fallback.
        return [{"id": "t1", "description": task, "depends_on": []}]

    return _mark(task_dag_split, "oskill.plan_decompose", "task_dag_split")


def _make_dag_split(real_split: Callable | None) -> Callable:
    """Bridge the scheduler convention to ``oskill.task_dag_split``."""

    async def task_dag_split(task: str, **kwargs: Any) -> list[dict]:
        if real_split is not None:
            try:
                out = real_split(task, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, list) and out:
                    return [
                        {"id": f"t{i}", "description": str(t), "depends_on": []}
                        for i, t in enumerate(out)
                    ]
            except Exception:
                pass
        return [{"id": "t1", "description": task, "depends_on": []}]

    return _mark(task_dag_split, "oskill.plan_decompose", "task_dag_split")


def _stub_subagent_runner() -> Callable:
    """Layer-4 sub-agent runner stub (real: ``omodul.run_subagent_task``)."""

    async def subagent_runner(task: dict, config: dict | None = None, **kwargs: Any) -> dict:
        return {"status": "noop", "task": task.get("description", ""), "cost_usd": 0.0}

    return _mark(subagent_runner, "omodul.run_subagent_task", "run_subagent_task")


def _make_subagent_runner(real_runner: Callable | None) -> Callable:
    """Bridge the engine ``(task, config)`` convention to ``omodul.run_subagent_task``."""

    async def subagent_runner(task: dict, config: dict | None = None, **kwargs: Any) -> dict:
        if real_runner is not None:
            try:
                from omodul.run_subagent_task import Config as SubConfig
                from omodul.run_subagent_task import InputData as SubInput

                cfg = SubConfig(max_depth=(config or {}).get("max_depth", 3))
                inp = SubInput(
                    task_description=task.get("description", ""),
                    depth=0,
                    parent_cost_tracker=None,
                )
                result = await real_runner(cfg, inp, _run_output_dir())
                return {
                    "status": result.get("status", "completed"),
                    "plan": result.get("plan", {}),
                    "cost_usd": float(result.get("cost_usd", 0.0) or 0.0),
                }
            except Exception:
                pass
        return {"status": "noop", "task": task.get("description", ""), "cost_usd": 0.0}

    return _mark(subagent_runner, "omodul.run_subagent_task", "run_subagent_task")


# ---------------------------------------------------------------------------
# HITL + swarm + realtime-media adapters (ultimate-assembly bridges)
# ---------------------------------------------------------------------------


class _HitlSignalBus:
    """Layer-4 in-process HITL signal bus (fallback for ``obase.hitl_signal_bus``).

    Holds pending approval gates keyed by approval id; ``decide()`` resolves a
    gate with approve / reject / steer_instruction.  Pending gates can be
    listed by the gateway so the frontend can render live approval prompts.
    """

    def __init__(self) -> None:
        import asyncio

        self._events: dict[str, asyncio.Event] = {}
        self._decisions: dict[str, dict[str, Any]] = {}
        self._steer_instructions: list[str] = []
        self._lock = asyncio.Lock()

    async def request(self, action: dict[str, Any]) -> str:
        """Open a pending approval gate; returns the approval id."""
        import asyncio
        import uuid

        approval_id = uuid.uuid4().hex[:8]
        async with self._lock:
            self._events[approval_id] = asyncio.Event()
        return approval_id

    async def decide(
        self, approval_id: str, action: str, instruction: str | None = None
    ) -> dict[str, Any]:
        """Resolve a pending gate: ``approve`` / ``reject`` / ``steer``."""
        async with self._lock:
            ev = self._events.get(approval_id)
            if ev is None:
                return {"status": "unknown_approval", "approval_id": approval_id}
            decision = {
                "approval_id": approval_id,
                "action": action,
                "instruction": instruction,
                "resolved_at": time.time(),
            }
            if action == "steer" and instruction:
                self._steer_instructions.append(instruction)
            self._decisions[approval_id] = decision
            self._events.pop(approval_id, None)
            ev.set()
        return {"status": "resolved", **decision}

    async def wait(self, approval_id: str, timeout: float = 30.0) -> dict[str, Any] | None:
        """Block until the gate is decided (or timeout → auto-approve)."""
        import asyncio

        ev = self._events.get(approval_id)
        if ev is None:
            return self._decisions.get(approval_id)
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except TimeoutError:
            # deterministic default: auto-approve on timeout (configurable)
            return await self.decide(approval_id, "approve", None)
        return self._decisions.get(approval_id)

    def pending(self) -> list[dict[str, Any]]:
        return [{"approval_id": aid, "action": "pending_approval"} for aid in self._events]

    def pop_steer_instructions(self) -> list[str]:
        out, self._steer_instructions = list(self._steer_instructions), []
        return out

    def queue_steer(self, instruction: str) -> None:
        """Queue a steer instruction for the next turn (no pending gate)."""
        self._steer_instructions.append(instruction)


def _make_hitl_signal_bus(real_bus: Callable | None) -> _HitlSignalBus:
    """Return the HITL signal bus: real ``obase.hitl_signal_bus`` element when
    mounted, else the Layer-4 in-process bus (deterministic offline fallback)."""
    if real_bus is not None:
        try:
            bus = real_bus()
            if bus is not None:
                return bus  # type: ignore[return-value]
        except Exception:
            pass
    return _HitlSignalBus()


def _make_hitl_instruction_steer(real_steer: Callable | None) -> Callable:
    """Instruction fusion: merge a steer instruction into the task context.

    Primary: ``oskill.hitl_instruction_steer`` element.  Fallback: deterministic
    merge that re-anchors the goal and records the steer for the decision trail.
    """

    async def hitl_instruction_steer(
        task: dict[str, Any], instruction: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        merged = dict(task)
        if instruction:
            goal = str(merged.get("goal", ""))
            merged["goal"] = f"{goal}\n[steer] {instruction}" if goal else instruction
            merged["steer_instruction"] = instruction
        if real_steer is not None:
            try:
                out = real_steer(merged, instruction=instruction, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
        return merged

    return _mark(hitl_instruction_steer, "oskill.hitl_instruction_steer", "hitl_instruction_steer")


def _make_hitl_approval_gate(
    real_gate: Callable | None, bus: _HitlSignalBus, default_timeout: float = 30.0
) -> Callable:
    """Approval gate: block an action until the operator decides.

    Primary: ``omodul.hitl_approval_workflow`` element.  Fallback: gate through
    the Layer-4 signal bus (timeout auto-approve, ``hitl_mode=strict`` disables
    auto-approve in favour of a bounded wait).
    """

    async def hitl_approval_gate(
        action: dict[str, Any], context: dict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if real_gate is not None:
            try:
                out = real_gate(action, context or {}, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
        ctx = context or {}
        timeout = float(ctx.get("hitl_approval_timeout", default_timeout))
        approval_id = await bus.request(action)
        decision = await bus.wait(approval_id, timeout=timeout)
        if decision is None:
            decision = {"approval_id": approval_id, "action": "approve"}
        if decision.get("action") == "reject":
            return {
                "status": "rejected",
                "approval_id": approval_id,
                "reason": decision.get("instruction") or "operator rejected",
            }
        return {
            "status": "approved",
            "approval_id": approval_id,
            "instruction": decision.get("instruction"),
        }

    return _mark(hitl_approval_gate, "omodul.hitl_approval_workflow", "hitl_approval_gate")


def _make_leader_worker_dispatch(real_dispatch: Callable | None) -> Callable:
    """Leader-worker dispatch: split a goal into worker tasks.

    Primary: ``oskill.leader_worker_dispatch`` element.  Fallback: deterministic
    single-worker dispatch (the orchestrate stage still isolates each task).
    """

    async def leader_worker_dispatch(
        goal: str, context: dict | None = None, **kwargs: Any
    ) -> list[dict]:
        if real_dispatch is not None:
            try:
                out = real_dispatch(goal, context or {}, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, list) and out:
                    return [
                        {"id": f"w{i}", "description": str(t), "depends_on": []}
                        for i, t in enumerate(out)
                    ]
            except Exception:
                pass
        return [{"id": "w1", "description": goal, "depends_on": []}]

    return _mark(leader_worker_dispatch, "oskill.leader_worker_dispatch", "leader_worker_dispatch")


def _make_vad_pipeline(real_vad: Callable | None) -> Callable:
    """Voice-activity detection pipeline (``obase.vad_pipeline``).

    Fallback: deterministic passthrough — every supplied audio frame belongs to
    a single utterance (marked ``vad=True``) so the downstream STT chain runs.
    """

    async def vad_pipeline(
        frames: list[dict[str, Any]], context: dict | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        if real_vad is not None:
            try:
                out = real_vad(frames, context or {}, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, list):
                    return out
            except Exception:
                pass
        return [{**frame, "vad": True, "utterance_id": "u1"} for frame in (frames or [])]

    return _mark(vad_pipeline, "obase.vad_pipeline", "vad_pipeline")


def _make_stt_stream(real_stt: Callable | None) -> Callable:
    """Streaming speech-to-text (``oprim.stt_transcribe_stream``).

    Primary: ``oprim.transcribe_audio`` / ``speech_to_text``.  Fallback:
    deterministic stub (empty transcript, status marks unavailability).
    """

    async def stt_transcribe_stream(
        utterance: dict[str, Any], context: dict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if real_stt is not None:
            try:
                audio = utterance.get("audio") or utterance.get("path")
                if audio is not None:
                    out = real_stt(audio, **kwargs)
                    if hasattr(out, "__await__"):
                        out = await out
                    text = out if isinstance(out, str) else getattr(out, "text", None)
                    if text:
                        return {"text": str(text), "status": "transcribed"}
            except Exception:
                pass
        return {"text": "", "status": "unavailable"}

    return _mark(stt_transcribe_stream, "oprim.stt_transcribe_stream", "stt_transcribe_stream")


def _make_tts_stream(real_tts: Callable | None) -> Callable:
    """Streaming text-to-speech (``oprim.tts_synthesize_stream``).

    Primary: ``oprim.tts_synthesize`` / ``text_to_speech``.  Fallback:
    deterministic stub (text echoed, no audio, status marks unavailability).
    """

    async def tts_synthesize_stream(
        text: str, context: dict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if real_tts is not None:
            try:
                out = real_tts(text, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                audio = getattr(out, "audio", None) or (
                    out.get("audio") if isinstance(out, dict) else None
                )
                if audio is not None:
                    return {"text": str(text), "audio": audio, "status": "synthesized"}
            except Exception:
                pass
        return {"text": str(text), "audio": None, "status": "unavailable"}

    return _mark(tts_synthesize_stream, "oprim.tts_synthesize_stream", "tts_synthesize_stream")


# ---------------------------------------------------------------------------
# Frontier adapters — neuro-symbolic verification & root-cause analysis
# ---------------------------------------------------------------------------


def _make_fol_translator(real_fol: Callable | None) -> Callable:
    """First-order-logic translator (``oprim.fol_translate``).

    Primary: the mounted 3O element.  Fallback: deterministic structural
    response that reports the stage as unavailable (the SMT stage then yields
    ``inconclusive`` — the pipeline shape is always exercised).
    """

    async def fol_translator(
        statement: str, context: dict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if real_fol is not None:
            try:
                out = real_fol(statement, context or {}, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, str):
                    formula = out
                elif isinstance(out, dict):
                    formula = out.get("formula")
                else:
                    formula = getattr(out, "formula", None)
                if formula:
                    return {"statement": statement, "formula": str(formula), "status": "translated"}
            except Exception:
                pass
        return {
            "statement": statement,
            "formula": None,
            "status": "unavailable",
            "reason": "oprim.fol_translate not mounted",
        }

    return _mark(fol_translator, "oprim.fol_translate", "fol_translate")


def _make_smt_solver_adapter(real_smt: Callable | None) -> Callable:
    """SMT solver adapter (``obase.smt_solver_adapter``).

    Primary: ``oprim.solve_function`` / ``solve_trig`` / ``solve_sequence``.
    Fallback: deterministic ``inconclusive`` verdict marking unavailability.
    """

    async def smt_solver(
        formula: str | None, context: dict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if real_smt is not None and formula:
            try:
                out = real_smt(formula, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, dict) and out.get("status") == "solved":
                    return {"formula": formula, "verdict": "sat", "model": out, "status": "solved"}
                if isinstance(out, dict) and out.get("status") == "unsatisfiable":
                    return {
                        "formula": formula,
                        "verdict": "unsat",
                        "model": None,
                        "status": "solved",
                    }
            except Exception:
                pass
        return {
            "formula": formula,
            "verdict": "inconclusive",
            "status": "unavailable",
            "reason": "obase.smt_solver_adapter not mounted",
        }

    return _mark(smt_solver, "obase.smt_solver_adapter", "smt_solver_adapter")


def _make_neuro_symbolic_verifier(real_verify: Callable | None) -> Callable:
    """Neuro-symbolic verifier (``oskill.neuro_symbolic_verify``).

    Combines the FOL formula with the SMT verdict: ``unsat`` on the negated
    claim ⇒ verified; ``sat`` ⇒ refuted (counterexample exists); anything else
    ⇒ inconclusive.  Primary: ``oskill.formal_proof_verify`` /
    ``theorem_verify_3way`` when mounted.
    """

    async def neuro_symbolic_verify(
        statement: str,
        fol_result: dict[str, Any],
        smt_result: dict[str, Any],
        context: dict | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        formula = (fol_result or {}).get("formula")
        verdict = (smt_result or {}).get("verdict")
        if real_verify is not None and formula and verdict in ("sat", "unsat"):
            try:
                out = real_verify(statement, formula=formula, verdict=verdict, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, dict) and out.get("verdict"):
                    return out
            except Exception:
                pass
        if formula and verdict == "unsat":
            return {
                "verdict": "verified",
                "confidence": 1.0,
                "reasoning": "negation unsatisfiable ⇒ statement holds",
                "status": "verified",
            }
        if formula and verdict == "sat":
            return {
                "verdict": "refuted",
                "confidence": 1.0,
                "reasoning": "negation satisfiable ⇒ counterexample exists",
                "status": "refuted",
            }
        return {
            "verdict": "inconclusive",
            "confidence": 0.0,
            "reasoning": "fol/smt stages unavailable offline",
            "status": "inconclusive",
        }

    return _mark(neuro_symbolic_verify, "oskill.neuro_symbolic_verify", "neuro_symbolic_verify")


def _make_causal_graph_builder(real_builder: Callable | None) -> Callable:
    """Causal graph builder (``oprim.causal_graph_build``).

    Primary: ``oskill.causal_discovery`` / ``pcmci_causal_discovery`` /
    ``structural_causal_model_fit``.  Fallback: deterministic adjacency graph
    derived from the event sequence (event[i] → event[i+1] edges).
    """

    async def causal_graph_build(
        events: list[dict[str, Any]], context: dict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if real_builder is not None:
            try:
                out = real_builder(events, context or {}, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, dict) and out.get("graph"):
                    return out
            except Exception:
                pass
        nodes: list[str] = []
        edges: list[dict[str, str]] = []
        kinds = [str(e.get("event", "step")) for e in (events or [])]
        for kind in kinds:
            if kind not in nodes:
                nodes.append(kind)
        for i in range(max(0, len(kinds) - 1)):
            edges.append({"from": kinds[i], "to": kinds[i + 1], "type": "sequence"})
        return {"graph": {"nodes": nodes, "edges": edges}, "status": "built"}

    return _mark(causal_graph_build, "oprim.causal_graph_build", "causal_graph_build")


def _make_root_cause_analyzer(real_analyzer: Callable | None) -> Callable:
    """Root-cause analyzer (``omodul.root_cause_analysis_workflow``).

    Primary: ``oskill.ic_root_cause_decompose``.  Fallback: deterministic
    frequency-based attribution — error-kind events are ranked by occurrence
    and normalized to a 0..1 score.
    """

    async def root_cause_analysis_workflow(
        events: list[dict[str, Any]],
        graph: dict[str, Any] | None = None,
        context: dict | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if real_analyzer is not None:
            try:
                out = real_analyzer(events, graph or {}, context or {}, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, dict) and out.get("root_causes"):
                    return out
            except Exception:
                pass
        counts: dict[str, int] = {}
        for e in events or []:
            if e.get("event") in ("error", "failed", "reject"):
                key = str(e.get("detail") or e.get("event"))
                counts[key] = counts.get(key, 0) + 1
        total = sum(counts.values()) or 1
        causes = [
            {"cause": k, "score": round(v / total, 3), "evidence": v}
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
        ]
        return {"root_causes": causes, "status": "analyzed"}

    return _mark(
        root_cause_analysis_workflow,
        "omodul.root_cause_analysis_workflow",
        "root_cause_analysis_workflow",
    )


def _make_counterfactual_reasoner(real_reasoner: Callable | None) -> Callable:
    """Counterfactual reasoner (``oskill.counterfactual_reasoning``).

    Primary: ``oskill.counterfactual_generator``.  Fallback: deterministic
    what-if scenario template per root cause.
    """

    async def counterfactual_reasoning(
        cause: dict[str, Any], context: dict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if real_reasoner is not None:
            try:
                out = real_reasoner(cause, context or {}, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, dict) and out.get("scenario"):
                    return out
            except Exception:
                pass
        name = cause.get("cause", "the failure")
        return {
            "cause": name,
            "scenario": f"if {name} had not occurred, the failure chain would not propagate",
            "impact": "mitigated",
            "status": "projected",
        }

    return _mark(
        counterfactual_reasoning, "oskill.counterfactual_reasoning", "counterfactual_reasoning"
    )


# ---------------------------------------------------------------------------
# Long-horizon & graph-skills adapters (30-element batch)
# ---------------------------------------------------------------------------


class _CheckpointStore:
    """Layer-4 checkpoint store (fallback for ``obase.checkpoint_store``).

    Disk-backed JSON snapshots under ``~/.veya/checkpoints`` so checkpoints
    survive engine instances and gateway restarts (session-keyed).
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base = base_dir or (Path.home() / ".veya" / "checkpoints")
        self._base.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe(key: str) -> str:
        return key.replace("/", "_").replace(":", "_")

    def save(self, key: str, state: dict[str, Any]) -> str:
        (self._base / f"{self._safe(key)}.json").write_text(
            json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return key

    def load(self, key: str) -> dict[str, Any] | None:
        path = self._base / f"{self._safe(key)}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def keys(self) -> list[str]:
        return sorted(p.stem for p in self._base.glob("*.json"))

    def reset(self, key: str | None = None) -> None:
        if key is None:
            for p in self._base.glob("*.json"):
                p.unlink(missing_ok=True)
        else:
            (self._base / f"{self._safe(key)}.json").unlink(missing_ok=True)


def _make_checkpoint_store(real_store: Callable | None) -> _CheckpointStore:
    """Return the checkpoint store: real ``obase.checkpoint_store`` element when
    constructible, else the Layer-4 in-memory store (deterministic offline)."""
    if real_store is not None:
        try:
            store = real_store()
            if store is not None and hasattr(store, "save") and hasattr(store, "load"):
                return store  # type: ignore[return-value]
        except Exception:
            pass
    return _CheckpointStore()


def _make_context_compressor(real_compress: Callable | None) -> Callable:
    """Incremental long-context compressor (``oskill.long_context_compress``).

    Fallback: deterministic head+tail window (keeps the system prefix and the
    newest ``max_tail`` messages) plus a running digest of the discarded span.
    """

    async def long_context_compress(
        messages: list[dict[str, Any]], context: dict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if real_compress is not None:
            try:
                out = real_compress(messages, context or {}, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, dict) and out.get("messages"):
                    return out
                if isinstance(out, list) and out:
                    return {"messages": out, "status": "compressed"}
            except Exception:
                pass
        msgs = list(messages or [])
        head = 1  # keep the system prefix
        tail = int((context or {}).get("max_tail", 8))
        if len(msgs) <= head + tail:
            return {"messages": msgs, "summary": "", "status": "unchanged"}
        kept = msgs[:head] + msgs[-tail:]
        dropped = len(msgs) - len(kept)
        return {
            "messages": kept,
            "summary": f"[{dropped} earlier messages compressed]",
            "status": "compressed",
        }

    return _mark(long_context_compress, "oskill.long_context_compress", "long_context_compress")


class _SkillsRegistry:
    """Layer-4 skills registry (fallback for ``obase.skills_registry``).

    Rule matching: each skill registers a trigger pattern; ``match`` returns
    the skills whose pattern is a substring of the task (deterministic).
    """

    def __init__(self) -> None:
        self._rules: dict[str, str] = {}

    def register(self, skill: str, rule: str) -> None:
        self._rules[skill] = rule

    def match(self, task: str) -> list[str]:
        t = task or ""
        return sorted(s for s, r in self._rules.items() if r and r in t)

    def rules(self) -> dict[str, str]:
        return dict(self._rules)


def _make_skills_registry(real_registry: Callable | None) -> _SkillsRegistry:
    """Return the skills registry: real ``obase.skills_registry`` element when
    constructible, else the Layer-4 rule registry (deterministic offline)."""
    if real_registry is not None:
        try:
            registry = real_registry()
            if registry is not None and hasattr(registry, "match"):
                return registry  # type: ignore[return-value]
        except Exception:
            pass
    return _SkillsRegistry()


def _make_code_graph_parser(real_parser: Callable | None) -> Callable:
    """AST code-graph parser (``oprim.code_graph_parse``).

    Fallback: deterministic structural parse — import / from / def / class
    lines become graph nodes; imports connect to same-file definitions and to
    cross-file nodes by symbol name.
    """

    async def code_graph_parse(
        files: list[dict[str, Any]], context: dict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if real_parser is not None:
            try:
                out = real_parser(files, context or {}, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, dict) and out.get("graph"):
                    return out
            except Exception:
                pass
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        for f in files or []:
            path = str(f.get("path", "?"))
            content = str(f.get("content", ""))
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    name = stripped.split()[1].split(".")[0]
                    node = {"type": "import", "name": name, "file": path}
                    if node not in nodes:
                        nodes.append(node)
                elif stripped.startswith("def ") or stripped.startswith("class "):
                    name = stripped.split()[1].split("(")[0]
                    node = {"type": "def", "name": name, "file": path}
                    if node not in nodes:
                        nodes.append(node)
        # imports → definitions edges (cross-file by symbol name)
        def_names = {n["name"] for n in nodes if n["type"] == "def"}
        for n in nodes:
            if n["type"] == "import" and n["name"] in def_names:
                edges.append({"from": n["name"], "to": n["name"], "type": "imports"})
        return {"graph": {"nodes": nodes, "edges": edges}, "status": "parsed"}

    return _mark(code_graph_parse, "oprim.code_graph_parse", "code_graph_parse")


def _make_graph_impact_analysis(real_analysis: Callable | None) -> Callable:
    """Graph impact analysis (``oskill.graph_impact_analysis``).

    Fallback: deterministic BFS reachability from the seed nodes over the
    graph edges (impacted = every node reachable).
    """

    async def graph_impact_analysis(
        graph: dict[str, Any], seed_nodes: list[str], context: dict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if real_analysis is not None:
            try:
                out = real_analysis(graph, seed_nodes, context or {}, **kwargs)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, dict) and out.get("impacted"):
                    return out
            except Exception:
                pass
        edges = graph.get("edges", []) if isinstance(graph, dict) else []
        impacted: list[str] = []
        queue = list(seed_nodes or [])
        seen: set[str] = set()
        while queue:
            node = queue.pop(0)
            if node in seen:
                continue
            seen.add(node)
            impacted.append(node)
            for e in edges:
                if e.get("from") == node and e.get("to") not in seen:
                    queue.append(e["to"])
        return {"impacted": impacted, "edges_traversed": len(impacted), "status": "analyzed"}

    return _mark(graph_impact_analysis, "oskill.graph_impact_analysis", "graph_impact_analysis")


def _make_skills_coding_workflow(real_workflow: Callable | None) -> Callable:
    """Skills-guided coding workflow (``omodul.skills_guided_coding_workflow``).

    Primary: ``omodul.run_and_fix`` / ``code_review``.  Fallback: deterministic
    no-op transaction (the plan is echoed, nothing is applied offline).
    """

    async def skills_guided_coding_workflow(
        plan: dict[str, Any], context: dict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if real_workflow is not None:
            try:
                from omodul.run_and_fix import Config as RfConfig
                from omodul.run_and_fix import InputData as RfInput

                out = await real_workflow(
                    RfConfig(max_iterations=3),
                    RfInput(task_description=plan.get("task", "")),
                    _run_output_dir(),
                )
                if isinstance(out, dict) and out.get("status") == "completed":
                    return {"status": "applied", "result": out.get("result", ""), "applied": True}
            except Exception:
                pass
        return {
            "status": "noop",
            "applied": False,
            "reason": "omodul.skills_guided_coding_workflow unavailable",
        }

    return _mark(
        skills_guided_coding_workflow,
        "omodul.skills_guided_coding_workflow",
        "skills_guided_coding_workflow",
    )


# ---------------------------------------------------------------------------
# Manifest builders (Step 1 deliverables)
# ---------------------------------------------------------------------------


def build_agentic_loop_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the ``agentic_loop`` engine manifest (E-1, SPEC §8).

    Injection wiring:
      * ``llm_caller`` ← ``oprim.llm_chat_call`` (llm_complete / llm_stream)
      * ``tools``      ← ``oskill.mcp_tool_route`` + sandbox execution node +
                         code-investigation node (tool router + execution nodes)
      * ``turn_handler`` ← omodul ``process_prompt`` (or identity bridge)
      * ``retrieval``  ← oskill ``code_search`` when available (optional)
      * ``layer4_ui``  ← None (CLI/TUI wires its own rich printer)

    Config bindings (observability):
      * ``cost_tracker``  ← ``obase.cost_tracker`` (CostTracker)
      * ``decision_logger`` ← ``obase.decision_logger`` (Trail)
    """
    cfg = dict(config or {})
    cost_tracker = resolve_element("obase.cost_tracker")
    decision_logger = resolve_element("obase.decision_logger")

    tools: list[Callable] = []
    tool_router = resolve_element("oskill.mcp_tool_route")
    tools.append(_make_tool_router(tool_router))

    sandbox_node = resolve_element("omodul.sandbox_execution_workflow")
    invest_node = resolve_element("omodul.code_investigation_workflow")
    tools.append(_make_sandbox_executor(sandbox_node))
    tools.append(_make_investigator(invest_node))

    real_llm = resolve_element("oprim.llm_chat_call")
    llm_caller = _make_llm_caller(real_llm)
    turn_handler = _make_turn_handler(_resolve_turn_handler())
    retrieval = _resolve_retrieval()

    inject: dict[str, Any] = {
        "llm_caller": llm_caller,
        "tools": tools,
        "turn_handler": turn_handler,
        "retrieval": retrieval,
        "layer4_ui": None,
    }
    cfg.setdefault("model", "claude-sonnet-4-6")
    cfg.setdefault("budget_usd", 10.0)
    cfg.setdefault("max_iterations", 50)
    cfg["cost_tracker"] = cost_tracker
    cfg["decision_logger"] = decision_logger

    return ServiceManifest(
        name="agentic_loop",
        skeleton="agentic_loop",
        inject=inject,
        trigger={"on_demand": True},
        config=cfg,
    )


def build_multi_agent_dag_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the multi-agent DAG orchestrator manifest (E-5, SPEC §8).

    Injection wiring:
      * ``subagent_runner`` ← ``omodul.multi_agent_worktree_workflow``
      * ``llm_caller``      ← ``oprim.llm_chat_call``
      * ``scheduler``       ← ``oskill.task_dag_split`` (Layer-4 bridge)

    Config bindings:
      * ``git_worktree``    ← ``obase.git_worktree`` (oprim worktree primitives)
      * ``cost_tracker`` / ``decision_logger`` for observability
    """
    cfg = dict(config or {})
    real_split = resolve_element("oskill.task_dag_split")
    real_runner = resolve_element("omodul.multi_agent_worktree_workflow")
    real_llm = resolve_element("oprim.llm_chat_call")
    cost_tracker = resolve_element("obase.cost_tracker")
    decision_logger = resolve_element("obase.decision_logger")
    worktree_add = resolve_element("obase.git_worktree")

    inject: dict[str, Any] = {
        "subagent_runner": _make_subagent_runner(real_runner),
        "llm_caller": _make_llm_caller(real_llm),
        "scheduler": _make_dag_split(real_split),
    }
    cfg.setdefault("max_parallel", 4)
    cfg.setdefault("max_depth", 3)
    cfg["cost_tracker"] = cost_tracker
    cfg["decision_logger"] = decision_logger
    cfg["git_worktree_add"] = worktree_add

    return ServiceManifest(
        name="multi_agent_dag",
        skeleton="subagent_orchestrator",
        inject=inject,
        trigger={"on_demand": True},
        config=cfg,
    )


def build_steerable_loop_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the steerable agentic-loop manifest (HITL, SPEC §8.4).

    Injection wiring:
      * ``llm_caller``           ← ``oprim.llm_chat_call``
      * ``tools``                ← ``oskill.mcp_tool_route`` + execution nodes
      * ``hitl_signal_bus``      ← ``obase.hitl_signal_bus`` (interrupt source)
      * ``hitl_instruction_steer`` ← ``oskill.hitl_instruction_steer`` (fusion)
      * ``hitl_approval_gate``   ← ``omodul.hitl_approval_workflow`` (gate)

    Config bindings:
      * ``hitl_approval_timeout`` — gate auto-approve timeout (default 30s)
      * ``hitl_mode`` — ``"auto"`` | ``"strict"`` (strict keeps gates open)
      * ``cost_tracker`` / ``decision_logger`` for observability
    """
    cfg = dict(config or {})
    real_llm = resolve_element("oprim.llm_chat_call")
    cost_tracker = resolve_element("obase.cost_tracker")
    decision_logger = resolve_element("obase.decision_logger")

    real_bus = resolve_element("obase.hitl_signal_bus")
    bus = _make_hitl_signal_bus(real_bus)
    real_steer = resolve_element("oskill.hitl_instruction_steer")
    real_gate = resolve_element("omodul.hitl_approval_workflow")

    tools: list[Callable] = [
        _make_tool_router(resolve_element("oskill.mcp_tool_route")),
        _make_sandbox_executor(resolve_element("omodul.sandbox_execution_workflow")),
        _make_investigator(resolve_element("omodul.code_investigation_workflow")),
    ]

    inject: dict[str, Any] = {
        "llm_caller": _make_llm_caller(real_llm),
        "tools": tools,
        "turn_handler": _make_turn_handler(_resolve_turn_handler()),
        "retrieval": _resolve_retrieval(),
        "layer4_ui": None,
        "hitl_signal_bus": bus,
        "hitl_instruction_steer": _make_hitl_instruction_steer(real_steer),
        "hitl_approval_gate": _make_hitl_approval_gate(
            real_gate, bus, default_timeout=float(cfg.get("hitl_approval_timeout", 30.0))
        ),
    }
    cfg.setdefault("model", "claude-sonnet-4-6")
    cfg.setdefault("budget_usd", 10.0)
    cfg.setdefault("hitl_approval_timeout", 30.0)
    cfg.setdefault("hitl_mode", "auto")
    cfg["cost_tracker"] = cost_tracker
    cfg["decision_logger"] = decision_logger
    cfg["hitl_signal_bus"] = bus  # engine steer() reaches the same instance
    cfg["engine_spec"] = "oservi.steerable_agentic_loop"  # assembled target

    return ServiceManifest(
        name="steerable_agentic_loop",
        skeleton="steerable_agentic_loop",
        inject=inject,
        trigger={"on_demand": True},
        config=cfg,
    )


def build_swarm_orchestrator_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the swarm orchestrator manifest (leader-worker, SPEC §8.5).

    Injection wiring:
      * ``leader_worker_dispatch`` ← ``oskill.leader_worker_dispatch``
      * ``subagent_runner``        ← ``omodul.swarm_collaborative_workflow``
      * ``llm_caller``             ← ``oprim.llm_chat_call``
      * ``scheduler``              ← ``oskill.task_dag_split`` (optional split)

    Config bindings:
      * ``max_workers`` — parallel worker cap (default 4)
      * ``swarm_mode`` — ``"isolated"`` (default; branch-isolated transactions)
      * ``cost_tracker`` / ``decision_logger`` for observability
    """
    cfg = dict(config or {})
    real_dispatch = resolve_element("oskill.leader_worker_dispatch")
    real_runner = resolve_element("omodul.swarm_collaborative_workflow")
    real_llm = resolve_element("oprim.llm_chat_call")
    real_split = resolve_element("oskill.task_dag_split")
    cost_tracker = resolve_element("obase.cost_tracker")
    decision_logger = resolve_element("obase.decision_logger")

    inject: dict[str, Any] = {
        "leader_worker_dispatch": _make_leader_worker_dispatch(real_dispatch),
        "subagent_runner": _make_subagent_runner(real_runner),
        "llm_caller": _make_llm_caller(real_llm),
        "scheduler": _make_dag_split(real_split),
    }
    cfg.setdefault("max_workers", 4)
    cfg.setdefault("max_depth", 3)
    cfg.setdefault("swarm_mode", "isolated")
    cfg["cost_tracker"] = cost_tracker
    cfg["decision_logger"] = decision_logger
    cfg["engine_spec"] = "oservi.swarm_orchestrator"  # assembled target

    return ServiceManifest(
        name="swarm_orchestrator",
        skeleton="swarm_orchestrator",
        inject=inject,
        trigger={"on_demand": True},
        config=cfg,
    )


def build_realtime_media_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the realtime media loop manifest (voice, SPEC §8.6).

    Injection wiring:
      * ``vad_pipeline``           ← ``obase.vad_pipeline``
      * ``stt_transcribe_stream``  ← ``oprim.stt_transcribe_stream``
      * ``tts_synthesize_stream``  ← ``oprim.tts_synthesize_stream``
      * ``llm_caller``             ← ``oprim.llm_chat_call``
      * ``turn_handler``           ← omodul ``process_prompt`` (optional)

    Config bindings:
      * ``media_loop_mode`` — ``"transcribe"`` | ``"converse"`` (default)
      * ``cost_tracker`` / ``decision_logger`` for observability
    """
    cfg = dict(config or {})
    real_vad = resolve_element("obase.vad_pipeline")
    real_stt = resolve_element("oprim.stt_transcribe_stream")
    real_tts = resolve_element("oprim.tts_synthesize_stream")
    real_llm = resolve_element("oprim.llm_chat_call")
    cost_tracker = resolve_element("obase.cost_tracker")
    decision_logger = resolve_element("obase.decision_logger")

    inject: dict[str, Any] = {
        "vad_pipeline": _make_vad_pipeline(real_vad),
        "stt_transcribe_stream": _make_stt_stream(real_stt),
        "tts_synthesize_stream": _make_tts_stream(real_tts),
        "llm_caller": _make_llm_caller(real_llm),
        "turn_handler": _make_turn_handler(_resolve_turn_handler()),
    }
    cfg.setdefault("model", "claude-sonnet-4-6")
    cfg.setdefault("media_loop_mode", "converse")
    cfg["cost_tracker"] = cost_tracker
    cfg["decision_logger"] = decision_logger
    cfg["engine_spec"] = "oservi.realtime_media_loop"  # assembled target

    return ServiceManifest(
        name="realtime_media_loop",
        skeleton="realtime_media_loop",
        inject=inject,
        trigger={"on_demand": True},
        config=cfg,
    )


def build_neuro_symbolic_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the neuro-symbolic verification manifest.

    Injection wiring:
      * ``neuro_verifier`` ← ``oskill.neuro_symbolic_verify``
      * ``fol_translator`` ← ``oprim.fol_translate``
      * ``smt_solver``     ← ``obase.smt_solver_adapter``
      * ``llm_caller``     ← ``oprim.llm_chat_call`` (optional explainer)

    Config bindings:
      * ``engine_spec`` — ``oskill.neuro_symbolic_verify`` (primary element)
      * ``cost_tracker`` / ``decision_logger`` for observability
    """
    cfg = dict(config or {})
    real_verify = resolve_element("oskill.neuro_symbolic_verify")
    real_fol = resolve_element("oprim.fol_translate")
    real_smt = resolve_element("obase.smt_solver_adapter")
    real_llm = resolve_element("oprim.llm_chat_call")
    cost_tracker = resolve_element("obase.cost_tracker")
    decision_logger = resolve_element("obase.decision_logger")

    inject: dict[str, Any] = {
        "fol_translator": _make_fol_translator(real_fol),
        "smt_solver": _make_smt_solver_adapter(real_smt),
        "neuro_verifier": _make_neuro_symbolic_verifier(real_verify),
        "llm_caller": _make_llm_caller(real_llm),
    }
    cfg.setdefault("model", "claude-sonnet-4-6")
    cfg["cost_tracker"] = cost_tracker
    cfg["decision_logger"] = decision_logger
    cfg["engine_spec"] = "oskill.neuro_symbolic_verify"  # primary assembled element

    return ServiceManifest(
        name="neuro_symbolic",
        skeleton="neuro_symbolic",
        inject=inject,
        trigger={"on_demand": True},
        config=cfg,
    )


def build_root_cause_analysis_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the root-cause analysis manifest.

    Injection wiring:
      * ``root_cause_analyzer``  ← ``omodul.root_cause_analysis_workflow``
      * ``causal_graph``         ← ``oprim.causal_graph_build``
      * ``counterfactual_reasoner`` ← ``oskill.counterfactual_reasoning``
      * ``llm_caller``           ← ``oprim.llm_chat_call`` (optional explainer)

    Config bindings:
      * ``engine_spec`` — ``omodul.root_cause_analysis_workflow``
      * ``cost_tracker`` / ``decision_logger`` for observability
    """
    cfg = dict(config or {})
    real_analyzer = resolve_element("omodul.root_cause_analysis_workflow")
    real_causal = resolve_element("oprim.causal_graph_build")
    real_counter = resolve_element("oskill.counterfactual_reasoning")
    real_llm = resolve_element("oprim.llm_chat_call")
    cost_tracker = resolve_element("obase.cost_tracker")
    decision_logger = resolve_element("obase.decision_logger")

    inject: dict[str, Any] = {
        "causal_graph": _make_causal_graph_builder(real_causal),
        "root_cause_analyzer": _make_root_cause_analyzer(real_analyzer),
        "counterfactual_reasoner": _make_counterfactual_reasoner(real_counter),
        "llm_caller": _make_llm_caller(real_llm),
    }
    cfg.setdefault("model", "claude-sonnet-4-6")
    cfg["cost_tracker"] = cost_tracker
    cfg["decision_logger"] = decision_logger
    cfg["engine_spec"] = "omodul.root_cause_analysis_workflow"  # primary assembled element

    return ServiceManifest(
        name="root_cause_analysis",
        skeleton="root_cause_analysis",
        inject=inject,
        trigger={"on_demand": True},
        config=cfg,
    )


def build_long_horizon_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the long-horizon agentic-loop manifest (checkpoint + compression).

    Injection wiring:
      * ``llm_caller``       ← ``oprim.llm_chat_call``
      * ``tools``            ← tool router + execution nodes
      * ``checkpoint_store`` ← ``obase.checkpoint_store`` (state snapshots)
      * ``context_compressor`` ← ``oskill.long_context_compress`` (incremental)

    Config bindings:
      * ``engine_spec`` — ``oservi.long_horizon_agentic_loop``
      * ``max_tail`` — compressor window tail size (default 8)
      * ``cost_tracker`` / ``decision_logger`` for observability
    """
    cfg = dict(config or {})
    real_llm = resolve_element("oprim.llm_chat_call")
    cost_tracker = resolve_element("obase.cost_tracker")
    decision_logger = resolve_element("obase.decision_logger")
    checkpoint_store = _make_checkpoint_store(resolve_element("obase.checkpoint_store"))

    tools: list[Callable] = [
        _make_tool_router(resolve_element("oskill.mcp_tool_route")),
        _make_sandbox_executor(resolve_element("omodul.sandbox_execution_workflow")),
        _make_investigator(resolve_element("omodul.code_investigation_workflow")),
    ]
    inject: dict[str, Any] = {
        "llm_caller": _make_llm_caller(real_llm),
        "tools": tools,
        "turn_handler": _make_turn_handler(_resolve_turn_handler()),
        "retrieval": _resolve_retrieval(),
        "layer4_ui": None,
        "checkpoint_store": checkpoint_store,
        "context_compressor": _make_context_compressor(
            resolve_element("oskill.long_context_compress")
        ),
    }
    cfg.setdefault("model", "claude-sonnet-4-6")
    cfg.setdefault("budget_usd", 10.0)
    cfg.setdefault("max_tail", 8)
    cfg["cost_tracker"] = cost_tracker
    cfg["decision_logger"] = decision_logger
    cfg["checkpoint_store"] = checkpoint_store
    cfg["engine_spec"] = "oservi.long_horizon_agentic_loop"  # assembled target

    return ServiceManifest(
        name="long_horizon_agentic_loop",
        skeleton="long_horizon_agentic_loop",
        inject=inject,
        trigger={"on_demand": True},
        config=cfg,
    )


def build_graph_skills_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the graph-skills agentic-loop manifest (AST graph + skills).

    Injection wiring:
      * ``llm_caller``            ← ``oprim.llm_chat_call``
      * ``skills_registry``       ← ``obase.skills_registry`` (rule matching)
      * ``code_graph_parser``     ← ``oprim.code_graph_parse`` (AST graph)
      * ``skills_coding_workflow`` ← ``omodul.skills_guided_coding_workflow``
      * ``graph_impact_analysis`` ← ``oskill.graph_impact_analysis`` (optional)

    Config bindings:
      * ``engine_spec`` — ``oservi.graph_skills_agentic_loop``
      * ``cost_tracker`` / ``decision_logger`` for observability
    """
    cfg = dict(config or {})
    real_llm = resolve_element("oprim.llm_chat_call")
    cost_tracker = resolve_element("obase.cost_tracker")
    decision_logger = resolve_element("obase.decision_logger")
    skills_registry = _make_skills_registry(resolve_element("obase.skills_registry"))
    # seed the registry with deterministic trigger rules
    for skill, rule in (("code-review", "review"), ("refactor", "refactor"), ("test", "test")):
        skills_registry.register(skill, rule)

    tools: list[Callable] = [
        _make_tool_router(resolve_element("oskill.mcp_tool_route")),
        _make_sandbox_executor(resolve_element("omodul.sandbox_execution_workflow")),
        _make_investigator(resolve_element("omodul.code_investigation_workflow")),
    ]
    inject: dict[str, Any] = {
        "llm_caller": _make_llm_caller(real_llm),
        "tools": tools,
        "turn_handler": _make_turn_handler(_resolve_turn_handler()),
        "retrieval": _resolve_retrieval(),
        "layer4_ui": None,
        "skills_registry": skills_registry,
        "code_graph_parser": _make_code_graph_parser(resolve_element("oprim.code_graph_parse")),
        "skills_coding_workflow": _make_skills_coding_workflow(
            resolve_element("omodul.skills_guided_coding_workflow")
        ),
        "graph_impact_analysis": _make_graph_impact_analysis(
            resolve_element("oskill.graph_impact_analysis")
        ),
    }
    cfg.setdefault("model", "claude-sonnet-4-6")
    cfg.setdefault("budget_usd", 10.0)
    cfg["cost_tracker"] = cost_tracker
    cfg["decision_logger"] = decision_logger
    cfg["skills_registry"] = skills_registry
    cfg["engine_spec"] = "oservi.graph_skills_agentic_loop"  # assembled target

    return ServiceManifest(
        name="graph_skills_agentic_loop",
        skeleton="graph_skills_agentic_loop",
        inject=inject,
        trigger={"on_demand": True},
        config=cfg,
    )


def build_agent_creation_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the agent-creation manifest (AutoAgent zero-code core)."""
    cfg = dict(config or {})
    real_form = resolve_element("oskill.agent_form_synthesize")
    real_codegen = resolve_element("oprim.agent_codegen")
    real_registry = resolve_element("obase.agent_registry")
    real_llm = resolve_element("oprim.llm_chat_call")
    inject: dict[str, Any] = {
        "agent_form_synthesize": _make_form_synthesizer(real_form),
        "agent_codegen": _make_codegen_adapter(real_codegen),
        "agent_registry": real_registry if real_registry is not None else _registry_ref(),
        "llm_caller": _make_llm_caller(real_llm),
    }
    cfg.setdefault("model", "claude-sonnet-4-6")
    cfg["cost_tracker"] = resolve_element("obase.cost_tracker")
    cfg["decision_logger"] = resolve_element("obase.decision_logger")
    cfg["engine_spec"] = "omodul.agent_creation_workflow"
    return ServiceManifest(name="agent_creation", skeleton="agent_creation_workflow", inject=inject, trigger={"on_demand": True}, config=cfg)


def build_event_workflow_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the event-driven workflow manifest."""
    cfg = dict(config or {})
    real_engine = resolve_element("oservi.event_workflow_engine")
    real_llm = resolve_element("oprim.llm_chat_call")
    inject: dict[str, Any] = {
        "event_engine": _make_event_engine_adapter(real_engine),
        "llm_caller": _make_llm_caller(real_llm),
    }
    cfg["engine_spec"] = "oservi.event_workflow_engine"
    return ServiceManifest(name="event_workflow", skeleton="event_workflow", inject=inject, trigger={"on_demand": True}, config=cfg)


def _make_form_synthesizer(real: Any) -> Callable:
    async def _synth(request, context=None, **kw):
        from oskill.agent_form_synthesize import agent_form_synthesize
        return agent_form_synthesize(request, llm_caller=None, context=context or {})
    return _mark(_synth, "oskill.agent_form_synthesize", "agent_form_synthesize")


def _make_codegen_adapter(real: Any) -> Callable:
    async def _gen(spec, **kw):
        from oprim.agent_codegen import agent_codegen
        return agent_codegen(spec)
    return _mark(_gen, "oprim.agent_codegen", "agent_codegen")


def _make_event_engine_adapter(real: Any) -> Any:
    try:
        return real() if real is not None else None
    except Exception:
        return None


def _registry_ref() -> Any:
    try:
        from obase.agent_registry import registry
        return registry
    except Exception:
        return None


def manifest_summary(manifest: ServiceManifest) -> dict[str, Any]:
    """Human-readable summary of a manifest (for CLI dry-run / SSE meta)."""
    return {
        "name": manifest.name,
        "skeleton": manifest.skeleton,
        "trigger": manifest.trigger,
        "inject": {k: _callable_label(v) for k, v in manifest.inject.items()},
        "config_keys": sorted(manifest.config.keys()),
    }


def _callable_label(ref: Any) -> str:
    if ref is None:
        return "none"
    if isinstance(ref, list):
        return ", ".join(_callable_label(r) for r in ref)
    return (
        getattr(ref, "__module__", type(ref).__name__)
        + "."
        + getattr(ref, "__name__", type(ref).__name__)
    )


# ---------------------------------------------------------------------------
# Assembly — produce a runnable engine from a manifest
# ---------------------------------------------------------------------------


class AssembledEngine:
    """Layer-4 assembled engine facade (oservi-compatible async surface).

    When the real ``oservi`` skeleton is importable we still go through
    ``ServiceManifest`` + DI; this facade guarantees a uniform async API
    (``invoke`` / ``run_turn`` / ``orchestrate`` / ``health``) regardless of
    whether the heavy 3O optional deps are installed.
    """

    def __init__(self, manifest: ServiceManifest) -> None:
        validate_manifest(manifest)
        self.manifest = manifest
        self.name = manifest.name
        self.skeleton = manifest.skeleton
        self._running = False
        self._tick_count = 0
        self._total_cost_usd = 0.0
        self.session_id = str(uuid.uuid4())[:8]

    # -- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def health(self) -> dict[str, Any]:
        return {
            "status": "healthy" if self._running else "stopped",
            "name": self.name,
            "skeleton": self.skeleton,
            "tick_count": self._tick_count,
            "total_cost_usd": round(self._total_cost_usd, 6),
            "session_id": self.session_id,
        }

    # -- agentic loop surface ----------------------------------------------
    async def run_turn(
        self,
        messages: list[dict],
        tools: list[Callable] | None = None,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """Execute a single conversation turn through the injected elements."""
        context = context or {}
        self._tick_count += 1
        llm_caller = self.manifest.inject.get("llm_caller")
        turn_handler = self.manifest.inject.get("turn_handler")

        # HITL: merge any queued steer instructions into the turn context
        bus = self.manifest.config.get("hitl_signal_bus") or self.manifest.inject.get(
            "hitl_signal_bus"
        )
        steer_instructions: list[str] = []
        if bus is not None and hasattr(bus, "pop_steer_instructions"):
            steer_instructions = bus.pop_steer_instructions()
        if steer_instructions:
            steer_fn = self.manifest.inject.get("hitl_instruction_steer")
            context = dict(context)
            context["steer_instructions"] = steer_instructions
            context["goal"] = (
                f"{context.get('goal', '')}\n[steer] {'; '.join(steer_instructions)}"
                if context.get("goal")
                else "; ".join(steer_instructions)
            )
            if steer_fn is not None:
                try:
                    out = steer_fn(context, instruction="; ".join(steer_instructions))
                    if hasattr(out, "__await__"):
                        out = await out
                    if isinstance(out, dict) and out.get("goal"):
                        context["goal"] = out["goal"]
                except Exception:
                    pass

        msgs = list(messages)
        if turn_handler is not None:
            try:
                prepared = await turn_handler(messages=msgs, context=context)
                if isinstance(prepared, dict) and prepared.get("messages"):
                    msgs = prepared["messages"]
            except Exception:
                pass
        if llm_caller is None:
            return {"status": "failed", "content": "no llm_caller injected", "cost_usd": 0.0}
        try:
            out = llm_caller(messages=msgs, tools=tools, config=self.manifest.config)
            if hasattr(out, "__await__"):
                out = await out
            cost = float(out.get("cost_usd", 0.0)) if isinstance(out, dict) else 0.0
            self._total_cost_usd += cost
            return {"status": "completed", "turn_result": out, "cost_usd": cost}
        except Exception as exc:  # pragma: no cover - defensive
            return {"status": "failed", "error": str(exc), "cost_usd": 0.0}

    async def invoke(self, task: dict[str, Any]) -> dict[str, Any]:
        """Single request/response invocation (agentic loop entry point)."""
        goal = task.get("goal", "")
        messages = [{"role": "user", "content": goal}]
        return await self.run_turn(messages=messages, context=task)

    # -- HITL steer surface (steerable_agentic_loop) -------------------------
    async def steer(
        self,
        action: str,
        instruction: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Interrupt a running task: ``approve`` / ``reject`` / ``steer``.

        With an ``approval_id`` the signal resolves the pending approval gate;
        otherwise the steer instruction is queued for the next invocation
        (merged by ``hitl_instruction_steer``).
        """
        bus = self.manifest.config.get("hitl_signal_bus") or self.manifest.inject.get(
            "hitl_signal_bus"
        )
        if bus is None:
            return {"status": "no_hitl_bus", "action": action}
        if approval_id:
            return await bus.decide(approval_id, action, instruction)
        if action == "steer" and instruction and hasattr(bus, "queue_steer"):
            bus.queue_steer(instruction)
        return {
            "status": "queued",
            "action": action,
            "instruction": instruction,
            "approval_id": approval_id,
        }

    def pending_approvals(self) -> list[dict[str, Any]]:
        """List open approval gates (for the gateway / frontend)."""
        bus = self.manifest.config.get("hitl_signal_bus") or self.manifest.inject.get(
            "hitl_signal_bus"
        )
        if bus is None:
            return []
        return bus.pending() if hasattr(bus, "pending") else []

    # -- swarm surface (swarm_orchestrator) ----------------------------------
    async def dispatch(self, goal: str, context: dict | None = None) -> dict[str, Any]:
        """Leader-worker dispatch entry: split, then orchestrate the workers."""
        dispatch_fn = self.manifest.inject.get("leader_worker_dispatch")
        if dispatch_fn is None:
            return await self.orchestrate(
                [{"id": "w1", "description": goal, "depends_on": []}], parallel=True
            )
        out = dispatch_fn(goal, context=context or {})
        if hasattr(out, "__await__"):
            out = await out
        tasks = out if isinstance(out, list) else [{"id": "w1", "description": goal}]
        return await self.orchestrate(tasks, parallel=True)

    # -- realtime media surface (realtime_media_loop) ------------------------
    async def run_media_stream(
        self,
        frames: list[dict[str, Any]],
        context: dict | None = None,
    ) -> dict[str, Any]:
        """One media utterance through the realtime loop.

        vad → stt → (llm converse) → tts.  Deterministic offline: VAD passes
        frames through, STT/TTS mark ``unavailable``, LLM uses the graceful
        stub — the pipeline shape is always exercised.
        """
        ctx = context or {}
        vad = self.manifest.inject.get("vad_pipeline")
        stt = self.manifest.inject.get("stt_transcribe_stream")
        tts = self.manifest.inject.get("tts_synthesize_stream")
        llm_caller = self.manifest.inject.get("llm_caller")
        if vad is None or stt is None or tts is None:
            return {"status": "failed", "error": "realtime_media_loop injections missing"}

        utterances = await vad(frames, context=ctx)
        turns: list[dict[str, Any]] = []
        for utt in utterances or []:
            transcript = await stt(utt, context=ctx)
            text = transcript.get("text", "") if isinstance(transcript, dict) else str(transcript)
            reply = ""
            if (
                text
                and llm_caller is not None
                and ctx.get("media_loop_mode", "converse") != "transcribe"
            ):
                out = llm_caller(
                    messages=[{"role": "user", "content": text}],
                    tools=None,
                    config=self.manifest.config,
                )
                if hasattr(out, "__await__"):
                    out = await out
                reply = (out.get("content") or "") if isinstance(out, dict) else str(out)
            audio = await tts(reply or text, context=ctx)
            turns.append(
                {
                    "utterance_id": utt.get("utterance_id", "u1"),
                    "vad": utt.get("vad", True),
                    "transcript": text,
                    "stt_status": transcript.get("status")
                    if isinstance(transcript, dict)
                    else "ok",
                    "reply": reply,
                    "tts_status": audio.get("status") if isinstance(audio, dict) else "ok",
                }
            )
        self._tick_count += 1
        return {"status": "completed", "turns": turns}

    # -- neuro-symbolic surface (neuro_symbolic) -----------------------------
    async def run_verify(self, statement: str, context: dict | None = None) -> dict[str, Any]:
        """Verify a natural-language proposition through the neuro-symbolic
        pipeline: fol_translate → smt_solver → neuro_verifier."""
        ctx = context or {}
        fol = self.manifest.inject.get("fol_translator")
        smt = self.manifest.inject.get("smt_solver")
        verifier = self.manifest.inject.get("neuro_verifier")
        if fol is None or smt is None or verifier is None:
            return {"status": "failed", "error": "neuro_symbolic injections missing"}

        fol_result = await fol(statement, context=ctx)
        smt_result = await smt(fol_result.get("formula"), context=ctx)
        verdict = await verifier(statement, fol_result, smt_result, context=ctx)
        self._tick_count += 1
        return {
            "status": "completed",
            "verdict": verdict.get("verdict", "inconclusive"),
            "confidence": float(verdict.get("confidence", 0.0)),
            "stages": {
                "fol_translate": fol_result,
                "smt_solver": smt_result,
                "neuro_symbolic_verify": verdict,
            },
        }

    # -- root-cause surface (root_cause_analysis) ----------------------------
    async def run_diagnose(
        self, events: list[dict[str, Any]], context: dict | None = None
    ) -> dict[str, Any]:
        """Root-cause attribution over a decision trail: causal_graph_build →
        root_cause_analysis_workflow → counterfactual_reasoning."""
        ctx = context or {}
        causal = self.manifest.inject.get("causal_graph")
        analyzer = self.manifest.inject.get("root_cause_analyzer")
        counter = self.manifest.inject.get("counterfactual_reasoner")
        if causal is None or analyzer is None or counter is None:
            return {"status": "failed", "error": "root_cause_analysis injections missing"}

        graph_result = await causal(events, context=ctx)
        analysis = await analyzer(events, graph_result.get("graph"), context=ctx)
        causes = analysis.get("root_causes", [])
        counterfactuals = []
        for cause in causes[: int(ctx.get("max_counterfactuals", 3))]:
            cf = await counter(cause, context=ctx)
            counterfactuals.append(cf)
        self._tick_count += 1
        return {
            "status": "completed",
            "graph": graph_result.get("graph", {}),
            "root_causes": causes,
            "counterfactuals": counterfactuals,
            "confidence": round(sum(float(c.get("score", 0.0)) for c in causes), 3),
        }


    # -- agent creation surface (agent_creation_workflow) ---------------------
    async def run_agent_create(self, task: dict[str, Any], context: dict | None = None) -> dict[str, Any]:
        """Natural language → agent form → codegen → file write → register."""
        ctx = context or {}
        form_synth = self.manifest.inject.get("agent_form_synthesize")
        codegen_fn = self.manifest.inject.get("agent_codegen")
        if form_synth is None or codegen_fn is None:
            return {"status": "failed", "error": "agent creation injections missing"}
        goal = task.get("goal", "")
        form = await form_synth(goal, context=ctx)
        source = codegen_fn(form)
        if hasattr(source, "__await__"):
            source = await source
        # write + register via the omodul workflow (fallback: direct codegen)
        try:
            from pathlib import Path

            from omodul.agent_creation_workflow import agent_creation_workflow
            out_dir = Path(ctx.get("output_dir", _run_output_dir()))
            result = agent_creation_workflow({"model": ctx.get("model", "claude-sonnet-4-6")}, {
                "form": form if isinstance(form, dict) else {"name": task.get("goal")[:30]},
                "goal": goal,
            }, out_dir)
            return result
        except Exception:
            return {"status": "completed", "form": form, "source_preview": str(source)[:500], "note": "codegen-only (omodul not available)"}

    # -- event workflow surface (event_workflow) -------------------------------
    async def run_workflow_drive(self, system_input: dict[str, Any], context: dict | None = None) -> dict[str, Any]:
        """Drive an event workflow from system input."""
        engine = self.manifest.inject.get("event_engine")
        if engine is None:
            return {"status": "failed", "error": "event_engine injection missing"}
        result = engine.drive(system_input)
        if hasattr(result, "__await__"):
            result = await result
        return result if isinstance(result, dict) else {"status": "completed", "result": result}

    # -- long-horizon surface (long_horizon_agentic_loop) --------------------
    async def run_long_horizon(
        self, task: dict[str, Any], context: dict | None = None
    ) -> dict[str, Any]:
        """Long-horizon task with checkpoint snapshot restore + incremental
        context compression.  Re-invoking with the same ``session_id`` resumes
        from the stored checkpoint (turns counter + compressed summary)."""
        ctx = context or {}
        store = self.manifest.config.get("checkpoint_store") or self.manifest.inject.get(
            "checkpoint_store"
        )
        compressor = self.manifest.inject.get("context_compressor")
        session_id = task.get("session_id") or self.session_id
        goal = task.get("goal", "")
        key = f"session:{session_id}"

        # 1. resume from checkpoint when present
        checkpoint = store.load(key) if store is not None else None
        resume = bool(checkpoint) if ctx.get("resume", True) else False
        summary = (checkpoint or {}).get("summary", "") if resume else ""
        turns_done = int((checkpoint or {}).get("turns_done", 0)) if resume else 0

        # 2. build + compress the working context
        messages: list[dict[str, Any]] = []
        if summary:
            messages.append({"role": "system", "content": f"[checkpoint] {summary}"})
        messages.append({"role": "user", "content": goal})
        compression = {"status": "unchanged", "messages": messages}
        if compressor is not None:
            compression = await compressor(messages, context=ctx)
        work_messages = compression.get("messages") or messages

        # 3. one agentic turn on the (compressed) context
        turn = await self.run_turn(messages=work_messages, context=task)
        self._total_cost_usd += float(turn.get("cost_usd", 0.0) or 0.0)
        turns_done += 1

        # 4. snapshot state for resume
        snapshot = {
            "session_id": session_id,
            "goal": goal,
            "turns_done": turns_done,
            "summary": compression.get("summary", "") or summary,
            "last_status": turn.get("status", "completed"),
        }
        if store is not None:
            store.save(key, snapshot)
        return {
            "status": "completed",
            "session_id": session_id,
            "resumed": resume,
            "checkpoint": snapshot,
            "compression": {
                "status": compression.get("status"),
                "kept_messages": len(work_messages),
            },
            "turn_result": turn.get("turn_result") or {},
            "cost_usd": float(turn.get("cost_usd", 0.0) or 0.0),
        }

    # -- graph-skills surface (graph_skills_agentic_loop) --------------------
    async def run_graph_investigate(
        self,
        files: list[dict[str, Any]],
        seed_nodes: list[str] | None = None,
        context: dict | None = None,
    ) -> dict[str, Any]:
        """AST code-graph dependency resolution + impact analysis over files."""
        ctx = context or {}
        parser = self.manifest.inject.get("code_graph_parser")
        impact = self.manifest.inject.get("graph_impact_analysis")
        registry = self.manifest.config.get("skills_registry") or self.manifest.inject.get(
            "skills_registry"
        )
        workflow = self.manifest.inject.get("skills_coding_workflow")
        if parser is None or workflow is None:
            return {"status": "failed", "error": "graph_skills injections missing"}

        parsed = await parser(files, context=ctx)
        graph = parsed.get("graph", {})
        seeds = seed_nodes or ctx.get("seed_nodes") or []
        impacted: list[str] = []
        if impact is not None and seeds:
            analysis = await impact(graph, seeds, context=ctx)
            impacted = analysis.get("impacted", [])
        task_text = ctx.get("task", "")
        skills = registry.match(task_text) if registry is not None else []
        plan = {"task": task_text, "impacted": impacted, "skills": skills}
        coding = await workflow(plan, context=ctx)
        self._tick_count += 1
        return {
            "status": "completed",
            "graph": graph,
            "impacted": impacted,
            "skills_matched": skills,
            "coding_workflow": coding,
        }

    # -- DAG orchestrator surface ------------------------------------------
    async def orchestrate(
        self,
        tasks: list[dict],
        *,
        parallel: bool = False,
    ) -> dict[str, Any]:
        """Split + run sub-tasks concurrently in isolated worktrees."""
        scheduler = self.manifest.inject.get("scheduler")
        subagent_runner = self.manifest.inject.get("subagent_runner")
        if scheduler is None or subagent_runner is None:
            return {"status": "failed", "error": "DAG injections missing"}

        # 1. task split (task_dag_split) — split EVERY input task, flatten
        split_tasks: list[dict] = []
        for input_task in tasks:
            desc = input_task.get("description", "")
            try:
                out = scheduler(desc)
                if hasattr(out, "__await__"):
                    out = await out
                if isinstance(out, list) and out:
                    split_tasks.extend(out)
                else:
                    split_tasks.append(input_task)
            except Exception:
                split_tasks.append(input_task)

        # 2. per sub-task: isolate in a git worktree then run
        worktree_add = self.manifest.config.get("git_worktree_add")
        repo = Path(self.manifest.config.get("repo", "."))

        async def _run_one(t: dict) -> dict:
            wt_path = None
            if worktree_add is not None:
                try:
                    wt_path = worktree_add(f"dag-{t.get('id', 't')}", repo=repo)
                except Exception:
                    wt_path = None
            try:
                result = subagent_runner(task=t, config=self.manifest.config)
                if hasattr(result, "__await__"):
                    result = await result
                return result if isinstance(result, dict) else {"result": str(result)}
            finally:
                if wt_path is not None:
                    try:
                        from oprim import git_worktree_remove  # type: ignore[import-not-found]

                        git_worktree_remove(wt_path, repo=repo, force=True)
                    except Exception:
                        pass

        if parallel:
            import asyncio

            max_par = int(self.manifest.config.get("max_parallel", 4))
            sem = asyncio.Semaphore(max_par)

            async def _sem_run(t: dict) -> dict:
                async with sem:
                    return await _run_one(t)

            results = await asyncio.gather(*[_sem_run(t) for t in split_tasks])
        else:
            results = []
            for t in split_tasks:
                results.append(await _run_one(t))
        return {"status": "completed", "results": results}


def assemble(manifest: ServiceManifest) -> AssembledEngine:
    """Assemble a manifest into a runnable engine (Layer-4 facade)."""
    return AssembledEngine(manifest)


def assemble_agentic_loop(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the agentic loop engine from ``build_agentic_loop_manifest``."""
    return assemble(build_agentic_loop_manifest(config))


def assemble_dag_orchestrator(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the multi-agent DAG orchestrator engine."""
    return assemble(build_multi_agent_dag_manifest(config))


def assemble_steerable_loop(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the steerable agentic loop (HITL) engine."""
    return assemble(build_steerable_loop_manifest(config))


def assemble_swarm_orchestrator(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the swarm orchestrator (leader-worker) engine."""
    return assemble(build_swarm_orchestrator_manifest(config))


def assemble_realtime_media(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the realtime media loop (voice) engine."""
    return assemble(build_realtime_media_manifest(config))


def assemble_neuro_symbolic(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the neuro-symbolic verification engine."""
    return assemble(build_neuro_symbolic_manifest(config))


def assemble_root_cause_analysis(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the root-cause analysis engine."""
    return assemble(build_root_cause_analysis_manifest(config))


def assemble_long_horizon(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the long-horizon agentic-loop engine."""
    return assemble(build_long_horizon_manifest(config))


def assemble_agent_creation(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the agent-creation engine."""
    return assemble(build_agent_creation_manifest(config))


def assemble_event_workflow(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the event-workflow engine."""
    return assemble(build_event_workflow_manifest(config))


def assemble_graph_skills(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the graph-skills agentic-loop engine."""
    return assemble(build_graph_skills_manifest(config))


# ---------------------------------------------------------------------------
# Running-engine registry (HITL steer / swarm control from the gateway)
# ---------------------------------------------------------------------------

_running_engines: dict[str, AssembledEngine] = {}


def register_running_engine(session_id: str, engine: AssembledEngine) -> None:
    """Register a live engine so gateway routes can steer / inspect it."""
    _running_engines[session_id] = engine


def get_running_engine(session_id: str) -> AssembledEngine | None:
    """Look up a live engine by session id (None when unknown / finished)."""
    return _running_engines.get(session_id)


def unregister_running_engine(session_id: str) -> None:
    """Drop a finished engine from the registry."""
    _running_engines.pop(session_id, None)


# ---------------------------------------------------------------------------
# Decision trail persistence (obase.decision_logger / Trail bridge)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# G13 — Voice & Vision element aliases
# ---------------------------------------------------------------------------

# These map the new voice/vision pipeline elements to their mount points.
# When the veya oprim/oskill/omodul packages are installed (local development),
# these resolve to real 3O symbols.  Otherwise they gracefully degrade to
# Layer-4 fallback stubs.

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

ELEMENT_ALIASES.update(_G13_ALIASES)


# ---------------------------------------------------------------------------
# G14 — Browser & Spawn element aliases
# ---------------------------------------------------------------------------

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

ELEMENT_ALIASES.update(_G14_ALIASES)


# ---------------------------------------------------------------------------
# G13 — Voice Agent manifest builder
# ---------------------------------------------------------------------------


def build_voice_agent_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the voice agent engine manifest (G13 voice).

    Injection wiring:
      * ``stt_skill``      ← ``oskill.speech_to_text``
      * ``tts_skill``      ← ``oskill.text_to_speech``
      * ``llm_caller``     ← ``oprim.llm_chat_call``
      * ``vad_oprim``      ← ``oprim.vad_frame``
      * ``turn_detector``  ← ``oskill.turn_detection`` (optional)
      * ``audio_io``       ← ``oskill.audio_pipeline`` (optional)
      * ``tools``          ← tool router + execution nodes (layer4)

    Config bindings:
      * ``sample_rate`` — audio sample rate (default 16000)
      * ``language`` — STT language code (default "en")
      * ``stt_provider`` / ``tts_provider`` / ``tts_voice``
      * ``cost_tracker`` / ``decision_logger`` for observability
    """
    cfg = dict(config or {})
    real_stt = resolve_element("oskill.speech_to_text")
    real_tts = resolve_element("oskill.text_to_speech")
    real_llm = resolve_element("oprim.llm_chat_call")
    real_vad = resolve_element("oprim.vad_frame")
    real_turn = resolve_element("oskill.turn_detection")
    real_audio = resolve_element("oskill.audio_pipeline")
    cost_tracker = resolve_element("obase.cost_tracker")
    decision_logger = resolve_element("obase.decision_logger")

    tools: list[Callable] = [
        _make_tool_router(resolve_element("oskill.mcp_tool_route")),
        _make_sandbox_executor(resolve_element("omodul.sandbox_execution_workflow")),
    ]

    inject: dict[str, Any] = {
        "stt_skill": _make_voice_skill(real_stt, "stt"),
        "tts_skill": _make_voice_skill(real_tts, "tts"),
        "llm_caller": _make_llm_caller(real_llm),
        "vad_oprim": _make_vad_adapter(real_vad),
        "turn_detector": real_turn,
        "audio_io": real_audio,
        "tools": tools,
    }
    cfg.setdefault("sample_rate", 16000)
    cfg.setdefault("language", "en")
    cfg.setdefault("stt_provider", "openai")
    cfg.setdefault("tts_provider", "openai")
    cfg["cost_tracker"] = cost_tracker
    cfg["decision_logger"] = decision_logger

    return ServiceManifest(
        name="voice_agent",
        skeleton="voice_agent",
        inject=inject,
        trigger={"on_demand": True},
        config=cfg,
    )


def build_vision_agent_manifest(config: dict[str, Any] | None = None) -> ServiceManifest:
    """Build the vision agent engine manifest (G13 vision).

    Injection wiring:
      * ``vision_skill``  ← ``oskill.analyze_image``
      * ``llm_caller``    ← ``oprim.llm_chat_call``
      * ``image_oprim``   ← ``oprim.encode_image_base64``
      * ``video_sampler`` ← oprim video frame sampler
      * ``tools``         ← tool router (layer4)
    """
    cfg = dict(config or {})
    real_vision = resolve_element("oskill.analyze_image")
    real_llm = resolve_element("oprim.llm_chat_call")
    real_image = resolve_element("oprim.encode_image_base64")
    cost_tracker = resolve_element("obase.cost_tracker")
    decision_logger = resolve_element("obase.decision_logger")

    tools: list[Callable] = [
        _make_tool_router(resolve_element("oskill.mcp_tool_route")),
    ]

    inject: dict[str, Any] = {
        "vision_skill": _make_vision_adapter(real_vision),
        "llm_caller": _make_llm_caller(real_llm),
        "image_oprim": _make_image_adapter(real_image),
        "video_sampler": None,
        "tools": tools,
    }
    cfg.setdefault("provider", "openai")
    cfg["cost_tracker"] = cost_tracker
    cfg["decision_logger"] = decision_logger

    return ServiceManifest(
        name="vision_agent",
        skeleton="vision_agent",
        inject=inject,
        trigger={"on_demand": True},
        config=cfg,
    )


# ── G13 adapter factories ────────────────────────────────────────────────


def _make_voice_skill(real_skill: Callable | None, skill_type: str) -> Callable:
    """Adapter for STT/TTS skills to match engine calling convention."""

    async def voice_skill(*args: Any, **kwargs: Any) -> Any:
        if real_skill is not None:
            try:
                out = real_skill(*args, **kwargs)
                if hasattr(out, "__await__"):
                    return await out
                return out
            except Exception:
                pass
        if skill_type == "stt":
            from veya.oprim.types import TranscriptionResult
            return TranscriptionResult(text="[STT unavailable]", metadata={"status": "unavailable"})
        elif skill_type == "tts":
            return b""
        return None

    return voice_skill


def _make_vad_adapter(real_vad: Callable | None) -> Callable:
    """Adapter for VAD operation to match engine convention."""

    def vad_oprim(frame: Any, **kwargs: Any) -> Any:
        if real_vad is not None:
            try:
                return real_vad(frame, **kwargs)
            except Exception:
                pass
        from veya.oprim.types import VADResult, VADState
        return VADResult(state=VADState.SILENCE, confidence=0.0, energy_db=-96.0)

    return vad_oprim


def _make_vision_adapter(real_vision: Callable | None) -> Callable:
    """Adapter for vision skill to match engine calling convention."""

    async def vision_skill(*args: Any, **kwargs: Any) -> Any:
        if real_vision is not None:
            try:
                out = real_vision(*args, **kwargs)
                if hasattr(out, "__await__"):
                    return await out
                return out
            except Exception:
                pass
        from veya.oprim.types import VisionResult
        return VisionResult(description="[Vision unavailable]", metadata={"status": "unavailable"})

    return vision_skill


def _make_image_adapter(real_image: Callable | None) -> Callable:
    """Adapter for image operations to match engine convention."""

    def image_oprim(*args: Any, **kwargs: Any) -> Any:
        if real_image is not None:
            try:
                return real_image(*args, **kwargs)
            except Exception:
                pass
        return None

    return image_oprim


def assemble_voice_agent(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the voice agent engine."""
    return assemble(build_voice_agent_manifest(config))


def assemble_vision_agent(config: dict[str, Any] | None = None) -> AssembledEngine:
    """Assemble the vision agent engine."""
    return assemble(build_vision_agent_manifest(config))


__all__ = [
    "ELEMENT_ALIASES",
    "AssembledEngine",
    "ManifestValidationError",
    "ServiceManifest",
    "assemble",
    "assemble_agent_creation",
    "assemble_agentic_loop",
    "assemble_dag_orchestrator",
    "assemble_event_workflow",
    "assemble_graph_skills",
    "assemble_long_horizon",
    "assemble_neuro_symbolic",
    "assemble_realtime_media",
    "assemble_root_cause_analysis",
    "assemble_steerable_loop",
    "assemble_swarm_orchestrator",
    "assemble_voice_agent",
    "assemble_vision_agent",
    "build_agent_creation_manifest",
    "build_agentic_loop_manifest",
    "build_event_workflow_manifest",
    "build_graph_skills_manifest",
    "build_long_horizon_manifest",
    "build_multi_agent_dag_manifest",
    "build_neuro_symbolic_manifest",
    "build_realtime_media_manifest",
    "build_root_cause_analysis_manifest",
    "build_steerable_loop_manifest",
    "build_swarm_orchestrator_manifest",
    "build_voice_agent_manifest",
    "build_vision_agent_manifest",
    "element_status",
    "get_running_engine",
    "load_decision_trail",
    "manifest_summary",
    "new_session_id",
    "register_running_engine",
    "resolve_element",
    "save_decision_trail",
    "unregister_running_engine",
    "validate_manifest",
]
