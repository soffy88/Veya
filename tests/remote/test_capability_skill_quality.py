"""Capability / skill / task-memory quality plane tests (P0: A, B, C)."""

from __future__ import annotations

from pathlib import Path

from veya.remote.capabilities import (
    CapabilityHealth,
    CapabilityProvider,
    CapabilityRegistry,
)
from veya.remote.skills import SkillPermission, SkillRecord, SkillRegistry
from veya.supervision.task_memory import Plan, TaskMemory


# ── A: task working memory ────────────────────────────────────────────
def test_task_memory_is_projection_not_authority(tmp_path: Path) -> None:
    memory = TaskMemory(tmp_path, "m1")
    memory.write_plan(
        Plan(goal="ship", acceptance=["tests pass"], phases=["plan", "exec"], current_phase="exec")
    )
    memory.add_finding("uses FastAPI", evidence="pyproject.toml")
    memory.add_progress("decomposed into 4 subtasks")
    context = memory.recovery_context()
    assert context["mission_store_authority"] is True
    assert context["markdown_plan_authority"] is False
    assert "goal: ship" in context["compact_plan"]
    assert any("FastAPI" in f for f in context["recent_findings"])
    assert any("decomposed" in p for p in context["recent_progress"])


def test_task_memory_error_repetition_guard(tmp_path: Path) -> None:
    memory = TaskMemory(tmp_path, "m2")
    assert memory.should_skip_repeat(action="run pytest", error_class="ImportError") is False
    memory.record_error(action="run pytest", error_class="ImportError", attempt=1, evidence="log")
    assert memory.should_skip_repeat(action="run pytest", error_class="ImportError") is True
    assert memory.should_skip_repeat(action="run pytest", error_class="TimeoutError") is False
    memory.record_error(
        action="run pytest",
        error_class="ImportError",
        attempt=2,
        evidence="fixed import",
        resolution="added dependency",
    )
    assert memory.should_skip_repeat(action="run pytest", error_class="ImportError") is False


def test_task_memory_recovery_context_is_bounded(tmp_path: Path) -> None:
    memory = TaskMemory(tmp_path, "m3")
    for i in range(50):
        memory.add_progress(f"step {i}")
    context = memory.recovery_context(recent=5)
    assert len(context["recent_progress"]) == 5


# ── B: capability registry ────────────────────────────────────────────
async def _unavailable() -> tuple[str, str]:
    return str(CapabilityHealth.AUTH_REQUIRED), "no token"


async def _healthy() -> tuple[str, str]:
    return str(CapabilityHealth.HEALTHY), "functional probe ok"


async def test_capability_ordered_fallback_and_evidence() -> None:
    registry = CapabilityRegistry()
    registry.register(
        "github.search",
        [
            CapabilityProvider(
                "gh-cli", priority=1, prober=_unavailable, credential_ref="gh-cli-token"
            ),
            CapabilityProvider("github-mcp", priority=2, prober=_healthy),
            CapabilityProvider("web-fallback", priority=3, prober=_healthy),
        ],
    )
    selected, evidence = await registry.select("github.search")
    assert selected is not None and selected.provider_id == "github-mcp"
    assert evidence["selected_provider"] == "github-mcp"
    assert evidence["rejected"][0]["provider_id"] == "gh-cli"
    # credential is a reference only
    assert selected.credential_ref is None
    assert registry.get("github.search").ordered()[0].credential_ref == "gh-cli-token"


async def test_capability_doctor_never_false_healthy() -> None:
    registry = CapabilityRegistry()
    registry.register(
        "web.read",
        [CapabilityProvider("broken", priority=1, prober=_unavailable)],
    )
    report = await registry.doctor()
    assert report[0]["selected_provider"] is None
    assert report[0]["health"] != str(CapabilityHealth.HEALTHY)
    assert report[0]["auth_state"] == str(CapabilityHealth.AUTH_REQUIRED)


# ── C: skill registry ─────────────────────────────────────────────────
def _skill(tmp_path: Path, skill_id: str = "frontend-design") -> SkillRecord:
    root = tmp_path / skill_id
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text("# Workflow\nstep 1\nstep 2\n", encoding="utf-8")
    (root / "references" / "tokens.md").write_text("tokens", encoding="utf-8")
    return SkillRecord(
        skill_id=skill_id,
        name="Frontend Design",
        description="design intent + quality gates",
        triggers=["frontend", "dashboard", "ui"],
        source="github.com/example/skills",
        source_commit="abc123",
        license="MIT",
        permissions_required=[SkillPermission.FILESYSTEM_READ, SkillPermission.NETWORK],
        trust_level="UNTRUSTED_CONTENT",
        eval_status="UNKNOWN",
        root=str(root),
    )


def test_skill_progressive_disclosure(tmp_path: Path) -> None:
    registry = SkillRegistry()
    registry.sync_from_canonical([_skill(tmp_path)])
    metadata = registry.metadata()
    assert metadata[0]["name"] == "Frontend Design"
    assert "step 1" not in str(metadata)  # Level 1 only
    assert "step 1" in registry.load_core("frontend-design")  # Level 2 on demand
    resources = registry.load_resources("frontend-design")  # Level 3 on demand
    assert any("tokens.md" in path for path in resources)


def test_skill_trigger_resolution_records_reasons(tmp_path: Path) -> None:
    registry = SkillRegistry()
    registry.sync_from_canonical([_skill(tmp_path)])
    resolution = registry.resolve("build a dashboard ui")
    assert "frontend-design" in resolution["candidates"]
    assert resolution["selected"][0]["reason"].startswith("trigger:")
    assert resolution["declined"] == []


def test_skill_permission_governance_blocks_escalation(tmp_path: Path) -> None:
    registry = SkillRegistry()
    registry.sync_from_canonical([_skill(tmp_path)])
    allowed, missing = registry.check_permissions(
        "frontend-design", [str(SkillPermission.FILESYSTEM_READ)]
    )
    assert allowed is False
    assert str(SkillPermission.NETWORK) in missing


def test_skill_provenance_recorded(tmp_path: Path) -> None:
    registry = SkillRegistry()
    registry.sync_from_canonical([_skill(tmp_path)])
    provenance = registry.get("frontend-design").provenance()
    assert provenance["source_commit"] == "abc123"
    assert provenance["license"] == "MIT"
    assert provenance["trust_level"] == "UNTRUSTED_CONTENT"


# ── D/E/F/G/H: shared execution capability context ────────────────────
async def test_execution_context_shared_bounded_and_credential_isolated(tmp_path: Path) -> None:
    from veya.remote.execution_context import ExecutionContextBuilder
    from veya.supervision.task_memory import Plan, TaskMemory

    capability_registry = CapabilityRegistry()
    capability_registry.register(
        "github.search",
        [CapabilityProvider("gh-cli", priority=1, prober=_healthy, credential_ref="ref-only")],
    )
    skill_registry = SkillRegistry()
    skill_registry.sync_from_canonical([_skill(tmp_path)])
    memory = TaskMemory(tmp_path, "m1")
    memory.write_plan(Plan(goal="build a dashboard ui", acceptance=["responsive"]))
    memory.add_finding("uses SvelteKit", evidence="package.json")
    for i in range(40):
        memory.add_progress(f"step {i}")

    builder = ExecutionContextBuilder(
        capability_registry=capability_registry,
        skill_registry=skill_registry,
        task_memory=memory,
        permission_policy={
            "allowed": [str(SkillPermission.FILESYSTEM_READ), str(SkillPermission.NETWORK)]
        },
    )
    context, evidence = await builder.build(
        mission_id="m1",
        execution_id="ex_1",
        worker_type="hicode",
        workspace=str(tmp_path),
        capability_ids=["github.search"],
        task_text="build a dashboard ui",
    )
    public = context.to_public()
    # shared capability evidence + credential reference only
    assert evidence["capability_routing"][0]["selected_provider"] == "gh-cli"
    assert public["selected_capabilities"][0]["credential_ref"] == "ref-only"
    assert "api_key" not in str(public).lower()
    # skill selected (Level 2 loaded for selected only)
    assert evidence["selected_skills"] == ["frontend-design"]
    assert public["selected_skills"][0]["trust_level"] == "UNTRUSTED_CONTENT"
    # bounded task memory (never full history)
    assert len(public["task_memory_context"]["recent_progress"]) == 5


async def test_execution_context_permission_denied_skill(tmp_path: Path) -> None:
    from veya.remote.execution_context import ExecutionContextBuilder

    skill_registry = SkillRegistry()
    skill_registry.sync_from_canonical([_skill(tmp_path)])
    builder = ExecutionContextBuilder(
        skill_registry=skill_registry,
        permission_policy={"allowed": [str(SkillPermission.FILESYSTEM_READ)]},
    )
    _context, evidence = await builder.build(
        mission_id="m1",
        execution_id="ex_1",
        worker_type="pi",
        workspace=str(tmp_path),
        task_text="build a dashboard ui",
    )
    assert evidence["selected_skills"] == []
    assert evidence["declined_skills"][0]["reason"].startswith("permission_denied:")


# ── J/K: skill eval harness ───────────────────────────────────────────
def test_skill_eval_precision_recall_and_negative_cases(tmp_path: Path) -> None:
    from veya.remote.skill_eval import SkillEvalCase, evaluate_skill

    registry = SkillRegistry()
    registry.sync_from_canonical([_skill(tmp_path)])
    result = evaluate_skill(
        registry,
        "frontend-design",
        [
            SkillEvalCase("build a dashboard ui", expected_trigger=True),
            SkillEvalCase("design a frontend page", expected_trigger=True),
            SkillEvalCase("fix a python import error", expected_trigger=False),
            SkillEvalCase("run database migration", expected_trigger=False),
        ],
        allowed_permissions=[str(SkillPermission.FILESYSTEM_READ), str(SkillPermission.NETWORK)],
    )
    assert result["trigger_precision"] == 1.0
    assert result["trigger_recall"] == 1.0
    assert result["false_positive"] == 0
    assert result["permission_regression"] is True
    assert result["context_cost_bytes"] > 0


def test_skill_eval_detects_permission_regression(tmp_path: Path) -> None:
    from veya.remote.skill_eval import SkillEvalCase, evaluate_skill

    registry = SkillRegistry()
    registry.sync_from_canonical([_skill(tmp_path)])
    result = evaluate_skill(
        registry,
        "frontend-design",
        [SkillEvalCase("build a dashboard ui", expected_trigger=True, expected_permission_ok=True)],
        allowed_permissions=[str(SkillPermission.FILESYSTEM_READ)],
    )
    assert result["permission_regression"] is False
    assert result["permission_regressions"] == 1


async def test_render_context_bounded_no_secrets_and_budget(tmp_path: Path) -> None:
    from veya.remote.execution_context import (
        ExecutionContextBuilder,
        context_budget,
        context_hash,
        render_execution_context,
        shared_capability_registry,
        shared_skill_registry,
    )

    # single shared registry authority
    assert shared_capability_registry() is shared_capability_registry()
    assert shared_skill_registry() is shared_skill_registry()

    capability_registry = CapabilityRegistry()
    capability_registry.register(
        "github.search",
        [CapabilityProvider("gh-cli", priority=1, prober=_healthy, credential_ref="ref-only")],
    )
    skill_registry = SkillRegistry()
    skill_registry.sync_from_canonical([_skill(tmp_path)])
    builder = ExecutionContextBuilder(
        capability_registry=capability_registry,
        skill_registry=skill_registry,
        permission_policy={
            "allowed": [str(SkillPermission.FILESYSTEM_READ), str(SkillPermission.NETWORK)]
        },
    )
    context, _evidence = await builder.build(
        mission_id="m1",
        execution_id="ex_1",
        worker_type="pi",
        workspace=str(tmp_path),
        capability_ids=["github.search"],
        task_text="build a dashboard ui",
    )
    rendered = render_execution_context(context)
    assert "capabilities" in rendered and "selected skills" in rendered
    assert "api_key" not in rendered.lower()
    assert "ref-only" not in rendered  # credential reference not leaked into prompt
    budget = context_budget(context)
    assert budget["total_injected_context_bytes"] == len(rendered)
    assert budget["selected_skill_count"] == 1
    assert budget["loaded_level3_resource_count"] == 0  # Level 3 on demand only
    assert context_hash(context) == context_hash(context)
