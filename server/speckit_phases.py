"""Spec Kit (SDD) Phase 0: goal_run stage templates (default off).

Spec Kit's core value is its multi-stage workflow: ``constitution → specify →
plan → tasks → implement → converge`` plus the two extension pipelines
``bug`` (assess → fix → test) and ``assess`` (intake → research → define →
shape → decide).  This module declares those pipelines as data that a
``goal_run`` task can carry, so the MasterAgent can run a Spec Kit flow through
the existing single ReAct entry point instead of a second orchestration chain.

Nothing here executes.  A template is a stage list; ``render_phase_instruction``
turns one stage into text a leaf can act on; artifacts are the
``.speckit/`` files the existing pipeline already reads and writes
(``server.spec_pack.export_speckit`` → ``server.goal_run.planner._g1_from_speckit``).

Every public entry short-circuits on ``VEYA_SPECKIT``.  With the flag off this
module imports, returns empty data, and touches nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.feature_flags import enabled as _flag_enabled

FLAG_NAME = "VEYA_SPECKIT"

#: Directory (relative to a project root) that holds every Spec Kit artifact.
SPECKIT_DIR = ".speckit"

#: Stage artifacts, in the order the SDD mainline produces them.
ARTIFACT_CONSTITUTION = "constitution.md"
ARTIFACT_SPEC = "spec.md"
ARTIFACT_PLAN = "plan.md"
ARTIFACT_TASKS = "tasks.md"
ARTIFACT_CHECKLIST = "checklist.md"

#: Templates vendored under ``templates/speckit/`` (MIT, github/spec-kit).
TEMPLATE_COMMANDS_DIR = "templates/speckit/commands"
TEMPLATE_ARTIFACTS_DIR = "templates/speckit"


@dataclass(frozen=True)
class PhaseTemplate:
    """One stage of a Spec Kit pipeline, as ``goal_run``-usable data."""

    workflow: str
    phase: str
    template: str
    artifact: str
    instruction: str


#: The four SDD pipelines.  ``artifact=""`` means the stage leaves no
#: ``.speckit/`` file of its own (it edits the previous one).
SPECKIT_PIPELINES: dict[str, tuple[dict[str, str], ...]] = {
    # Mainline SDD: the four commands plus the converge audit.
    "speckit": (
        {
            "phase": "constitution",
            "template": "constitution.md",
            "artifact": f"{SPECKIT_DIR}/" + ARTIFACT_CONSTITUTION,
            "instruction": (
                "Read templates/speckit/constitution-template.md and write the project "
                "constitution to .speckit/constitution.md. Record the non-negotiable "
                "principles and technical boundaries; do not invent constraints the "
                "user did not state."
            ),
        },
        {
            "phase": "specify",
            "template": "specify.md",
            "artifact": f"{SPECKIT_DIR}/" + ARTIFACT_SPEC,
            "instruction": (
                "Read templates/speckit/spec-template.md and the constitution, then write "
                ".speckit/spec.md from the user's requirement. State acceptance scenarios; "
                "leave technical decisions out of this file."
            ),
        },
        {
            "phase": "plan",
            "template": "plan.md",
            "artifact": f"{SPECKIT_DIR}/" + ARTIFACT_PLAN,
            "instruction": (
                "Read templates/speckit/plan-template.md, the spec and the constitution, "
                "then write .speckit/plan.md. Every technical choice needs a rationale "
                "traceable to a spec requirement."
            ),
        },
        {
            "phase": "tasks",
            "template": "tasks.md",
            "artifact": f"{SPECKIT_DIR}/" + ARTIFACT_TASKS,
            "instruction": (
                "Read templates/speckit/tasks-template.md and the plan, then write "
                ".speckit/tasks.md in Spec Kit checkbox format: '- [ ] T1: 标题' with "
                "optional indented 'Depends: T2,T3' and 'Accept: ...' child lines. This is "
                "the file server.goal_run.planner compiles into the task graph, so every "
                "task needs a non-empty acceptance."
            ),
        },
        {
            "phase": "implement",
            "template": "implement.md",
            "artifact": "",
            "instruction": (
                "Execute the tasks in .speckit/tasks.md with the existing coding path "
                "(coding_task_run or run_in_sandbox). Tick each task as it is verified. "
                "Do not edit tasks.md to make it look done."
            ),
        },
        {
            "phase": "converge",
            "template": "converge.md",
            "artifact": f"{SPECKIT_DIR}/" + ARTIFACT_CHECKLIST,
            "instruction": (
                "Compare the codebase against .speckit/{spec,plan,tasks}.md and write the "
                "checklist to .speckit/checklist.md. Append any unbuilt work back into "
                "tasks.md instead of claiming completion."
            ),
        },
    ),
    # Bug pipeline: assess → fix → test, per-bug artifacts.
    "bug": (
        {
            "phase": "assess",
            "template": "bug.assess",
            "artifact": f"{SPECKIT_DIR}/bugs/{{slug}}/assessment.md",
            "instruction": (
                "Read templates/speckit/commands/bug.assess.md, reproduce the reported bug "
                "against the codebase, and write the assessment to the bug directory. State "
                "root cause, blast radius and proposed remediation; do not change code yet."
            ),
        },
        {
            "phase": "fix",
            "template": "bug.fix",
            "artifact": "",
            "instruction": (
                "Read templates/speckit/commands/bug.fix.md and the assessment, then apply "
                "the remediation with the existing coding path. Record what changed and why "
                "in the bug directory."
            ),
        },
        {
            "phase": "test",
            "template": "bug.test",
            "artifact": f"{SPECKIT_DIR}/bugs/{{slug}}/verification.md",
            "instruction": (
                "Read templates/speckit/commands/bug.test.md and verify the fix with the "
                "project's own test command. Write the verification report; a fix without a "
                "reproduced-then-passing test is not verified."
            ),
        },
    ),
    # Idea assessment pipeline: intake → research → define → shape → decide.
    "assess": (
        {
            "phase": "intake",
            "template": "assess.intake",
            "artifact": f"{SPECKIT_DIR}/assess/{{slug}}/intake.md",
            "instruction": (
                "Read templates/speckit/commands/assess.intake.md and capture the idea as "
                "stated by the user, without judging feasibility yet."
            ),
        },
        {
            "phase": "research",
            "template": "assess.research",
            "artifact": f"{SPECKIT_DIR}/assess/{{slug}}/research.md",
            "instruction": (
                "Read templates/speckit/commands/assess.research.md and gather the context "
                "the decision needs: existing code, prior art, constraints and costs."
            ),
        },
        {
            "phase": "define",
            "template": "assess.define",
            "artifact": f"{SPECKIT_DIR}/assess/{{slug}}/definition.md",
            "instruction": (
                "Read templates/speckit/commands/assess.define.md and turn the research into "
                "a crisp problem statement with measurable success criteria."
            ),
        },
        {
            "phase": "shape",
            "template": "assess.shape",
            "artifact": f"{SPECKIT_DIR}/assess/{{slug}}/shape.md",
            "instruction": (
                "Read templates/speckit/commands/assess.shape.md and outline candidate "
                "shapes with their trade-offs; do not pick one yet."
            ),
        },
        {
            "phase": "decide",
            "template": "assess.decide",
            "artifact": f"{SPECKIT_DIR}/assess/{{slug}}/decision.md",
            "instruction": (
                "Read templates/speckit/commands/assess.decide.md and record the verdict with "
                "its reasoning. A go verdict hands off to the speckit pipeline; this "
                "pipeline stops here."
            ),
        },
    ),
}


def enabled() -> bool:
    """Whether the Spec Kit integration is on (canonical flag authority)."""
    try:
        return _flag_enabled(FLAG_NAME)
    except KeyError:
        return False


def phase_templates() -> dict[str, tuple[dict[str, str], ...]]:
    """All pipelines, or an empty mapping when the flag is off."""
    if not enabled():
        return {}
    return {name: phases for name, phases in SPECKIT_PIPELINES.items()}


def phase_template(workflow: str) -> tuple[dict[str, str], ...]:
    """One pipeline's stage list; empty when unknown or disabled."""
    if not enabled():
        return ()
    return SPECKIT_PIPELINES.get(str(workflow or "").strip().lower(), ())


def workflows() -> tuple[str, ...]:
    """Pipeline ids, or an empty tuple when disabled."""
    if not enabled():
        return ()
    return tuple(SPECKIT_PIPELINES)


def render_phase_instruction(
    workflow: str,
    phase: str,
    *,
    slug: str = "",
    goal_text: str = "",
) -> str:
    """Render one stage as a ``goal_run`` task instruction.

    The rendered text carries the artifact path so the leaf knows where the
    stage's output must land, and the vendored template path so the model can
    read the upstream instructions on demand instead of having them inlined.
    """
    if not enabled():
        return ""
    for stage in phase_template(workflow):
        if stage["phase"] != str(phase or "").strip().lower():
            continue
        artifact = stage["artifact"].format(slug=slug or "feature")
        lines = [
            f"# Spec Kit stage: {workflow} / {stage['phase']}",
            "",
            stage["instruction"],
            "",
            f"Upstream template: {stage['template']}",
        ]
        if artifact:
            lines.append(f"Artifact: {artifact}")
        if goal_text:
            lines.extend(["", f"Goal: {goal_text}"])
        return "\n".join(lines)
    return ""


def status() -> dict[str, Any]:
    """Read-only projection; never spawns, never writes."""
    if not enabled():
        return {
            "name": "speckit",
            "available": False,
            "reason": f"unavailable: flag disabled ({FLAG_NAME})",
        }
    return {
        "name": "speckit",
        "available": True,
        "reason": "ready",
        "workflows": list(SPECKIT_PIPELINES),
        "stages": {
            name: [s["phase"] for s in phases] for name, phases in SPECKIT_PIPELINES.items()
        },
    }


__all__ = [
    f"{SPECKIT_DIR}/ARTIFACT_CHECKLIST",
    f"{SPECKIT_DIR}/ARTIFACT_CONSTITUTION",
    f"{SPECKIT_DIR}/ARTIFACT_PLAN",
    f"{SPECKIT_DIR}/ARTIFACT_SPEC",
    f"{SPECKIT_DIR}/ARTIFACT_TASKS",
    "FLAG_NAME",
    "PhaseTemplate",
    "SPECKIT_DIR",
    "SPECKIT_PIPELINES",
    "TEMPLATE_ARTIFACTS_DIR",
    "TEMPLATE_COMMANDS_DIR",
    "enabled",
    "phase_template",
    "phase_templates",
    "render_phase_instruction",
    "status",
    "workflows",
]
